import json
import logging
import os
import time
from datetime import datetime, timezone

import boto3
from botocore.exceptions import ClientError

from common.fingerprint import generate_fingerprint

logger = logging.getLogger()
logger.setLevel(logging.INFO)

_dynamodb = boto3.resource("dynamodb")
_table = None


def _get_table():
    global _table
    if _table is None:
        _table = _dynamodb.Table(os.environ["DEDUP_TABLE_NAME"])
    return _table


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
    return event
