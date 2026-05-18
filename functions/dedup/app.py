import json
import logging
import os
import time
import uuid
from datetime import datetime, timezone

import boto3
from boto3.dynamodb.conditions import Attr
from botocore.exceptions import ClientError

from common.fingerprint import generate_fingerprint

logger = logging.getLogger()
logger.setLevel(logging.INFO)

_dynamodb = boto3.resource("dynamodb")
_table = None
_window_table = None
_incident_table = None


def _get_table():
    global _table
    if _table is None:
        _table = _dynamodb.Table(os.environ["DEDUP_TABLE_NAME"])
    return _table


def _get_window_table():
    global _window_table
    if _window_table is None:
        _window_table = _dynamodb.Table(os.environ["CORRELATION_TABLE_NAME"])
    return _window_table


def _get_incident_table():
    global _incident_table
    if _incident_table is None:
        _incident_table = _dynamodb.Table(os.environ["INCIDENT_TABLE_NAME"])
    return _incident_table


def _alert_summary(event: dict) -> dict:
    return {
        "alert_id": event["alert_id"],
        "source": event["source"],
        "alert_name": event["alert_name"],
        "severity": event["severity"],
        "status": event["status"],
        "received_at": event["received_at"],
    }


def _group_into_window(event: dict, window_seconds: int) -> dict:
    service_key = event["affected_service"]
    now = int(time.time())
    ttl = now + window_seconds
    incident_id = str(uuid.uuid4())
    summary = _alert_summary(event)

    try:
        _get_window_table().put_item(
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
    response = _get_window_table().update_item(
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


def _persist_incident(event: dict, grouping: dict) -> None:
    incident_id = grouping["incident_id"]
    summary = _alert_summary(event)
    now_iso = datetime.now(timezone.utc).isoformat()

    if grouping["is_new"]:
        try:
            _get_incident_table().put_item(
                Item={
                    "incident_id": incident_id,
                    "affected_service": event["affected_service"],
                    "severity": event["severity"],
                    "status": "open",
                    "source_alerts": [summary],
                    "created_at": now_iso,
                },
                ConditionExpression="attribute_not_exists(incident_id)",
            )
            logger.info("Persisted new incident: incident_id=%s", incident_id)
        except ClientError as e:
            if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
                logger.info("Incident %s already exists, skipping duplicate write", incident_id)
            else:
                raise
    else:
        _get_incident_table().update_item(
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


def handler(event: dict, context) -> dict | None:
    fingerprint = generate_fingerprint(
        source=event["source"],
        alert_name=event["alert_name"],
        affected_service=event["affected_service"],
    )

    window_seconds = int(os.environ.get("CORRELATION_WINDOW_MINUTES", "5")) * 60
    ttl = int(time.time()) + window_seconds

    try:
        _get_table().put_item(
            Item={
                "fingerprint": fingerprint,
                "first_seen_at": datetime.now(timezone.utc).isoformat(),
                "source": event["source"],
                "alert_name": event["alert_name"],
                "ttl": ttl,
            },
            ConditionExpression="attribute_not_exists(fingerprint)",
        )
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            logger.warning(
                "Suppressing duplicate alert: fingerprint=%s source=%s alert_name=%s affected_service=%s",
                fingerprint,
                event["source"],
                event["alert_name"],
                event["affected_service"],
            )
            return None
        raise

    logger.info(
        "New alert accepted: fingerprint=%s source=%s alert_name=%s",
        fingerprint,
        event["source"],
        event["alert_name"],
    )

    grouping = _group_into_window(event, window_seconds)
    _persist_incident(event, grouping)
    return {"incident_id": grouping["incident_id"], "is_new": grouping["is_new"], "alert_count": grouping["alert_count"], "alert": event}
