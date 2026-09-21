"""Any alert source to the shared alert schema (`common.schema.NormalizedAlert`).

Deterministic Python owns service, severity and status here; the model only
restates them later.
"""
import logging
import re

from common.schema import NormalizedAlert

logger = logging.getLogger()

_SEVERITY_KEYWORDS = ["critical", "high", "medium", "low"]

_DD_PRIORITY_MAP = {"P1": "critical", "P2": "high", "P3": "medium", "P4": "low"}
_DD_ALERT_TYPE_MAP = {"error": "high", "warning": "medium", "info": "low"}
# Only a recovery closes; anything we cannot classify stays open so it is seen.
_DD_RESOLVED_TRANSITIONS = {"Recovered"}
# $ALERT_TITLE / $EVENT_TITLE carry the transition as a bracketed prefix
# ("[Triggered on {service:x}] Error rate"); the fingerprint must not.
_DD_TITLE_PREFIX = re.compile(r"^(\[[^\]]*\]\s*)+")


def normalize(event: dict) -> NormalizedAlert | None:
    """The alert an inbound event describes, or None when it is not one.

    None covers an unknown source and a payload that cannot be normalized
    (logged and discarded, never raised: a malformed alert must not poison
    the function).
    """
    source = _detect_source(event)
    if source is None:
        logger.warning("Discarding event with unknown source: %s", event.get("source"))
        return None

    try:
        if source == "cloudwatch":
            return _normalize_cloudwatch(event)
        return _normalize_datadog(event)
    except Exception:
        logger.exception("Failed to normalize %s event, discarding", source)
        return None


def _detect_source(event: dict) -> str | None:
    src = event.get("source")
    if src == "aws.cloudwatch":
        return "cloudwatch"
    if src == "datadog":
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


# A CloudFormation-named resource: <stack>-<LogicalId>-<12-13 random chars>,
# e.g. stale-ticket-bot-StaleTicketBotFunction-G8cd3Ax5XBMd or
# ai-incident-summarizer-IngestDLQ-CHgswNqI8tXR. The stack is the service.
_CFN_PHYSICAL_NAME = re.compile(r"^(?P<stack>.+)-(?P<logical>[A-Z][A-Za-z0-9]*)-(?P<suffix>[A-Za-z0-9]{12,13})$")


def _cloudwatch_service(detail: dict, alarm_name: str) -> str:
    """The alarm's first metric dimension, reduced to the stack name when it
    is a CloudFormation-generated physical name; the alarm name when there
    are no dimensions.

    Before RC1-437 the raw dimension value was the service, so the first real
    CloudWatch traffic registered `stale-ticket-bot-StaleTicketBotFunction-
    G8cd3Ax5XBMd` as a service in the dashboard's filter list. Every stack in
    this account is SAM-deployed, so the physical name's shape is the one
    stable clue to which system an alarm belongs to."""
    try:
        metrics = detail["configuration"]["metrics"]
        dims = metrics[0]["metricStat"]["metric"]["dimensions"]
        if dims:
            return service_from_resource_name(next(iter(dims.values())))
    except (KeyError, IndexError, StopIteration):
        pass
    return alarm_name


def service_from_resource_name(name: str) -> str:
    match = _CFN_PHYSICAL_NAME.match(name)
    return match.group("stack") if match else name


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
