import json
import logging
import os

import boto3
import requests

logger = logging.getLogger()
logger.setLevel(logging.INFO)

_secrets_client = boto3.client("secretsmanager")
_dynamodb = boto3.resource("dynamodb")
_incident_table = None
_api_key_cache: dict[str, str] = {}

# Datadog's v1 events endpoint caps `text` at 4000 characters.
_TEXT_LIMIT = 4000

_ALERT_TYPE = {
    "critical": "error",
    "high": "error",
    "medium": "warning",
    "low": "info",
}

_PRIORITY = {
    "critical": "normal",
    "high": "normal",
    "medium": "low",
    "low": "low",
}


def _get_incident_table():
    global _incident_table
    if _incident_table is None:
        _incident_table = _dynamodb.Table(os.environ["INCIDENT_TABLE_NAME"])
    return _incident_table


def _get_api_key() -> str:
    # The same key the Datadog Lambda extension already reads for traces and logs —
    # Datadog API keys carry no scopes, so there is no narrower key to mint.
    arn = os.environ["DD_API_KEY_SECRET_ARN"]
    if arn not in _api_key_cache:
        _api_key_cache[arn] = _secrets_client.get_secret_value(SecretId=arn)["SecretString"]
    return _api_key_cache[arn]


def _events_url() -> str:
    site = os.environ.get("DD_SITE", "datadoghq.com")
    return f"https://api.{site}/api/v1/events"


def _parsed_summary(incident: dict) -> dict | None:
    llm_summary = incident.get("llm_summary")
    if not llm_summary:
        return None
    try:
        parsed = json.loads(llm_summary)
        return {k: parsed[k] for k in ("summary", "likely_cause", "next_step")}
    except (json.JSONDecodeError, KeyError, TypeError):
        return None


def _links(incident: dict) -> list[tuple[str, str]]:
    links = []
    slack_thread_id = incident.get("slack_thread_id")
    if slack_thread_id:
        channel = os.environ.get("SLACK_CHANNEL_ID", "")
        links.append((
            "Slack thread",
            f"https://slack.com/app_redirect?channel={channel}&message_ts={slack_thread_id}",
        ))
    jira_ticket_id = incident.get("jira_ticket_id")
    jira_base_url = os.environ.get("JIRA_BASE_URL", "").rstrip("/")
    if jira_ticket_id and jira_base_url:
        links.append((f"Jira {jira_ticket_id}", f"{jira_base_url}/browse/{jira_ticket_id}"))
    dashboard_url = os.environ.get("INCIDENT_DASHBOARD_URL", "").rstrip("/")
    if dashboard_url:
        links.append(("Incident dashboard", f"{dashboard_url}/incidents/{incident['incident_id']}"))
    return links


def _build_text(incident: dict) -> str:
    parts = []
    parsed = _parsed_summary(incident)
    if parsed:
        parts.append(
            f"**Summary:** {parsed['summary']}\n\n"
            f"**Likely cause:** {parsed['likely_cause']}\n\n"
            f"**Next step:** {parsed['next_step']}"
        )
    else:
        parts.append("_No LLM summary available._")

    alerts = incident.get("source_alerts", [])
    if alerts:
        parts.append("**Alerts:**\n" + "\n".join(f"- {a['alert_name']} ({a['source']})" for a in alerts))

    links = _links(incident)
    if links:
        parts.append(" · ".join(f"[{label}]({url})" for label, url in links))

    # The %%% fence tells Datadog to render the body as markdown. Keep the fence
    # and the links intact when the summary alone would overflow the limit.
    body = "\n\n".join(parts)
    frame = "%%% \n{}\n %%%"
    overflow = len(frame.format(body)) - _TEXT_LIMIT
    if overflow > 0:
        parts[0] = parts[0][: max(0, len(parts[0]) - overflow - 1)] + "…"
        body = "\n\n".join(parts)
    return frame.format(body)


def _build_tags(incident: dict) -> list[str]:
    severity = incident.get("severity", "").lower()
    tags = [
        f"incident_id:{incident['incident_id']}",
        f"service:{incident['affected_service']}",
        f"severity:{severity}",
        "source:incident-summarizer",
        "kind:incident-summary",
    ]
    env = os.environ.get("DD_ENV")
    if env:
        tags.append(f"env:{env}")
    if incident.get("jira_ticket_id"):
        tags.append(f"jira_ticket:{incident['jira_ticket_id']}")
    # For Datadog-sourced alerts: alert_id is the monitor *event* that opened
    # this incident (a row in the same stream this summary lands in), and
    # monitor_id is the monitor itself, stable across trigger and recovery.
    for alert in incident.get("source_alerts", []):
        if alert.get("source") != "datadog":
            continue
        if alert.get("alert_id"):
            tags.append(f"alert_id:{alert['alert_id']}")
        if alert.get("monitor_id"):
            tags.append(f"monitor_id:{alert['monitor_id']}")
    return tags


def _build_event(incident: dict) -> dict:
    severity = incident.get("severity", "").lower()
    return {
        "title": f"[{severity.upper()}] {incident['affected_service']} — incident {incident['incident_id']}",
        "text": _build_text(incident),
        "alert_type": _ALERT_TYPE.get(severity, "info"),
        "priority": _PRIORITY.get(severity, "low"),
        # One incident, one row in the timeline: every re-summary of the same
        # incident rolls up under it instead of posting a look-alike.
        "aggregation_key": f"incident:{incident['incident_id']}",
        "tags": _build_tags(incident),
    }


def _post_event(event: dict) -> str:
    response = requests.post(
        _events_url(),
        json=event,
        headers={"DD-API-KEY": _get_api_key(), "Content-Type": "application/json"},
        timeout=10,
    )
    response.raise_for_status()
    return str(response.json()["event"]["id"])


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

    event_id = _post_event(_build_event(incident))
    table.update_item(
        Key={"incident_id": incident_id},
        UpdateExpression="SET datadog_event_id = :e",
        ExpressionAttributeValues={":e": event_id},
    )
    logger.info("Datadog event %s posted for incident %s", event_id, incident_id)
    return {"incident_id": incident_id, "datadog_event_id": event_id}
