import importlib
import json
import sys
from unittest.mock import MagicMock, patch

import pytest

# conftest.py adds layers/common/python to sys.path

DEDUP_FUNCTION_NAME = "dedup-function"


def _load():
    for mod in list(sys.modules):
        if mod == "app" or mod.startswith("app."):
            del sys.modules[mod]
    sys.path.insert(0, "functions/normalizer")
    import app
    importlib.reload(app)
    return app


@pytest.fixture()
def mock_lambda(monkeypatch):
    monkeypatch.setenv("DEDUP_FUNCTION_NAME", DEDUP_FUNCTION_NAME)
    mock = MagicMock()
    with patch("boto3.client", return_value=mock):
        yield mock


@pytest.fixture()
def normalizer(mock_lambda):
    app = _load()
    app._lambda_client = mock_lambda
    return app


# ── Fixtures ──────────────────────────────────────────────────────────────────

def _cw_event(alarm_name="payments-service-error-rate", state="ALARM", dimension_value="payments-service"):
    return {
        "version": "0",
        "id": "test-event-id-123",
        "source": "aws.cloudwatch",
        "account": "123456789012",
        "time": "2024-01-15T10:30:00Z",
        "region": "us-east-1",
        "detail-type": "CloudWatch Alarm State Change",
        "detail": {
            "alarmName": alarm_name,
            "state": {"value": state, "reason": "Threshold crossed", "timestamp": "2024-01-15T10:30:00Z"},
            "previousState": {"value": "OK", "reason": "OK", "timestamp": "2024-01-15T10:00:00Z"},
            "configuration": {
                "description": "Test alarm",
                "metrics": [
                    {
                        "id": "m1",
                        "metricStat": {
                            "metric": {
                                "namespace": "AWS/Lambda",
                                "name": "Errors",
                                "dimensions": {"FunctionName": dimension_value},
                            },
                            "period": 60,
                            "stat": "Sum",
                        },
                    }
                ],
            },
        },
    }


def _dd_envelope(priority="P1", transition="Triggered", tags=None, alert_type="error"):
    return {
        "source": "datadog",
        "received_at": "2024-01-15T10:30:00+00:00",
        "path": "/webhook/datadog",
        "raw_payload": {
            "id": "dd-alert-abc123",
            "title": "Error rate above threshold",
            "priority": priority,
            "alert_type": alert_type,
            "alert_transition": transition,
            "tags": tags or ["service:payments-service", "env:production"],
            "url": "https://app.datadoghq.com/monitors/99999",
        },
    }


def _gh_envelope(conclusion="failure", workflow_name="CI", repo="org/repo"):
    return {
        "source": "github",
        "received_at": "2024-01-15T10:30:00+00:00",
        "path": "/webhook/github",
        "raw_payload": {
            "action": "completed",
            "workflow_run": {
                "id": 1234567890,
                "name": workflow_name,
                "head_branch": "main",
                "conclusion": conclusion,
                "status": "completed",
            },
            "repository": {"full_name": repo, "name": repo.split("/")[-1]},
        },
    }


# ── CloudWatch tests ──────────────────────────────────────────────────────────

class TestCloudWatch:
    def test_alarm_state_returns_open(self, normalizer):
        result = normalizer.handler(_cw_event(state="ALARM"), None)
        assert result["status"] == "open"
        assert result["source"] == "cloudwatch"

    def test_ok_state_returns_resolved(self, normalizer):
        result = normalizer.handler(_cw_event(state="OK"), None)
        assert result["status"] == "resolved"

    def test_insufficient_data_returns_open(self, normalizer):
        result = normalizer.handler(_cw_event(state="INSUFFICIENT_DATA"), None)
        assert result["status"] == "open"

    def test_alarm_name_with_critical_keyword(self, normalizer):
        result = normalizer.handler(_cw_event(alarm_name="payments-service-critical-errors"), None)
        assert result["severity"] == "critical"

    def test_alarm_name_with_medium_keyword(self, normalizer):
        result = normalizer.handler(_cw_event(alarm_name="api-medium-latency"), None)
        assert result["severity"] == "medium"

    def test_alarm_default_severity_is_high_for_alarm_state(self, normalizer):
        result = normalizer.handler(_cw_event(alarm_name="no-keyword-alarm", state="ALARM"), None)
        assert result["severity"] == "high"

    def test_service_extracted_from_dimensions(self, normalizer):
        result = normalizer.handler(_cw_event(dimension_value="payments-service"), None)
        assert result["affected_service"] == "payments-service"

    def test_alert_id_from_event_id(self, normalizer):
        result = normalizer.handler(_cw_event(), None)
        assert result["alert_id"] == "test-event-id-123"

    def test_raw_payload_is_full_event(self, normalizer):
        event = _cw_event()
        result = normalizer.handler(event, None)
        assert result["raw_payload"] == event

    def test_received_at_from_event_time(self, normalizer):
        result = normalizer.handler(_cw_event(), None)
        assert result["received_at"] == "2024-01-15T10:30:00Z"


# ── Datadog tests ─────────────────────────────────────────────────────────────

class TestDatadog:
    def test_triggered_returns_open(self, normalizer):
        result = normalizer.handler(_dd_envelope(transition="Triggered"), None)
        assert result["status"] == "open"
        assert result["source"] == "datadog"

    def test_re_triggered_returns_open(self, normalizer):
        result = normalizer.handler(_dd_envelope(transition="Re-Triggered"), None)
        assert result["status"] == "open"

    def test_recovered_returns_resolved(self, normalizer):
        result = normalizer.handler(_dd_envelope(transition="Recovered"), None)
        assert result["status"] == "resolved"

    def test_p1_maps_to_critical(self, normalizer):
        result = normalizer.handler(_dd_envelope(priority="P1"), None)
        assert result["severity"] == "critical"

    def test_p2_maps_to_high(self, normalizer):
        result = normalizer.handler(_dd_envelope(priority="P2"), None)
        assert result["severity"] == "high"

    def test_p3_maps_to_medium(self, normalizer):
        result = normalizer.handler(_dd_envelope(priority="P3"), None)
        assert result["severity"] == "medium"

    def test_p4_maps_to_low(self, normalizer):
        result = normalizer.handler(_dd_envelope(priority="P4"), None)
        assert result["severity"] == "low"

    def test_alert_type_fallback_when_no_priority(self, normalizer):
        env = _dd_envelope(alert_type="warning")
        del env["raw_payload"]["priority"]
        result = normalizer.handler(env, None)
        assert result["severity"] == "medium"

    def test_service_extracted_from_tags(self, normalizer):
        result = normalizer.handler(_dd_envelope(tags=["service:checkout-service", "env:prod"]), None)
        assert result["affected_service"] == "checkout-service"

    def test_no_service_tag_returns_unknown(self, normalizer):
        result = normalizer.handler(_dd_envelope(tags=["env:production"]), None)
        assert result["affected_service"] == "unknown"

    def test_alert_id_from_payload_id(self, normalizer):
        result = normalizer.handler(_dd_envelope(), None)
        assert result["alert_id"] == "dd-alert-abc123"


# ── GitHub Actions tests ──────────────────────────────────────────────────────

class TestGitHub:
    def test_failure_returns_open_high(self, normalizer):
        result = normalizer.handler(_gh_envelope(conclusion="failure"), None)
        assert result["status"] == "open"
        assert result["severity"] == "high"
        assert result["source"] == "github"

    def test_timed_out_returns_open_high(self, normalizer):
        result = normalizer.handler(_gh_envelope(conclusion="timed_out"), None)
        assert result["status"] == "open"
        assert result["severity"] == "high"

    def test_cancelled_returns_open_medium(self, normalizer):
        result = normalizer.handler(_gh_envelope(conclusion="cancelled"), None)
        assert result["status"] == "open"
        assert result["severity"] == "medium"

    def test_success_returns_resolved(self, normalizer):
        result = normalizer.handler(_gh_envelope(conclusion="success"), None)
        assert result["status"] == "resolved"

    def test_affected_service_is_repo_full_name(self, normalizer):
        result = normalizer.handler(_gh_envelope(repo="acme/payments-api"), None)
        assert result["affected_service"] == "acme/payments-api"

    def test_alert_name_is_workflow_name(self, normalizer):
        result = normalizer.handler(_gh_envelope(workflow_name="Deploy to Production"), None)
        assert result["alert_name"] == "Deploy to Production"

    def test_alert_id_from_run_id(self, normalizer):
        result = normalizer.handler(_gh_envelope(), None)
        assert result["alert_id"] == "1234567890"


# ── Unknown source ────────────────────────────────────────────────────────────

class TestUnknownSource:
    def test_unknown_source_returns_none(self, normalizer):
        result = normalizer.handler({"source": "pagerduty", "data": {}}, None)
        assert result is None

    def test_missing_source_returns_none(self, normalizer):
        result = normalizer.handler({"data": "some payload"}, None)
        assert result is None


# ── Dedup invocation ──────────────────────────────────────────────────────────

class TestDedupInvocation:
    def test_valid_alert_invokes_dedup_async(self, normalizer, mock_lambda):
        normalizer.handler(_cw_event(), None)
        mock_lambda.invoke.assert_called_once()
        call_kwargs = mock_lambda.invoke.call_args[1]
        assert call_kwargs["FunctionName"] == DEDUP_FUNCTION_NAME
        assert call_kwargs["InvocationType"] == "Event"
        payload = json.loads(call_kwargs["Payload"])
        assert payload["source"] == "cloudwatch"

    def test_unknown_source_does_not_invoke_dedup(self, normalizer, mock_lambda):
        normalizer.handler({"source": "unknown"}, None)
        mock_lambda.invoke.assert_not_called()
