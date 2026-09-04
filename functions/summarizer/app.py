import json
import logging
import os

import anthropic
import boto3
from botocore.exceptions import ClientError

from common.duration import incident_duration

logger = logging.getLogger()
logger.setLevel(logging.INFO)

_secrets_client = boto3.client("secretsmanager")
_lambda_client = boto3.client("lambda")
_dynamodb = boto3.resource("dynamodb")
_incident_table = None
_api_key_cache: dict[str, str] = {}


def _get_incident_table():
    global _incident_table
    if _incident_table is None:
        _incident_table = _dynamodb.Table(os.environ["INCIDENT_TABLE_NAME"])
    return _incident_table


def _get_api_key() -> str:
    arn = os.environ["ANTHROPIC_API_KEY_SECRET_ARN"]
    if arn not in _api_key_cache:
        response = _secrets_client.get_secret_value(SecretId=arn)
        _api_key_cache[arn] = response["SecretString"]
    return _api_key_cache[arn]


def _build_prompt(incident: dict) -> str:
    alerts = incident.get("source_alerts", [])
    alert_names = ", ".join(a["alert_name"] for a in alerts)
    first_seen = alerts[0]["received_at"] if alerts else "unknown"
    last_seen = alerts[-1]["received_at"] if len(alerts) > 1 else first_seen

    return f"""You are an on-call engineer assistant. Analyze this incident and produce a structured operational summary.

Incident:
- Affected service: {incident["affected_service"]}
- Severity: {incident["severity"]}
- Alert count: {len(alerts)}
- Alerts: {alert_names}
- First seen: {first_seen}
- Last seen: {last_seen}

Respond with a JSON object containing exactly these three fields:
{{
  "summary": "One concise paragraph describing what is happening and its operational impact",
  "likely_cause": "The most probable root cause based on the alert pattern",
  "next_step": "The single most important action the on-call engineer should take right now"
}}

Return only the JSON object. Do not include markdown, code fences, or any other text."""


def _build_recovery_prompt(incident: dict) -> str:
    alerts = incident.get("source_alerts", [])
    alert_lines = "\n".join(f"- {a['alert_name']} ({a['source']}, {a.get('status', '?')}) at {a.get('received_at', '?')}" for a in alerts)
    duration = incident_duration(incident) or "unknown"
    original = incident.get("llm_summary") or "(none)"

    return f"""You are an on-call engineer assistant. This incident has just RECOVERED. Write the closing note.

Incident:
- Affected service: {incident["affected_service"]}
- Severity while open: {incident["severity"]}
- Opened: {incident.get("created_at", "unknown")}
- Resolved: {incident.get("resolved_at", "unknown")}
- Duration: {duration}
- Alert timeline:
{alert_lines}
- Summary written while it was open: {original}

Respond with a JSON object containing exactly these three fields:
{{
  "summary": "One concise paragraph: what happened, for how long, and that it has recovered",
  "likely_cause": "The most probable root cause, revised in light of the recovery",
  "next_step": "The single most useful follow-up now that it is over (a fix, a check, or a post-incident action)"
}}

Return only the JSON object. Do not include markdown, code fences, or any other text."""


def _call_llm(incident: dict, recovered: bool = False) -> dict:
    client = anthropic.Anthropic(api_key=_get_api_key())
    prompt = _build_recovery_prompt(incident) if recovered else _build_prompt(incident)
    message = client.messages.create(
        model=os.environ["MODEL_ID"],
        max_tokens=1024,
        messages=[{"role": "user", "content": prompt}],
    )
    return json.loads(message.content[0].text)


def _fallback_summary(incident: dict, recovered: bool = False) -> dict:
    alerts = incident.get("source_alerts", [])
    service = incident.get("affected_service", "unknown")
    if recovered:
        duration = incident_duration(incident)
        return {
            "summary": f"{service} recovered after {duration or 'an unknown duration'} ({len(alerts)} alert(s)). Automated summarization unavailable.",
            "likely_cause": "Unable to determine — LLM summarization failed.",
            "next_step": "Review the incident timeline manually.",
        }
    return {
        "summary": f"{len(alerts)} alert(s) triggered for {service}. Automated summarization unavailable.",
        "likely_cause": "Unable to determine — LLM summarization failed.",
        "next_step": "Investigate the alert list manually.",
    }


def handler(event: dict, context) -> dict | None:
    incident_id = event.get("incident_id")
    if not incident_id:
        logger.error("No incident_id in event")
        return None

    response = _get_incident_table().get_item(Key={"incident_id": incident_id})
    incident = response.get("Item")
    if not incident:
        logger.error("Incident %s not found in DynamoDB", incident_id)
        return None

    recovered = bool(event.get("recovered"))
    field = "recovery_summary" if recovered else "llm_summary"

    try:
        structured = _call_llm(incident, recovered=recovered)
        llm_summary = json.dumps(structured)
        logger.info("LLM %s generated for incident %s", field, incident_id)
    except Exception:
        logger.exception("LLM summarization failed for incident %s, using fallback", incident_id)
        llm_summary = json.dumps(_fallback_summary(incident, recovered=recovered))

    # Write the summary and claim delivery in one conditional update (RC1-384).
    #
    # `dedup` invokes this function with InvocationType="Event", so a failed
    # invocation is retried by Lambda twice. The Slack invoke below is the last
    # thing this handler does, which means a retry of a run that already got
    # that far posts the same summary into the thread a second time. That is
    # not hypothetical: the function was timing out *after* delivering, so
    # every incident was a retry candidate.
    #
    # `source_alerts` grows by one per alert joined to the incident, so its
    # length identifies the generation being summarized. A retry of the same
    # generation fails the condition and stops before Slack; a genuine
    # re-summary — a new alert landed in the window — carries a higher count
    # and proceeds, which is the threaded-reply behavior the chain is built on.
    #
    # Deliberately claimed *after* the model call rather than before: claiming
    # first would make a crash mid-summarize look delivered, and a dropped
    # incident is worse than a repeated model call on a rare retry.
    marker = "recovery_summarized_count" if recovered else "summarized_alert_count"
    generation = len(incident.get("source_alerts") or [])
    try:
        _get_incident_table().update_item(
            Key={"incident_id": incident_id},
            UpdateExpression=f"SET {field} = :s, {marker} = :g",
            ConditionExpression=f"attribute_not_exists({marker}) OR {marker} < :g",
            ExpressionAttributeValues={":s": llm_summary, ":g": generation},
        )
    except ClientError as e:
        if e.response["Error"]["Code"] != "ConditionalCheckFailedException":
            raise
        logger.info(
            "Incident %s already delivered %s for generation %s, not notifying again",
            incident_id,
            field,
            generation,
        )
        return None
    logger.info("%s written to DynamoDB for incident %s", field, incident_id)

    _lambda_client.invoke(
        FunctionName=os.environ["SLACK_NOTIFIER_FUNCTION_NAME"],
        InvocationType="Event",
        Payload=json.dumps(_handoff(incident_id, recovered)),
    )
    logger.info("Slack notifier invoked for incident %s", incident_id)

    return {"incident_id": incident_id, field: llm_summary}


def _handoff(incident_id: str, recovered: bool) -> dict:
    # The flag rides the whole delivery chain; absent means the usual open-incident rendering.
    payload = {"incident_id": incident_id}
    if recovered:
        payload["recovered"] = True
    return payload
