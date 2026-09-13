"""Authentication of the inbound webhooks (GitHub Actions, Datadog) and the
envelope the normalizer reads from them.

GitHub signs the body with HMAC-SHA256 (`X-Hub-Signature-256`). Datadog's
webhook integration cannot sign, so a shared secret travels in a custom
`X-Webhook-Secret` header and is compared timing-safely.
"""
import base64
import hashlib
import hmac
import json
import logging
import os
from datetime import datetime, timezone

from common import aws

logger = logging.getLogger()

GITHUB_PATH = "/webhook/github"
DATADOG_PATH = "/webhook/datadog"


class Rejected(Exception):
    """A webhook that must not be processed, with the HTTP response to return."""

    def __init__(self, status_code: int, error: str):
        super().__init__(error)
        self.status_code = status_code
        self.error = error

    def response(self) -> dict:
        return response(self.status_code, {"error": self.error})


def is_http_event(event: dict) -> bool:
    """True for an API Gateway HTTP API event, as opposed to an EventBridge one."""
    return "rawPath" in event or "routeKey" in event


def parse(event: dict) -> dict:
    """Authenticate an API Gateway event and return the source envelope.

    Raises `Rejected` with the status to return: 401 on a bad signature or
    secret, 404 off the two routes, 400 when the body is not JSON.
    """
    path = route_path(event)
    body_raw, body_bytes = extract_body(event)
    headers = event.get("headers") or {}

    if path == GITHUB_PATH:
        secret = shared_secret(os.environ["GITHUB_WEBHOOK_SECRET_ARN"])
        if not verify_github(headers.get("x-hub-signature-256", ""), body_bytes, secret):
            raise Rejected(401, "Unauthorized")
    elif path == DATADOG_PATH:
        secret = shared_secret(os.environ["DATADOG_WEBHOOK_SECRET_ARN"])
        if not verify_datadog_header(headers.get("x-webhook-secret", ""), secret):
            raise Rejected(401, "Unauthorized")
    else:
        logger.warning("Rejected webhook for unknown path %r (rawPath=%r)", path, event.get("rawPath"))
        raise Rejected(404, "Not Found")

    try:
        raw_payload = json.loads(body_raw)
    except (ValueError, TypeError):
        raise Rejected(400, "Bad Request")

    source = "github" if path == GITHUB_PATH else "datadog"
    envelope = {
        "source": source,
        "raw_payload": raw_payload,
        "received_at": datetime.now(timezone.utc).isoformat(),
        "path": path,
    }
    if source == "github":
        # GitHub names the event only in this header; the body of a
        # workflow_job or push event has no workflow_run key to tell them apart.
        envelope["github_event"] = headers.get("x-github-event")
    return envelope


def route_path(event: dict) -> str:
    """The path as the routes declare it, independent of the API stage.

    With a named stage (`prod`) API Gateway hands the Lambda
    `rawPath="/prod/webhook/datadog"`, so matching on rawPath rejected every
    real request with a silent 404 until RC1-370. `routeKey` ("POST
    /webhook/datadog") never carries the stage; rawPath minus the stage is the
    fallback for events that lack it (local invokes, older test fixtures).
    """
    route_key = event.get("routeKey") or ""
    if " " in route_key:
        return route_key.split(" ", 1)[1]
    path = event.get("rawPath", "")
    stage = (event.get("requestContext") or {}).get("stage")
    if stage and stage != "$default" and path.startswith(f"/{stage}/"):
        return path[len(stage) + 1:]
    return path


def extract_body(event: dict) -> tuple[str, bytes]:
    body = event.get("body") or ""
    if event.get("isBase64Encoded"):
        body_bytes = base64.b64decode(body)
        body_str = body_bytes.decode("utf-8")
    else:
        body_str = body
        body_bytes = body_str.encode("utf-8")
    return body_str, body_bytes


def verify_github(header_value: str, body_bytes: bytes, secret: str) -> bool:
    if not header_value.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(
        secret.encode("utf-8"), body_bytes, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, header_value)


def verify_datadog_header(header_value: str, secret: str) -> bool:
    if not header_value:
        return False
    return hmac.compare_digest(secret, header_value)


def shared_secret(arn: str) -> str:
    return secret_value(aws.secret(arn))


def secret_value(secret_string: str) -> str:
    """The shared secret itself, whichever way Secrets Manager holds it.

    Both webhook secrets were created as key/value pairs, so the stored string
    is '{"gh-webhook-secret": "<hex>"}', while GitHub signs with, and Datadog
    sends, the bare <hex>. Comparing against the JSON text rejected every
    real webhook with a 401 (RC1-370). A one-key JSON object yields its value;
    anything else is used as-is.
    """
    stripped = secret_string.strip()
    if stripped.startswith("{"):
        try:
            parsed = json.loads(stripped)
        except ValueError:
            return stripped
        if isinstance(parsed, dict) and len(parsed) == 1:
            return str(next(iter(parsed.values())))
    return stripped


def response(status_code: int, body: dict) -> dict:
    return {
        "statusCode": status_code,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body),
    }
