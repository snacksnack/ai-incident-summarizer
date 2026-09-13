"""Jira: one ticket per incident, created once; the recovery comments on it and
moves it to a Done-category status when the workflow offers one."""
import json
import logging
import os

import requests
from requests.auth import HTTPBasicAuth

from common import aws
from common import recurrence

logger = logging.getLogger()

_PRIORITY_MAP = {
    "critical": "Highest",
    "high": "High",
    "medium": "Medium",
    "low": "Low",
}


def deliver(incident: dict, recovered: bool = False) -> str | None:
    """Create the ticket, or on recovery close it; return its key (None if none)."""
    incident_id = incident["incident_id"]

    if recovered:
        if incident.get("jira_ticket_id"):
            _close_jira_ticket(incident)
        else:
            logger.info("Incident %s resolved with no Jira ticket to close", incident_id)
        return incident.get("jira_ticket_id")

    if incident.get("jira_ticket_id"):
        logger.info("Incident %s already has jira_ticket_id %s, skipping", incident_id, incident["jira_ticket_id"])
        return incident["jira_ticket_id"]

    ticket_key = _create_jira_ticket(incident)
    aws.table("INCIDENT_TABLE_NAME").update_item(
        Key={"incident_id": incident_id},
        UpdateExpression="SET jira_ticket_id = :k",
        ExpressionAttributeValues={":k": ticket_key},
    )
    incident["jira_ticket_id"] = ticket_key
    logger.info("Jira ticket %s created for incident %s", ticket_key, incident_id)
    return ticket_key


def _get_api_token() -> str:
    return _token_from_secret(aws.secret(os.environ["JIRA_API_TOKEN_SECRET_ARN"]))


def _token_from_secret(secret_string: str) -> str:
    # The secret is shared with the stale-ticket bot, whose contract is
    # {"email": ..., "api_token": ...}. Sending that JSON as the password made
    # Jira treat every create as anonymous (400 "project doesn't exist") — RC1-371.
    # A bare token still works, so a secret of either shape is accepted.
    stripped = secret_string.strip()
    if stripped.startswith("{"):
        return json.loads(stripped)["api_token"]
    return stripped


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

    recurring = recurrence.sentence(incident)
    if recurring:
        content = [{"type": "text", "text": f"Recurring: {recurring}", "marks": [{"type": "strong"}]}]
        previous = recurrence.previous_ticket(incident)
        if previous:
            base_url = os.environ.get("JIRA_BASE_URL", "").rstrip("/")
            url = f"{base_url}/browse/{previous}"
            content.append({"type": "text", "text": " Previous ticket: "})
            content.append({"type": "text", "text": previous, "marks": [{"type": "link", "attrs": {"href": url}}]})
        paragraphs.append({"type": "paragraph", "content": content})

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

    title = f"[{severity.upper()}] {service} — {incident_id}"
    repeat = recurrence.badge(incident)
    if repeat:
        title += f" (recurring: {repeat})"
    payload = {
        "fields": {
            "project": {"key": project_key},
            "summary": title,
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
    if not response.ok:
        logger.error("Jira rejected issue creation (%s): %s", response.status_code, response.text[:500])
    response.raise_for_status()
    return response.json()["key"]


def _recovery_comment(incident: dict) -> dict:
    paragraphs = [{"type": "paragraph", "content": [{"type": "text", "text": "Incident resolved.", "marks": [{"type": "strong"}]}]}]
    try:
        parsed = json.loads(incident.get("recovery_summary") or "")
        for label, key in [("Summary", "summary"), ("Likely cause", "likely_cause"), ("Next step", "next_step")]:
            paragraphs.append({"type": "paragraph", "content": [{"type": "text", "text": f"{label}: {parsed[key]}"}]})
    except (json.JSONDecodeError, KeyError, TypeError):
        pass
    if incident.get("resolved_at"):
        paragraphs.append({"type": "paragraph", "content": [{"type": "text", "text": f"Resolved at: {incident['resolved_at']}"}]})
    return {"version": 1, "type": "doc", "content": paragraphs}


def _close_jira_ticket(incident: dict) -> None:
    """Comment the recovery on the ticket and move it to a Done-category status
    if the project's workflow offers one from here. Both best effort: a Jira
    hiccup must not stop the Datadog event that follows."""
    base_url = os.environ["JIRA_BASE_URL"].rstrip("/")
    auth = HTTPBasicAuth(os.environ["JIRA_USER_EMAIL"], _get_api_token())
    key = incident["jira_ticket_id"]
    headers = {"Accept": "application/json"}

    resp = requests.post(f"{base_url}/rest/api/3/issue/{key}/comment", json={"body": _recovery_comment(incident)}, auth=auth, headers=headers, timeout=10)
    if not resp.ok:
        logger.error("Jira rejected the recovery comment on %s (%s): %s", key, resp.status_code, resp.text[:300])
    else:
        logger.info("Recovery comment posted on %s", key)

    resp = requests.get(f"{base_url}/rest/api/3/issue/{key}/transitions", auth=auth, headers=headers, timeout=10)
    if not resp.ok:
        logger.error("Could not list transitions for %s (%s)", key, resp.status_code)
        return
    done = next((t for t in resp.json().get("transitions", []) if (t.get("to") or {}).get("statusCategory", {}).get("key") == "done"), None)
    if done is None:
        logger.info("No Done-category transition available for %s; left as is", key)
        return
    resp = requests.post(f"{base_url}/rest/api/3/issue/{key}/transitions", json={"transition": {"id": done["id"]}}, auth=auth, headers=headers, timeout=10)
    if resp.ok:
        logger.info("Transitioned %s to %s", key, done.get("name"))
    else:
        logger.error("Jira rejected transition %s on %s (%s): %s", done.get("name"), key, resp.status_code, resp.text[:300])
