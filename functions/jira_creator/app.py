import json
import logging
import os

import boto3
import requests
from requests.auth import HTTPBasicAuth

logger = logging.getLogger()
logger.setLevel(logging.INFO)

_secrets_client = boto3.client("secretsmanager")
_dynamodb = boto3.resource("dynamodb")
_incident_table = None
_token_cache: dict[str, str] = {}

_PRIORITY_MAP = {
    "critical": "Highest",
    "high": "High",
    "medium": "Medium",
    "low": "Low",
}


def _get_incident_table():
    global _incident_table
    if _incident_table is None:
        _incident_table = _dynamodb.Table(os.environ["INCIDENT_TABLE_NAME"])
    return _incident_table


def _get_api_token() -> str:
    arn = os.environ["JIRA_API_TOKEN_SECRET_ARN"]
    if arn not in _token_cache:
        _token_cache[arn] = _secrets_client.get_secret_value(SecretId=arn)["SecretString"]
    return _token_cache[arn]


def _build_description(incident: dict) -> dict:
    paragraphs = []

    llm_summary = incident.get("llm_summary")
    if llm_summary:
        try:
            parsed = json.loads(llm_summary)
            for label, key in [("Summary", "summary"), ("Likely cause", "likely_cause"), ("Next step", "next_step")]:
                paragraphs.append({
                    "type": "paragraph",
                    "content": [{"type": "text", "text": f"{label}: {parsed[key]}"}],
                })
        except (json.JSONDecodeError, KeyError):
            pass

    alerts = incident.get("source_alerts", [])
    if alerts:
        bullet_items = [
            {
                "type": "listItem",
                "content": [{"type": "paragraph", "content": [
                    {"type": "text", "text": f"{a['alert_name']} ({a['source']})"}
                ]}],
            }
            for a in alerts
        ]
        paragraphs.append({"type": "paragraph", "content": [{"type": "text", "text": "Alerts:"}]})
        paragraphs.append({"type": "bulletList", "content": bullet_items})

    slack_thread_id = incident.get("slack_thread_id")
    if slack_thread_id:
        channel = os.environ.get("SLACK_CHANNEL_ID", "")
        slack_url = f"https://slack.com/app_redirect?channel={channel}&message_ts={slack_thread_id}"
        paragraphs.append({
            "type": "paragraph",
            "content": [
                {"type": "text", "text": "Slack thread: "},
                {"type": "text", "text": slack_url, "marks": [{"type": "link", "attrs": {"href": slack_url}}]},
            ],
        })

    return {"version": 1, "type": "doc", "content": paragraphs}


def _create_jira_ticket(incident: dict) -> str:
    base_url = os.environ["JIRA_BASE_URL"].rstrip("/")
    project_key = os.environ["JIRA_PROJECT_KEY"]
    user_email = os.environ["JIRA_USER_EMAIL"]

    severity = incident.get("severity", "").lower()
    priority = _PRIORITY_MAP.get(severity, "Medium")
    service = incident["affected_service"]
    incident_id = incident["incident_id"]

    payload = {
        "fields": {
            "project": {"key": project_key},
            "summary": f"[{severity.upper()}] {service} — {incident_id}",
            "description": _build_description(incident),
            "issuetype": {"name": "Bug"},
            "priority": {"name": priority},
        }
    }

    response = requests.post(
        f"{base_url}/rest/api/3/issue",
        json=payload,
        auth=HTTPBasicAuth(user_email, _get_api_token()),
        headers={"Accept": "application/json"},
        timeout=10,
    )
    response.raise_for_status()
    return response.json()["key"]


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

    if incident.get("jira_ticket_id"):
        logger.info("Incident %s already has jira_ticket_id %s, skipping", incident_id, incident["jira_ticket_id"])
        return {"incident_id": incident_id, "jira_ticket_id": incident["jira_ticket_id"]}

    ticket_key = _create_jira_ticket(incident)
    table.update_item(
        Key={"incident_id": incident_id},
        UpdateExpression="SET jira_ticket_id = :k",
        ExpressionAttributeValues={":k": ticket_key},
    )
    logger.info("Jira ticket %s created for incident %s", ticket_key, incident_id)
    return {"incident_id": incident_id, "jira_ticket_id": ticket_key}
