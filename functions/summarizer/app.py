import json
import logging
import os

import anthropic
import boto3

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


def _call_llm(incident: dict) -> dict:
    client = anthropic.Anthropic(api_key=_get_api_key())
    message = client.messages.create(
        model=os.environ["MODEL_ID"],
        max_tokens=1024,
        messages=[{"role": "user", "content": _build_prompt(incident)}],
    )
    return json.loads(message.content[0].text)


def _fallback_summary(incident: dict) -> dict:
    alerts = incident.get("source_alerts", [])
    service = incident.get("affected_service", "unknown")
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

    try:
        structured = _call_llm(incident)
        llm_summary = json.dumps(structured)
        logger.info("LLM summary generated for incident %s", incident_id)
    except Exception:
        logger.exception("LLM summarization failed for incident %s, using fallback", incident_id)
        llm_summary = json.dumps(_fallback_summary(incident))

    _get_incident_table().update_item(
        Key={"incident_id": incident_id},
        UpdateExpression="SET llm_summary = :s",
        ExpressionAttributeValues={":s": llm_summary},
    )
    logger.info("llm_summary written to DynamoDB for incident %s", incident_id)

    _lambda_client.invoke(
        FunctionName=os.environ["SLACK_NOTIFIER_FUNCTION_NAME"],
        InvocationType="Event",
        Payload=json.dumps({"incident_id": incident_id}),
    )
    logger.info("Slack notifier invoked for incident %s", incident_id)

    return {"incident_id": incident_id, "llm_summary": llm_summary}
