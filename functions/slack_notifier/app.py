import json
import logging
import os
import time

import boto3
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

logger = logging.getLogger()
logger.setLevel(logging.INFO)

_secrets_client = boto3.client("secretsmanager")
_dynamodb = boto3.resource("dynamodb")
_incident_table = None
_token_cache: dict[str, str] = {}

_SEVERITY_EMOJI = {
    "critical": "🔴",
    "high": "🟠",
    "medium": "🟡",
    "low": "🟢",
}


def _get_incident_table():
    global _incident_table
    if _incident_table is None:
        _incident_table = _dynamodb.Table(os.environ["INCIDENT_TABLE_NAME"])
    return _incident_table


def _get_token() -> str:
    arn = os.environ["SLACK_BOT_TOKEN_SECRET_ARN"]
    if arn not in _token_cache:
        _token_cache[arn] = _secrets_client.get_secret_value(SecretId=arn)["SecretString"]
    return _token_cache[arn]


def _build_message(incident: dict) -> str:
    severity = incident.get("severity", "").lower()
    emoji = _SEVERITY_EMOJI.get(severity, "")
    badge = f"{emoji} *{severity.upper()}*" if emoji else f"*{severity.upper()}*"
    service = incident["affected_service"]
    created_at = incident.get("created_at", "unknown")

    header = f"{badge} | {service} | {created_at}"

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


def handler(event: dict, context) -> dict | None:
    incident_id = event.get("incident_id")
    if not incident_id:
        logger.error("No incident_id in event")
        return None

    table = _get_incident_table()
    response = table.get_item(Key={"incident_id": incident_id})
    incident = response.get("Item")
    if not incident:
        logger.warning("Incident %s not found", incident_id)
        return None

    channel = os.environ["SLACK_CHANNEL_ID"]
    client = WebClient(token=_get_token())
    text = _build_message(incident)
    slack_thread_id = incident.get("slack_thread_id")

    if slack_thread_id:
        _post_with_retry(client, channel=channel, text=text, thread_ts=slack_thread_id)
        logger.info("Posted reply to thread %s for incident %s", slack_thread_id, incident_id)
        return {"incident_id": incident_id, "slack_thread_id": slack_thread_id}

    result = _post_with_retry(client, channel=channel, text=text)
    thread_ts = result["ts"]
    table.update_item(
        Key={"incident_id": incident_id},
        UpdateExpression="SET slack_thread_id = :ts",
        ExpressionAttributeValues={":ts": thread_ts},
    )
    logger.info("Opened new thread %s for incident %s", thread_ts, incident_id)
    return {"incident_id": incident_id, "slack_thread_id": thread_ts}
