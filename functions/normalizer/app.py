import json
import logging

from common.schema import NormalizedAlert

logger = logging.getLogger()
logger.setLevel(logging.INFO)

_SEVERITY_KEYWORDS = ["critical", "high", "medium", "low"]

_DD_PRIORITY_MAP = {"P1": "critical", "P2": "high", "P3": "medium", "P4": "low"}
_DD_ALERT_TYPE_MAP = {"error": "high", "warning": "medium", "info": "low"}
_DD_OPEN_TRANSITIONS = {"Triggered", "Re-Triggered"}

_GH_OPEN_CONCLUSIONS = {"failure", "cancelled", "timed_out", "startup_failure"}
_GH_SEVERITY_MAP = {"failure": "high", "timed_out": "high", "cancelled": "medium"}


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

    logger.info("Normalized alert: %s", json.dumps(alert.to_dict()))
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


def _normalize_datadog(envelope: dict) -> NormalizedAlert:
    payload = envelope["raw_payload"]

    affected_service = "unknown"
    for tag in payload.get("tags", []):
        if tag.startswith("service:"):
            affected_service = tag.split(":", 1)[1]
            break

    priority = payload.get("priority", "")
    severity = _DD_PRIORITY_MAP.get(priority) or _DD_ALERT_TYPE_MAP.get(
        payload.get("alert_type", ""), "medium"
    )

    transition = payload.get("alert_transition", "")
    status = "open" if transition in _DD_OPEN_TRANSITIONS else "resolved"

    return NormalizedAlert(
        alert_id=str(payload["id"]),
        source="datadog",
        alert_name=payload["title"],
        affected_service=affected_service,
        severity=severity,
        status=status,
        raw_payload=payload,
        received_at=envelope["received_at"],
    )


def _normalize_github(envelope: dict) -> NormalizedAlert:
    payload = envelope["raw_payload"]
    run = payload["workflow_run"]
    conclusion = run.get("conclusion") or "failure"

    severity = _GH_SEVERITY_MAP.get(conclusion, "low")
    status = "open" if conclusion in _GH_OPEN_CONCLUSIONS else "resolved"

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
