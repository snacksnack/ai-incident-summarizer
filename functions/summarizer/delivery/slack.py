"""Slack: one thread per incident. A new incident opens the thread; every later
summary and the recovery reply inside it."""
import json
import logging
import os
import time

from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

from common import aws
from common import recurrence
from common.duration import incident_duration

logger = logging.getLogger()

_SEVERITY_EMOJI = {
    "critical": "🔴",
    "high": "🟠",
    "medium": "🟡",
    "low": "🟢",
}


def deliver(incident: dict, recovered: bool = False) -> str:
    """Post the incident's current summary; return the thread it lives in."""
    channel = os.environ["SLACK_CHANNEL_ID"]
    client = WebClient(token=_get_token())
    text = _build_recovery_message(incident) if recovered else _build_message(incident)
    incident_id = incident["incident_id"]
    slack_thread_id = incident.get("slack_thread_id")

    if slack_thread_id:
        _post_with_retry(client, channel=channel, text=text, thread_ts=slack_thread_id)
        logger.info("Posted %s to thread %s for incident %s", "recovery" if recovered else "reply", slack_thread_id, incident_id)
        return slack_thread_id

    result = _post_with_retry(client, channel=channel, text=text)
    thread_ts = result["ts"]
    aws.table("INCIDENT_TABLE_NAME").update_item(
        Key={"incident_id": incident_id},
        UpdateExpression="SET slack_thread_id = :ts",
        ExpressionAttributeValues={":ts": thread_ts},
    )
    incident["slack_thread_id"] = thread_ts
    logger.info("Opened new thread %s for incident %s", thread_ts, incident_id)
    return thread_ts


def _get_token() -> str:
    return aws.secret(os.environ["SLACK_BOT_TOKEN_SECRET_ARN"])


def _build_message(incident: dict) -> str:
    severity = incident.get("severity", "").lower()
    emoji = _SEVERITY_EMOJI.get(severity, "")
    badge = f"{emoji} *{severity.upper()}*" if emoji else f"*{severity.upper()}*"
    service = incident["affected_service"]
    created_at = incident.get("created_at", "unknown")

    header = f"{badge} | {service} | {created_at}"
    repeat = recurrence.badge(incident)
    if repeat:
        header += f" | 🔁 {repeat}"
        previous = recurrence.previous_ticket(incident)
        if previous:
            header += f" (previous: {previous})"

    llm_summary = incident.get("llm_summary")
    if llm_summary:
        try:
            parsed = json.loads(llm_summary)
            body = (
                f"*Summary:* {parsed['summary']}\n"
                f"*Likely cause:* {parsed['likely_cause']}\n"
                f"*Next step:* {parsed['next_step']}"
            )
        except (json.JSONDecodeError, KeyError):
            body = _raw_alert_list(incident)
    else:
        body = _raw_alert_list(incident)

    return f"{header}\n\n{body}"


def _build_recovery_message(incident: dict) -> str:
    service = incident["affected_service"]
    duration = incident_duration(incident)
    header = f"🟢 *RESOLVED* | {service} | {incident.get('resolved_at', 'unknown')}"
    if duration:
        header += f" | open for {duration}"

    try:
        parsed = json.loads(incident.get("recovery_summary") or "")
        body = (
            f"*Summary:* {parsed['summary']}\n"
            f"*Likely cause:* {parsed['likely_cause']}\n"
            f"*Next step:* {parsed['next_step']}"
        )
    except (json.JSONDecodeError, KeyError, TypeError):
        body = "Incident resolved.\n" + _raw_alert_list(incident)

    return f"{header}\n\n{body}"


def _raw_alert_list(incident: dict) -> str:
    alerts = incident.get("source_alerts", [])
    if not alerts:
        return "No alert details available."
    return "\n".join(f"• {a['alert_name']} ({a['source']})" for a in alerts)


def _post_with_retry(client: WebClient, **kwargs) -> dict:
    for attempt in range(3):
        try:
            return client.chat_postMessage(**kwargs)
        except SlackApiError as e:
            if attempt == 2:
                raise
            delay = 2 ** attempt
            logger.warning(
                "Slack API error on attempt %d: %s. Retrying in %ds.",
                attempt + 1,
                e.response["error"],
                delay,
            )
            time.sleep(delay)
