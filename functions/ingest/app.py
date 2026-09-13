"""Ingest: every alert source in, one incident hand-off out (RC1-431).

Two triggers reach this handler. API Gateway delivers the GitHub Actions and
Datadog webhooks; EventBridge delivers CloudWatch alarm state changes as
native events. Both are authenticated or trusted at the edge, normalized to
the shared alert schema, deduplicated and grouped into an incident, and the
incident is handed to the summarizer with one asynchronous invoke.

This used to be three functions and two async hops (receiver → normalizer →
dedup). They shared no failure domain worth isolating: the normalizer
discarded on any exception and the whole path is a few DynamoDB writes, well
inside API Gateway's 29 s integration cap.
"""
import json
import logging
import os

import boto3

import dedup
import normalize
import webhook

logger = logging.getLogger()
logger.setLevel(logging.INFO)

_lambda_client = boto3.client("lambda")


def handler(event: dict, context) -> dict | None:
    if webhook.is_http_event(event):
        try:
            envelope = webhook.parse(event)
        except webhook.Rejected as rejected:
            return rejected.response()
        return _accept(envelope)

    if event.get("source") == "aws.cloudwatch":
        return _ingest(event)

    logger.warning("Discarding event with unknown source: %s", event.get("source"))
    return None


def _accept(envelope: dict) -> dict:
    """The 202 an authenticated webhook gets, whatever became of its alert."""
    outcome = _ingest(envelope)
    body = {"status": "accepted"}
    if outcome:
        body["incident_id"] = outcome["incident_id"]
    logger.info("Accepted %s webhook", envelope["source"])
    return webhook.response(202, body)


def _ingest(event: dict) -> dict | None:
    alert = normalize.normalize(event)
    if alert is None:
        return None
    logger.info("Normalized alert: %s", json.dumps(alert.to_dict()))
    return process_alert(alert.to_dict())


def process_alert(alert: dict) -> dict | None:
    """Dedup and group a normalized alert; hand any touched incident to the summarizer."""
    outcome = dedup.process(alert)
    if outcome is None:
        return None
    payload = {"incident_id": outcome["incident_id"]}
    if outcome.get("resolved"):
        # The flag rides the whole delivery chain; absent means the usual
        # open-incident rendering.
        payload["recovered"] = True
    _lambda_client.invoke(
        FunctionName=os.environ["SUMMARIZER_FUNCTION_NAME"],
        InvocationType="Event",
        Payload=json.dumps(payload),
    )
    return outcome
