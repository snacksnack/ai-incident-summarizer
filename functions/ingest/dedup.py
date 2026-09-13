"""Fingerprint suppression, time-window grouping into incidents, and the
recovery path that closes them.

State is four DynamoDB tables, all reached through `common.aws`:
the deduplication table (PK fingerprint, TTL), the correlation window table
(PK service_key, TTL), the incident table and the service registry.
"""
import logging
import os
import time
import uuid
from datetime import datetime, timezone

from boto3.dynamodb.conditions import Attr, Key
from botocore.exceptions import ClientError

from common import aws
from common.fingerprint import generate_fingerprint

logger = logging.getLogger()


def _dedup_table():
    return aws.table("DEDUP_TABLE_NAME")


def _window_table():
    return aws.table("CORRELATION_TABLE_NAME")


def _incident_table():
    return aws.table("INCIDENT_TABLE_NAME")


def _service_registry_table():
    return aws.table("SERVICE_REGISTRY_TABLE_NAME")


def process(alert: dict) -> dict | None:
    """Suppress, group or close; return what happened, or None when nothing did.

    A duplicate inside the window returns None. An open alert returns the
    incident it opened or joined (`is_new`, `alert_count`). A resolved alert
    returns the incident it closed with `resolved: True`, or None when there
    was nothing to close. The caller decides what to summarize from that.
    """
    fingerprint = generate_fingerprint(
        source=alert["source"],
        alert_name=alert["alert_name"],
        affected_service=alert["affected_service"],
    )

    if alert.get("status") == "resolved":
        return _handle_recovery(alert, fingerprint)

    window_seconds = int(os.environ.get("CORRELATION_WINDOW_MINUTES", "5")) * 60
    now = int(time.time())
    ttl = now + window_seconds

    try:
        _dedup_table().put_item(
            Item={
                "fingerprint": fingerprint,
                "first_seen_at": datetime.now(timezone.utc).isoformat(),
                "source": alert["source"],
                "alert_name": alert["alert_name"],
                "ttl": ttl,
            },
            # An expired row that DynamoDB's TTL sweep has not yet removed (it
            # promises deletion within ~48 h, not at expiry) must not count as a
            # duplicate, or the 5-minute window silently becomes a 2-day one
            # (RC1-372). Same rule the correlation window already applies.
            ConditionExpression=(
                Attr("fingerprint").not_exists() | Attr("ttl").lte(now)
            ),
        )
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            logger.warning(
                "Suppressing duplicate alert: fingerprint=%s source=%s alert_name=%s affected_service=%s",
                fingerprint,
                alert["source"],
                alert["alert_name"],
                alert["affected_service"],
            )
            return None
        raise

    logger.info(
        "New alert accepted: fingerprint=%s source=%s alert_name=%s",
        fingerprint,
        alert["source"],
        alert["alert_name"],
    )

    grouping = _group_into_window(alert, window_seconds)
    _persist_incident(alert, grouping)

    return {"incident_id": grouping["incident_id"], "is_new": grouping["is_new"], "alert_count": grouping["alert_count"], "alert": alert}


def _register_service(affected_service: str, now_iso: str) -> None:
    """Record that this service has opened an incident.

    Feeds the dashboard's service filters, which would otherwise have to scan the
    incident table. The write is an idempotent upsert — safe to replay, and
    self-healing, since the next incident for the service retries it.

    Failures are logged and swallowed on purpose. The registry is derived data;
    losing an incident because a derived write failed would be a far worse
    outcome than a service missing from the filter list for one incident.
    """
    try:
        _service_registry_table().update_item(
            Key={"affected_service": affected_service},
            UpdateExpression=(
                "SET last_seen_at = :ts, "
                "first_seen_at = if_not_exists(first_seen_at, :ts)"
            ),
            ExpressionAttributeValues={":ts": now_iso},
        )
        logger.info("Registered service in registry: affected_service=%s", affected_service)
    except Exception:
        logger.warning(
            "Service registry write failed for affected_service=%s — "
            "incident is unaffected, registry self-heals on the next incident",
            affected_service,
            exc_info=True,
        )


def _alert_summary(alert: dict) -> dict:
    summary = {
        "alert_id": alert["alert_id"],
        "source": alert["source"],
        "alert_name": alert["alert_name"],
        "severity": alert["severity"],
        "status": alert["status"],
        "received_at": alert["received_at"],
    }
    if alert.get("monitor_id"):
        summary["monitor_id"] = alert["monitor_id"]
    return summary


def _group_into_window(alert: dict, window_seconds: int) -> dict:
    service_key = alert["affected_service"]
    now = int(time.time())
    ttl = now + window_seconds
    incident_id = str(uuid.uuid4())
    summary = _alert_summary(alert)

    try:
        _window_table().put_item(
            Item={
                "service_key": service_key,
                "incident_id": incident_id,
                "service": service_key,
                "first_seen_at": datetime.now(timezone.utc).isoformat(),
                "last_updated_at": datetime.now(timezone.utc).isoformat(),
                "alert_summaries": [summary],
                "alert_count": 1,
                "ttl": ttl,
            },
            ConditionExpression=(
                Attr("service_key").not_exists() | Attr("ttl").lte(now)
            ),
        )
        logger.info(
            "New incident window opened: incident_id=%s service=%s",
            incident_id,
            service_key,
        )
        return {"incident_id": incident_id, "is_new": True, "alert_count": 1}

    except ClientError as e:
        if e.response["Error"]["Code"] != "ConditionalCheckFailedException":
            raise

    # Window is still open — append to the existing incident
    response = _window_table().update_item(
        Key={"service_key": service_key},
        UpdateExpression=(
            "SET alert_summaries = list_append(alert_summaries, :s), "
            "last_updated_at = :ts, "
            "alert_count = alert_count + :one"
        ),
        ExpressionAttributeValues={
            ":s": [summary],
            ":ts": datetime.now(timezone.utc).isoformat(),
            ":one": 1,
        },
        ReturnValues="ALL_NEW",
    )
    attrs = response["Attributes"]
    logger.info(
        "Alert appended to existing incident: incident_id=%s service=%s count=%s",
        attrs["incident_id"],
        service_key,
        attrs["alert_count"],
    )
    return {
        "incident_id": attrs["incident_id"],
        "is_new": False,
        "alert_count": int(attrs["alert_count"]),
    }


def _persist_incident(alert: dict, grouping: dict) -> None:
    incident_id = grouping["incident_id"]
    summary = _alert_summary(alert)
    now_iso = datetime.now(timezone.utc).isoformat()

    if grouping["is_new"]:
        try:
            _incident_table().put_item(
                Item={
                    "incident_id": incident_id,
                    "affected_service": alert["affected_service"],
                    "severity": alert["severity"],
                    "status": "open",
                    "source_alerts": [summary],
                    "created_at": now_iso,
                },
                ConditionExpression="attribute_not_exists(incident_id)",
            )
            logger.info("Persisted new incident: incident_id=%s", incident_id)
            _register_service(alert["affected_service"], now_iso)
        except ClientError as e:
            if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
                logger.info("Incident %s already exists, skipping duplicate write", incident_id)
            else:
                raise
    else:
        _incident_table().update_item(
            Key={"incident_id": incident_id},
            UpdateExpression=(
                "SET source_alerts = list_append(source_alerts, :s), "
                "last_updated_at = :ts"
            ),
            ExpressionAttributeValues={
                ":s": [summary],
                ":ts": now_iso,
            },
        )
        logger.info("Updated incident: incident_id=%s alert_count=%s", incident_id, grouping["alert_count"])


def _find_open_incident(alert: dict) -> dict | None:
    """The newest open incident for this service carrying an alert with the same
    source and name — the one a recovery of that alert closes."""
    response = _incident_table().query(
        IndexName="service-created-index",
        KeyConditionExpression=Key("affected_service").eq(alert["affected_service"]),
        ScanIndexForward=False,
        Limit=10,
    )
    for incident in response.get("Items", []):
        if incident.get("status") != "open":
            continue
        for existing in incident.get("source_alerts", []):
            if existing.get("source") == alert["source"] and existing.get("alert_name") == alert["alert_name"]:
                return incident
    return None


def _close_incident(alert: dict, incident: dict, fingerprint: str) -> bool:
    """Append the recovery, mark the incident resolved, and retire the window
    and fingerprint rows so the next alert opens a fresh incident. False when a
    concurrent recovery already closed it."""
    incident_id = incident["incident_id"]
    now_iso = datetime.now(timezone.utc).isoformat()
    try:
        _incident_table().update_item(
            Key={"incident_id": incident_id},
            UpdateExpression=(
                "SET source_alerts = list_append(source_alerts, :s), "
                "#st = :resolved, resolved_at = :ts, last_updated_at = :ts"
            ),
            ConditionExpression=Attr("status").eq("open"),
            ExpressionAttributeNames={"#st": "status"},
            ExpressionAttributeValues={
                ":s": [_alert_summary(alert)],
                ":resolved": "resolved",
                ":ts": now_iso,
            },
        )
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return False
        raise

    try:
        _window_table().delete_item(
            Key={"service_key": alert["affected_service"]},
            ConditionExpression=Attr("incident_id").eq(incident_id),
        )
    except ClientError as e:
        if e.response["Error"]["Code"] != "ConditionalCheckFailedException":
            raise  # the window already belongs to a newer incident, or is gone
    _dedup_table().delete_item(Key={"fingerprint": fingerprint})
    return True


def _handle_recovery(alert: dict, fingerprint: str) -> dict | None:
    """A resolved alert is not an incident. It closes the open incident it
    belongs to, or it is dropped (RC1-374)."""
    incident = _find_open_incident(alert)
    if incident is None:
        logger.info(
            "Recovery with no open incident to close, dropping: source=%s alert_name=%s affected_service=%s",
            alert["source"], alert["alert_name"], alert["affected_service"],
        )
        return None
    incident_id = incident["incident_id"]
    if not _close_incident(alert, incident, fingerprint):
        logger.info("Incident %s already resolved, dropping duplicate recovery", incident_id)
        return None
    logger.info(
        "Incident resolved: incident_id=%s service=%s by %s alert %s",
        incident_id, alert["affected_service"], alert["source"], alert["alert_name"],
    )
    return {
        "incident_id": incident_id,
        "is_new": False,
        "resolved": True,
        "alert_count": len(incident.get("source_alerts", [])) + 1,
        "alert": alert,
    }
