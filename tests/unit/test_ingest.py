"""The ingest handler: routing by event shape, and the one async hand-off to
the summarizer (RC1-431). Normalization and dedup have their own files; here
they are stubbed so the handler's own decisions are what is under test."""
import json
from unittest.mock import MagicMock, patch

import pytest

from common import aws
from tests.conftest import load_function_module

SUMMARIZER_FUNCTION = "test-summarizer"
DATADOG_SECRET = "datadog-test-secret"
DATADOG_SECRET_ARN = "arn:aws:secretsmanager:us-east-1:123456789012:secret:datadog-webhook"

ALERT = {
    "alert_id": "test-id-123",
    "source": "cloudwatch",
    "alert_name": "payments-service-error-rate",
    "affected_service": "payments-service",
    "severity": "high",
    "status": "open",
    "raw_payload": {},
    "received_at": "2024-01-15T10:30:00Z",
    "monitor_id": None,
}


def _cw_event(state="ALARM"):
    return {
        "version": "0",
        "id": "test-event-id-123",
        "source": "aws.cloudwatch",
        "time": "2024-01-15T10:30:00Z",
        "detail-type": "CloudWatch Alarm State Change",
        "detail": {
            "alarmName": "payments-service-error-rate",
            "state": {"value": state},
            "configuration": {"metrics": [{"metricStat": {"metric": {"dimensions": {"FunctionName": "payments-service"}}}}]},
        },
    }


def _dd_webhook(payload: dict) -> dict:
    return {
        "rawPath": "/webhook/datadog",
        "headers": {"x-webhook-secret": DATADOG_SECRET},
        "body": json.dumps(payload),
        "isBase64Encoded": False,
    }


@pytest.fixture()
def ingest(monkeypatch):
    monkeypatch.setenv("SUMMARIZER_FUNCTION_NAME", SUMMARIZER_FUNCTION)
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET_ARN", "arn:github")
    monkeypatch.setenv("DATADOG_WEBHOOK_SECRET_ARN", DATADOG_SECRET_ARN)
    aws.reset()
    mock_secrets = MagicMock()
    mock_secrets.get_secret_value.return_value = {"SecretString": DATADOG_SECRET}
    aws._secrets_client = mock_secrets
    with patch("boto3.client"):
        app = load_function_module("ingest")
    app._lambda_client = MagicMock()
    yield app


def _payload(app) -> dict:
    app._lambda_client.invoke.assert_called_once()
    kwargs = app._lambda_client.invoke.call_args[1]
    assert kwargs["FunctionName"] == SUMMARIZER_FUNCTION
    assert kwargs["InvocationType"] == "Event"
    return json.loads(kwargs["Payload"])


class TestHandOff:
    def test_open_alert_hands_the_incident_to_the_summarizer(self, ingest):
        with patch.object(ingest.dedup, "process", return_value={"incident_id": "inc-1", "is_new": True, "alert_count": 1, "alert": ALERT}):
            outcome = ingest.process_alert(ALERT)
        assert outcome["incident_id"] == "inc-1"
        assert _payload(ingest) == {"incident_id": "inc-1"}

    def test_recovery_hands_off_with_the_flag(self, ingest):
        with patch.object(ingest.dedup, "process", return_value={"incident_id": "inc-1", "is_new": False, "resolved": True, "alert_count": 2, "alert": ALERT}):
            ingest.process_alert(ALERT)
        assert _payload(ingest) == {"incident_id": "inc-1", "recovered": True}

    def test_suppressed_or_dropped_alert_invokes_nothing(self, ingest):
        with patch.object(ingest.dedup, "process", return_value=None):
            assert ingest.process_alert(ALERT) is None
        ingest._lambda_client.invoke.assert_not_called()


class TestEventBridgePath:
    def test_cloudwatch_event_is_normalized_and_processed(self, ingest):
        with patch.object(ingest, "process_alert", return_value={"incident_id": "inc-1"}) as process:
            assert ingest.handler(_cw_event(), None) == {"incident_id": "inc-1"}
        alert = process.call_args[0][0]
        assert alert["source"] == "cloudwatch"
        assert alert["affected_service"] == "payments-service"
        assert alert["status"] == "open"

    def test_unknown_source_is_discarded(self, ingest):
        with patch.object(ingest, "process_alert") as process:
            assert ingest.handler({"source": "pagerduty"}, None) is None
        process.assert_not_called()
        ingest._lambda_client.invoke.assert_not_called()


class TestWebhookPath:
    def test_datadog_webhook_runs_the_whole_path_and_reports_the_incident(self, ingest):
        event = _dd_webhook({"id": "dd-1", "title": "[Triggered on {service:payments}] Error rate", "priority": "P2",
                             "alert_transition": "Triggered", "tags": "service:payments-service,env:prod", "alert_id": "99999"})
        with patch.object(ingest.dedup, "process", return_value={"incident_id": "inc-9", "is_new": True, "alert_count": 1, "alert": {}}) as process:
            response = ingest.handler(event, None)
        assert response["statusCode"] == 202
        assert json.loads(response["body"]) == {"status": "accepted", "incident_id": "inc-9"}
        alert = process.call_args[0][0]
        assert alert["source"] == "datadog"
        assert alert["alert_name"] == "Error rate"
        assert alert["severity"] == "high"
        assert alert["monitor_id"] == "99999"
        assert _payload(ingest) == {"incident_id": "inc-9"}

    def test_ignored_delivery_is_still_accepted(self, ingest):
        # Authenticated, well-formed, but not an alert: a Datadog payload the
        # normalizer cannot read is discarded and logged, never a 5xx.
        with patch.object(ingest.dedup, "process") as process:
            response = ingest.handler(_dd_webhook({"nothing": "here"}), None)
        assert response["statusCode"] == 202
        assert json.loads(response["body"]) == {"status": "accepted"}
        process.assert_not_called()
        ingest._lambda_client.invoke.assert_not_called()

    def test_rejected_webhook_never_reaches_normalization(self, ingest):
        event = _dd_webhook({"id": "dd-1"})
        event["headers"]["x-webhook-secret"] = "wrong"
        with patch.object(ingest.normalize, "normalize") as normalize:
            assert ingest.handler(event, None)["statusCode"] == 401
        normalize.assert_not_called()
