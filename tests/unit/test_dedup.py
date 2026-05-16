import importlib
import os
import sys
import time
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError

# conftest.py adds layers/common/python to sys.path
from common.fingerprint import generate_fingerprint

ALERT = {
    "alert_id": "test-id-123",
    "source": "cloudwatch",
    "alert_name": "payments-service-error-rate",
    "affected_service": "payments-service",
    "severity": "high",
    "status": "open",
    "raw_payload": {},
    "received_at": "2024-01-15T10:30:00Z",
}

DEDUP_TABLE = "test-dedup-table"
WINDOW_MINUTES = "5"


# ── Fingerprint tests (pure function, no mocking) ─────────────────────────────

class TestGenerateFingerprint:
    def test_identical_inputs_produce_identical_fingerprint(self):
        fp1 = generate_fingerprint("cloudwatch", "my-alarm", "payments-service")
        fp2 = generate_fingerprint("cloudwatch", "my-alarm", "payments-service")
        assert fp1 == fp2

    def test_different_source_produces_different_fingerprint(self):
        fp1 = generate_fingerprint("cloudwatch", "my-alarm", "payments-service")
        fp2 = generate_fingerprint("datadog", "my-alarm", "payments-service")
        assert fp1 != fp2

    def test_different_alert_name_produces_different_fingerprint(self):
        fp1 = generate_fingerprint("cloudwatch", "alarm-a", "payments-service")
        fp2 = generate_fingerprint("cloudwatch", "alarm-b", "payments-service")
        assert fp1 != fp2

    def test_different_affected_service_produces_different_fingerprint(self):
        fp1 = generate_fingerprint("cloudwatch", "my-alarm", "payments-service")
        fp2 = generate_fingerprint("cloudwatch", "my-alarm", "checkout-service")
        assert fp1 != fp2

    def test_output_is_64_char_hex_string(self):
        fp = generate_fingerprint("cloudwatch", "my-alarm", "payments-service")
        assert len(fp) == 64
        assert all(c in "0123456789abcdef" for c in fp)

    def test_deterministic_across_invocations(self):
        results = {generate_fingerprint("github", "CI", "org/repo") for _ in range(10)}
        assert len(results) == 1


# ── Dedup handler tests ───────────────────────────────────────────────────────

def _load_dedup():
    for mod in list(sys.modules):
        if mod == "app" or mod.startswith("app."):
            del sys.modules[mod]
    sys.path.insert(0, "functions/dedup")
    import app
    importlib.reload(app)
    return app


def _conditional_check_failed_error():
    error_response = {"Error": {"Code": "ConditionalCheckFailedException", "Message": "The conditional request failed"}}
    return ClientError(error_response, "PutItem")


def _other_dynamo_error():
    error_response = {"Error": {"Code": "ProvisionedThroughputExceededException", "Message": "Throughput exceeded"}}
    return ClientError(error_response, "PutItem")


@pytest.fixture()
def dedup(monkeypatch):
    monkeypatch.setenv("DEDUP_TABLE_NAME", DEDUP_TABLE)
    monkeypatch.setenv("CORRELATION_WINDOW_MINUTES", WINDOW_MINUTES)
    mock_table = MagicMock()
    with patch("boto3.resource") as mock_resource:
        mock_resource.return_value.Table.return_value = mock_table
        app = _load_dedup()
        app._table = mock_table
        yield app, mock_table


class TestDedupHandler:
    def test_first_occurrence_returns_alert(self, dedup):
        app, mock_table = dedup
        mock_table.put_item.return_value = {}
        result = app.handler(ALERT, None)
        assert result == ALERT

    def test_first_occurrence_calls_put_item(self, dedup):
        app, mock_table = dedup
        mock_table.put_item.return_value = {}
        app.handler(ALERT, None)
        mock_table.put_item.assert_called_once()
        call_kwargs = mock_table.put_item.call_args[1]
        assert call_kwargs["Item"]["fingerprint"] == generate_fingerprint(
            ALERT["source"], ALERT["alert_name"], ALERT["affected_service"]
        )
        assert call_kwargs["ConditionExpression"] == "attribute_not_exists(fingerprint)"

    def test_duplicate_returns_none(self, dedup):
        app, mock_table = dedup
        mock_table.put_item.side_effect = _conditional_check_failed_error()
        result = app.handler(ALERT, None)
        assert result is None

    def test_duplicate_logs_warning(self, dedup, caplog):
        import logging
        app, mock_table = dedup
        mock_table.put_item.side_effect = _conditional_check_failed_error()
        with caplog.at_level(logging.WARNING):
            app.handler(ALERT, None)
        assert "Suppressing duplicate alert" in caplog.text
        assert ALERT["source"] in caplog.text
        assert ALERT["alert_name"] in caplog.text

    def test_other_dynamo_error_propagates(self, dedup):
        app, mock_table = dedup
        mock_table.put_item.side_effect = _other_dynamo_error()
        with pytest.raises(ClientError):
            app.handler(ALERT, None)

    def test_ttl_equals_now_plus_window(self, dedup):
        app, mock_table = dedup
        mock_table.put_item.return_value = {}
        before = int(time.time()) + int(WINDOW_MINUTES) * 60
        app.handler(ALERT, None)
        after = int(time.time()) + int(WINDOW_MINUTES) * 60
        written_ttl = mock_table.put_item.call_args[1]["Item"]["ttl"]
        assert before <= written_ttl <= after
