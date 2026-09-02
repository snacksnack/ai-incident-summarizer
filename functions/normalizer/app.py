import json
import logging
import os
import re

import boto3

from common.schema import NormalizedAlert

logger = logging.getLogger()
logger.setLevel(logging.INFO)

_lambda_client = boto3.client("lambda")

_SEVERITY_KEYWORDS = ["critical", "high", "medium", "low"]

_DD_PRIORITY_MAP = {"P1": "critical", "P2": "high", "P3": "medium", "P4": "low"}
_DD_ALERT_TYPE_MAP = {"error": "high", "warning": "medium", "info": "low"}
# Only a recovery closes; anything we cannot classify stays open so it is seen.
_DD_RESOLVED_TRANSITIONS = {"Recovered"}
# $ALERT_TITLE / $EVENT_TITLE carry the transition as a bracketed prefix
# ("[Triggered on {service:x}] Error rate"); the fingerprint must not.
_DD_TITLE_PREFIX = re.compile(r"^(\[[^\]]*\]\s*)+")

# A run is an alert only once it has completed. in_progress / requested runs
# (no conclusion yet), workflow_job and push events are ignored (RC1-373: every
# deploy start used to open a HIGH incident because a missing conclusion
# defaulted to "failure"). A successful run is a *recovery*: dedup closes the
# open incident for that workflow if there is one, and drops it otherwise
# (RC1-374).
_GH_SUCCESS_CONCLUSIONS = {"success", "skipped", "neutral"}
_GH_SEVERITY_MAP = {"failure": "high", "timed_out": "high", "startup_failure": "high", "cancelled": "medium"}


def handler(event: dict, context) -> dict | None:
    source = _detect_source(event)
    if source is None:
        logger.warning("Discarding event with unknown source: %s", event.get("source"))
        return None

    try:
        if source == "cloudwatch":
            alert = _normalize_cloudwatch(event)
        elif source == "datadog":
            alert = _normalize_datadog(event)
        else:
            alert = _normalize_github(event)
    except Exception:
        logger.exception("Failed to normalize %s event, discarding", source)
        return None

    if alert is None:
        return None

    logger.info("Normalized alert: %s", json.dumps(alert.to_dict()))

    _lambda_client.invoke(
        FunctionName=os.environ["DEDUP_FUNCTION_NAME"],
        InvocationType="Event",
        Payload=json.dumps(alert.to_dict()),
    )

    return alert.to_dict()


def _detect_source(event: dict) -> str | None:
    src = event.get("source")
    if src == "aws.cloudwatch":
        return "cloudwatch"
    if src in ("datadog", "github"):
        return src
    return None


def _normalize_cloudwatch(event: dict) -> NormalizedAlert:
    detail = event["detail"]
    alarm_name = detail["alarmName"]
    state_value = detail["state"]["value"]

    affected_service = _cloudwatch_service(detail, alarm_name)
    severity = _cloudwatch_severity(alarm_name, state_value)
    status = "resolved" if state_value == "OK" else "open"

    return NormalizedAlert(
        alert_id=event["id"],
        source="cloudwatch",
        alert_name=alarm_name,
        affected_service=affected_service,
        severity=severity,
        status=status,
        raw_payload=event,
        received_at=event["time"],
    )


def _cloudwatch_service(detail: dict, alarm_name: str) -> str:
    try:
        metrics = detail["configuration"]["metrics"]
        dims = metrics[0]["metricStat"]["metric"]["dimensions"]
        if dims:
            return next(iter(dims.values()))
    except (KeyError, IndexError, StopIteration):
        pass
    return alarm_name


def _cloudwatch_severity(alarm_name: str, state_value: str) -> str:
    lower = alarm_name.lower()
    for level in _SEVERITY_KEYWORDS:
        if level in lower:
            return level
    return "high" if state_value == "ALARM" else "low"


def _dd_tags(payload: dict) -> list[str]:
    # The webhook's $TAGS variable renders as one comma-separated string; the
    # sample payloads and older tests carry a list. Accept both.
    tags = payload.get("tags") or []
    if isinstance(tags, str):
        tags = tags.split(",")
    return [t.strip() for t in tags if t and t.strip()]


def _dd_alert_name(title: str) -> str:
    stripped = _DD_TITLE_PREFIX.sub("", title).strip()
    return stripped or title


def _normalize_datadog(envelope: dict) -> NormalizedAlert:
    payload = envelope["raw_payload"]

    affected_service = "unknown"
    for tag in _dd_tags(payload):
        if tag.startswith("service:"):
            affected_service = tag.split(":", 1)[1]
            break

    priority = payload.get("priority", "")
    severity = _DD_PRIORITY_MAP.get(priority) or _DD_ALERT_TYPE_MAP.get(
        payload.get("alert_type", ""), "medium"
    )

    transition = payload.get("alert_transition", "")
    status = "resolved" if transition in _DD_RESOLVED_TRANSITIONS else "open"

    monitor_id = payload.get("alert_id")

    return NormalizedAlert(
        alert_id=str(payload["id"]),
        source="datadog",
        alert_name=_dd_alert_name(payload["title"]),
        affected_service=affected_service,
        severity=severity,
        status=status,
        raw_payload=payload,
        received_at=envelope["received_at"],
        monitor_id=str(monitor_id) if monitor_id not in (None, "") else None,
    )


def _normalize_github(envelope: dict) -> NormalizedAlert | None:
    payload = envelope["raw_payload"]
    event_name = envelope.get("github_event") or ("workflow_run" if "workflow_run" in payload else "unknown")
    action = payload.get("action")
    if event_name != "workflow_run" or action != "completed":
        logger.info("Ignoring github %s.%s event: only completed workflow runs are alerts", event_name, action)
        return None

    run = payload["workflow_run"]
    conclusion = run.get("conclusion")
    if not conclusion:
        logger.warning("Ignoring completed github workflow run %r with no conclusion", run.get("name"))
        return None

    if conclusion in _GH_SUCCESS_CONCLUSIONS:
        severity, status = "low", "resolved"
    else:
        # failure, timed_out, cancelled, startup_failure, action_required, stale, …
        severity, status = _GH_SEVERITY_MAP.get(conclusion, "medium"), "open"

    return NormalizedAlert(
        alert_id=str(run["id"]),
        source="github",
        alert_name=run["name"],
        affected_service=payload["repository"]["full_name"],
        severity=severity,
        status=status,
        raw_payload=payload,
        received_at=envelope["received_at"],
    )
