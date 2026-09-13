import json
import logging
import os

import anthropic
from botocore.exceptions import ClientError

from common import aws
from common.duration import incident_duration
from common import recurrence as recurrence_text
from delivery import datadog_events, jira, slack

logger = logging.getLogger()
logger.setLevel(logging.INFO)

_anthropic_client = None

# The delivery chain, in the order the Datadog event needs: it is last so it
# carries both the Slack and Jira links. Each stage is idempotent about its
# own artifact (thread, ticket, event row) and records the generation it
# delivered, so a retried invocation resumes at the first stage that did not
# finish instead of posting the earlier ones again.
_STAGES = (
    ("slack", slack.deliver),
    ("jira", jira.deliver),
    ("datadog", datadog_events.deliver),
)


def _get_incident_table():
    return aws.table("INCIDENT_TABLE_NAME")


def _get_api_key() -> str:
    return aws.secret(os.environ["ANTHROPIC_API_KEY_SECRET_ARN"])


def _build_prompt(incident: dict) -> str:
    alerts = incident.get("source_alerts", [])
    alert_names = ", ".join(a["alert_name"] for a in alerts)
    first_seen = alerts[0]["received_at"] if alerts else "unknown"
    last_seen = alerts[-1]["received_at"] if len(alerts) > 1 else first_seen

    recurring = recurrence_text.sentence(incident)
    recurrence_line = (
        f"\n- Recurrence: {recurring} Treat it as a recurring problem: say so in the summary, "
        "and make the next step about breaking the pattern, not about triage."
        if recurring else ""
    )

    return f"""You are an on-call engineer assistant. Analyze this incident and produce a structured operational summary.

Incident:
- Affected service: {incident["affected_service"]}
- Severity: {incident["severity"]}
- Alert count: {len(alerts)}
- Alerts: {alert_names}
- First seen: {first_seen}
- Last seen: {last_seen}{recurrence_line}

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


def _get_anthropic_client() -> anthropic.Anthropic:
    """One client for the life of the execution environment.

    It used to be constructed inside `_call_llm`, so every invocation left
    another `httpx` connection pool behind and paid for a fresh TLS
    handshake. Reusing it is the same pattern `common.aws` follows for tables
    and secrets.

    History, so nobody re-derives it: this was first written as the fix for
    the python3.14 hang (RC1-385) on the reasoning that the summarizer was
    the only function using the Anthropic SDK and the only one hanging. It
    was not the fix. The hang was the 128 MB memory ceiling — the function
    needs ~220 MB — and is fixed in template.yaml, not here. This stays
    because leaking a connection pool per invocation is worth not doing on
    any runtime.
    """
    global _anthropic_client
    if _anthropic_client is None:
        _anthropic_client = anthropic.Anthropic(api_key=_get_api_key())
    return _anthropic_client


def _call_llm(incident: dict, recovered: bool = False) -> dict:
    client = _get_anthropic_client()
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
    # `source_alerts` grows by one per alert joined to the incident (the
    # recovery alert included), so its length identifies the generation
    # being summarized and delivered.
    generation = len(incident.get("source_alerts") or [])

    llm_summary = _summarize(incident, field, generation, recovered)
    if llm_summary is None:
        return None

    delivered = _deliver(incident, generation, recovered)
    return {"incident_id": incident_id, field: llm_summary, **delivered}


def _summarize(incident: dict, field: str, generation: int, recovered: bool) -> str | None:
    """Write this generation's summary once; return it, or None when a newer
    generation has superseded this invocation.

    Ingest invokes this function with InvocationType="Event", so a failed
    invocation is retried by Lambda twice, and two alerts landing seconds
    apart run as two concurrent invocations for consecutive generations. The
    summary write carries the generation it summarized and refuses to go
    backwards (RC1-384):

    - A retry of a generation already summarized skips the model call and
      goes straight to delivery, where the per-stage markers decide what is
      still owed.
    - An invocation whose generation is behind the marker has been
      superseded — the newer invocation delivers the newer summary — and
      stops here.

    Claimed *after* the model call rather than before: claiming first would
    make a crash mid-summarize look delivered, and a dropped incident is
    worse than a repeated model call on a rare retry.
    """
    incident_id = incident["incident_id"]
    marker = "recovery_summarized_count" if recovered else "summarized_alert_count"
    summarized = int(incident.get(marker, -1))
    if summarized > generation:
        logger.info("Incident %s already summarized generation %s, superseding %s", incident_id, summarized, generation)
        return None
    if summarized == generation:
        logger.info("Incident %s generation %s already summarized, resuming delivery", incident_id, generation)
        return incident.get(field)

    try:
        structured = _call_llm(incident, recovered=recovered)
        llm_summary = json.dumps(structured)
        logger.info("LLM %s generated for incident %s", field, incident_id)
    except Exception:
        logger.exception("LLM summarization failed for incident %s, using fallback", incident_id)
        llm_summary = json.dumps(_fallback_summary(incident, recovered=recovered))

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
        # Someone claimed this or a newer generation between our read and
        # write. Re-read to learn which, and to deliver the stored summary
        # rather than our unclaimed one.
        fresh = _get_incident_table().get_item(Key={"incident_id": incident_id}).get("Item") or {}
        incident.update(fresh)
        if int(incident.get(marker, -1)) > generation:
            logger.info("Incident %s generation %s superseded before its summary was written", incident_id, generation)
            return None
        logger.info("Incident %s generation %s summarized concurrently, resuming delivery", incident_id, generation)
        return incident.get(field)

    incident[field] = llm_summary
    incident[marker] = generation
    logger.info("%s written to DynamoDB for incident %s", field, incident_id)
    return llm_summary


def _deliver(incident: dict, generation: int, recovered: bool) -> dict:
    """Run the delivery chain in order, skipping stages this generation has
    already completed; return the artifact IDs."""
    incident_id = incident["incident_id"]
    delivered = {}
    for stage, deliver in _STAGES:
        marker = f"{stage}_delivered_count"
        if int(incident.get(marker, -1)) >= generation:
            logger.info("Incident %s generation %s already delivered to %s, skipping", incident_id, generation, stage)
            continue
        delivered[stage] = deliver(incident, recovered)
        _mark_delivered(incident, marker, generation)
    return {
        "slack_thread_id": incident.get("slack_thread_id"),
        "jira_ticket_id": incident.get("jira_ticket_id"),
        "datadog_event_id": incident.get("datadog_event_id"),
        "delivered": list(delivered),
    }


def _mark_delivered(incident: dict, marker: str, generation: int) -> None:
    try:
        _get_incident_table().update_item(
            Key={"incident_id": incident["incident_id"]},
            UpdateExpression=f"SET {marker} = :g",
            ConditionExpression=f"attribute_not_exists({marker}) OR {marker} < :g",
            ExpressionAttributeValues={":g": generation},
        )
    except ClientError as e:
        if e.response["Error"]["Code"] != "ConditionalCheckFailedException":
            raise
        # A newer generation got there first; its marker stands.
        return
    incident[marker] = generation
