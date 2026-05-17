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


CORRELATION_TABLE = "test-correlation-table"


@pytest.fixture()
def dedup(monkeypatch):
    monkeypatch.setenv("DEDUP_TABLE_NAME", DEDUP_TABLE)
    monkeypatch.setenv("CORRELATION_TABLE_NAME", CORRELATION_TABLE)
    monkeypatch.setenv("CORRELATION_WINDOW_MINUTES", WINDOW_MINUTES)
    mock_dedup_table = MagicMock()
    mock_window_table = MagicMock()
    with patch("boto3.resource"):
        app = _load_dedup()
        app._table = mock_dedup_table
        app._window_table = mock_window_table
        yield app, mock_dedup_table, mock_window_table


class TestDedupHandler:
    def _window_new_incident(self, mock_window_table):
        """Configure the window table mock to simulate opening a new incident."""
        mock_window_table.put_item.return_value = {}

    def _window_existing_incident(self, mock_window_table, incident_id="existing-inc-123", count=2):
        """Configure the window table mock to simulate an open window."""
        mock_window_table.put_item.side_effect = _conditional_check_failed_error()
        mock_window_table.update_item.return_value = {
            "Attributes": {
                "incident_id": incident_id,
                "alert_count": count,
                "service_key": ALERT["affected_service"],
            }
        }

    def test_first_occurrence_returns_incident_envelope(self, dedup):
        app, mock_dedup_table, mock_window_table = dedup
        mock_dedup_table.put_item.return_value = {}
        self._window_new_incident(mock_window_table)
        result = app.handler(ALERT, None)
        assert result is not None
        assert result["alert"] == ALERT
        assert result["is_new"] is True
        assert result["alert_count"] == 1
        assert "incident_id" in result

    def test_first_occurrence_calls_put_item(self, dedup):
        app, mock_dedup_table, mock_window_table = dedup
        mock_dedup_table.put_item.return_value = {}
        self._window_new_incident(mock_window_table)
        app.handler(ALERT, None)
        mock_dedup_table.put_item.assert_called_once()
        call_kwargs = mock_dedup_table.put_item.call_args[1]
        assert call_kwargs["Item"]["fingerprint"] == generate_fingerprint(
            ALERT["source"], ALERT["alert_name"], ALERT["affected_service"]
        )
        assert call_kwargs["ConditionExpression"] == "attribute_not_exists(fingerprint)"

    def test_duplicate_returns_none(self, dedup):
        app, mock_dedup_table, mock_window_table = dedup
        mock_dedup_table.put_item.side_effect = _conditional_check_failed_error()
        result = app.handler(ALERT, None)
        assert result is None

    def test_duplicate_logs_warning(self, dedup, caplog):
        import logging
        app, mock_dedup_table, mock_window_table = dedup
        mock_dedup_table.put_item.side_effect = _conditional_check_failed_error()
        with caplog.at_level(logging.WARNING):
            app.handler(ALERT, None)
        assert "Suppressing duplicate alert" in caplog.text
        assert ALERT["source"] in caplog.text
        assert ALERT["alert_name"] in caplog.text

    def test_other_dynamo_error_propagates(self, dedup):
        app, mock_dedup_table, mock_window_table = dedup
        mock_dedup_table.put_item.side_effect = _other_dynamo_error()
        with pytest.raises(ClientError):
            app.handler(ALERT, None)

    def test_ttl_equals_now_plus_window(self, dedup):
        app, mock_dedup_table, mock_window_table = dedup
        mock_dedup_table.put_item.return_value = {}
        self._window_new_incident(mock_window_table)
        before = int(time.time()) + int(WINDOW_MINUTES) * 60
        app.handler(ALERT, None)
        after = int(time.time()) + int(WINDOW_MINUTES) * 60
        written_ttl = mock_dedup_table.put_item.call_args[1]["Item"]["ttl"]
        assert before <= written_ttl <= after


# ── Window grouping tests ─────────────────────────────────────────────────────

class TestWindowGrouping:
    def test_new_service_opens_new_incident(self, dedup):
        app, mock_dedup_table, mock_window_table = dedup
        mock_dedup_table.put_item.return_value = {}
        mock_window_table.put_item.return_value = {}
        result = app.handler(ALERT, None)
        assert result["is_new"] is True
        assert result["alert_count"] == 1
        assert "incident_id" in result

    def test_second_alert_same_service_joins_existing_incident(self, dedup):
        app, mock_dedup_table, mock_window_table = dedup
        mock_dedup_table.put_item.return_value = {}
        mock_window_table.put_item.side_effect = _conditional_check_failed_error()
        mock_window_table.update_item.return_value = {
            "Attributes": {
                "incident_id": "existing-inc-456",
                "alert_count": 2,
                "service_key": ALERT["affected_service"],
            }
        }
        result = app.handler(ALERT, None)
        assert result["is_new"] is False
        assert result["incident_id"] == "existing-inc-456"
        assert result["alert_count"] == 2

    def test_different_services_get_different_incident_ids(self, dedup):
        app, mock_dedup_table, mock_window_table = dedup
        mock_dedup_table.put_item.return_value = {}
        mock_window_table.put_item.return_value = {}

        result1 = app.handler(ALERT, None)
        other_alert = {**ALERT, "affected_service": "checkout-service"}
        result2 = app.handler(other_alert, None)

        assert result1["incident_id"] != result2["incident_id"]

    def test_window_table_put_stores_alert_summary_without_raw_payload(self, dedup):
        app, mock_dedup_table, mock_window_table = dedup
        mock_dedup_table.put_item.return_value = {}
        mock_window_table.put_item.return_value = {}
        app.handler(ALERT, None)
        item = mock_window_table.put_item.call_args[1]["Item"]
        assert "alert_summaries" in item
        summary = item["alert_summaries"][0]
        assert "raw_payload" not in summary
        assert summary["alert_id"] == ALERT["alert_id"]
        assert summary["source"] == ALERT["source"]

    def test_window_table_error_propagates(self, dedup):
        app, mock_dedup_table, mock_window_table = dedup
        mock_dedup_table.put_item.return_value = {}
        mock_window_table.put_item.side_effect = _other_dynamo_error()
        with pytest.raises(ClientError):
            app.handler(ALERT, None)
