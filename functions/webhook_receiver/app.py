import base64
import hashlib
import hmac
import json
import logging
import os
from datetime import datetime, timezone

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

_lambda_client = boto3.client("lambda")
_secrets_client = boto3.client("secretsmanager")
_secret_cache: dict[str, str] = {}

GITHUB_PATH = "/webhook/github"
DATADOG_PATH = "/webhook/datadog"


def handler(event: dict, context) -> dict:
    path = event.get("rawPath", "")
    body_raw, body_bytes = _extract_body(event)

    if path == GITHUB_PATH:
        secret = _get_secret(os.environ["GITHUB_WEBHOOK_SECRET_ARN"])
        sig_header = event.get("headers", {}).get("x-hub-signature-256", "")
        if not _verify_github(sig_header, body_bytes, secret):
            return _response(401, {"error": "Unauthorized"})
    elif path == DATADOG_PATH:
        secret = _get_secret(os.environ["DATADOG_WEBHOOK_SECRET_ARN"])
        sig_header = event.get("headers", {}).get("x-datadog-signature", "")
        if not _verify_datadog(sig_header, body_bytes, secret):
            return _response(401, {"error": "Unauthorized"})
    else:
        return _response(404, {"error": "Not Found"})

    try:
        raw_payload = json.loads(body_raw)
    except (ValueError, TypeError):
        return _response(400, {"error": "Bad Request"})

    source = "github" if path == GITHUB_PATH else "datadog"
    envelope = {
        "source": source,
        "raw_payload": raw_payload,
        "received_at": datetime.now(timezone.utc).isoformat(),
        "path": path,
    }

    _lambda_client.invoke(
        FunctionName=os.environ["NORMALIZER_FUNCTION_NAME"],
        InvocationType="Event",
        Payload=json.dumps(envelope),
    )

    logger.info("Accepted %s webhook and forwarded to normalizer", source)
    return _response(202, {"status": "accepted"})


def _extract_body(event: dict) -> tuple[str, bytes]:
    body = event.get("body") or ""
    if event.get("isBase64Encoded"):
        body_bytes = base64.b64decode(body)
        body_str = body_bytes.decode("utf-8")
    else:
        body_str = body
        body_bytes = body_str.encode("utf-8")
    return body_str, body_bytes


def _verify_github(header_value: str, body_bytes: bytes, secret: str) -> bool:
    if not header_value.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(
        secret.encode("utf-8"), body_bytes, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, header_value)


def _verify_datadog(header_value: str, body_bytes: bytes, secret: str) -> bool:
    if not header_value:
        return False
    expected = hmac.new(
        secret.encode("utf-8"), body_bytes, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, header_value)


def _get_secret(arn: str) -> str:
    if arn not in _secret_cache:
        response = _secrets_client.get_secret_value(SecretId=arn)
        _secret_cache[arn] = response["SecretString"]
    return _secret_cache[arn]


def _response(status_code: int, body: dict) -> dict:
    return {
        "statusCode": status_code,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body),
    }
