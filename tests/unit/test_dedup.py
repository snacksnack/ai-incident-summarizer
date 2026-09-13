import time
from unittest.mock import MagicMock, patch

import pytest
from boto3.dynamodb.conditions import ConditionExpressionBuilder
from botocore.exceptions import ClientError

from common import aws
from common.fingerprint import generate_fingerprint
from tests.conftest import load_function_module

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

def _conditional_check_failed_error():
    error_response = {"Error": {"Code": "ConditionalCheckFailedException", "Message": "The conditional request failed"}}
    return ClientError(error_response, "PutItem")


def _other_dynamo_error():
    error_response = {"Error": {"Code": "ProvisionedThroughputExceededException", "Message": "Throughput exceeded"}}
    return ClientError(error_response, "PutItem")


def _key_names(condition) -> set[str]:
    built = ConditionExpressionBuilder().build_expression(condition, is_key_condition=True)
    return set(built.attribute_name_placeholders.values())


ALERT_STATE_TABLE = "test-alert-state-table"
INCIDENT_TABLE = "test-incident-table"
SERVICE_REGISTRY_TABLE = "test-service-registry-table"


class RoutedStateTable:
    """The one alert-state table (RC1-432), split back into a fingerprint mock
    and a window mock by key prefix, so each test can still set up and assert
    the two kinds of row separately."""

    def __init__(self):
        self.fingerprints = MagicMock()
        self.windows = MagicMock()

    def _side(self, kwargs):
        pk = (kwargs.get("Item") or kwargs.get("Key"))["pk"]
        assert pk.startswith(("fp#", "window#")), pk
        return self.fingerprints if pk.startswith("fp#") else self.windows

    def put_item(self, **kwargs):
        return self._side(kwargs).put_item(**kwargs)

    def update_item(self, **kwargs):
        return self._side(kwargs).update_item(**kwargs)

    def delete_item(self, **kwargs):
        return self._side(kwargs).delete_item(**kwargs)


@pytest.fixture()
def dedup(monkeypatch):
    monkeypatch.setenv("ALERT_STATE_TABLE_NAME", ALERT_STATE_TABLE)
    monkeypatch.setenv("INCIDENT_TABLE_NAME", INCIDENT_TABLE)
    monkeypatch.setenv("SERVICE_REGISTRY_TABLE_NAME", SERVICE_REGISTRY_TABLE)
    monkeypatch.setenv("CORRELATION_WINDOW_MINUTES", WINDOW_MINUTES)
    aws.reset()
    state = RoutedStateTable()
    mock_incident_table = MagicMock()
    mock_registry_table = MagicMock()
    aws._tables.update({
        ALERT_STATE_TABLE: state,
        INCIDENT_TABLE: mock_incident_table,
        SERVICE_REGISTRY_TABLE: mock_registry_table,
    })
    app = load_function_module("ingest", "dedup")
    # The tuple keeps the fingerprint side and the window side apart, as the
    # tests were written; registry_table() reaches the fifth mock.
    yield app, state.fingerprints, state.windows, mock_incident_table, mock_registry_table


def registry_table(app) -> MagicMock:
    return aws._tables[SERVICE_REGISTRY_TABLE]


class TestDedupHandler:
    def _setup_new_incident(self, mock_window_table, mock_incident_table):
        mock_window_table.put_item.return_value = {}
        mock_incident_table.put_item.return_value = {}

    def _setup_existing_incident(self, mock_window_table, mock_incident_table, incident_id="existing-inc-123", count=2):
        mock_window_table.put_item.side_effect = _conditional_check_failed_error()
        mock_window_table.update_item.return_value = {
            "Attributes": {
                "incident_id": incident_id,
                "alert_count": count,
                "service_key": ALERT["affected_service"],
            }
        }
        mock_incident_table.update_item.return_value = {}

    def test_first_occurrence_returns_incident_envelope(self, dedup):
        app, mock_dedup_table, mock_window_table, mock_incident_table, _ = dedup
        mock_dedup_table.put_item.return_value = {}
        self._setup_new_incident(mock_window_table, mock_incident_table)
        result = app.process(ALERT)
        assert result is not None
        assert result["alert"] == ALERT
        assert result["is_new"] is True
        assert result["alert_count"] == 1
        assert "incident_id" in result

    def test_first_occurrence_calls_put_item(self, dedup):
        app, mock_dedup_table, mock_window_table, mock_incident_table, _ = dedup
        mock_dedup_table.put_item.return_value = {}
        self._setup_new_incident(mock_window_table, mock_incident_table)
        app.process(ALERT)
        mock_dedup_table.put_item.assert_called_once()
        call_kwargs = mock_dedup_table.put_item.call_args[1]
        fingerprint = generate_fingerprint(ALERT["source"], ALERT["alert_name"], ALERT["affected_service"])
        assert call_kwargs["Item"]["pk"] == f"fp#{fingerprint}"
        assert call_kwargs["Item"]["fingerprint"] == fingerprint
        assert "ConditionExpression" in call_kwargs  # shape asserted in test_condition_accepts_missing_or_expired_fingerprint

    def test_duplicate_returns_none(self, dedup):
        app, mock_dedup_table, mock_window_table, mock_incident_table, _ = dedup
        mock_dedup_table.put_item.side_effect = _conditional_check_failed_error()
        result = app.process(ALERT)
        assert result is None

    def test_duplicate_logs_warning(self, dedup, caplog):
        import logging
        app, mock_dedup_table, mock_window_table, mock_incident_table, _ = dedup
        mock_dedup_table.put_item.side_effect = _conditional_check_failed_error()
        with caplog.at_level(logging.WARNING):
            app.process(ALERT)
        assert "Suppressing duplicate alert" in caplog.text
        assert ALERT["source"] in caplog.text
        assert ALERT["alert_name"] in caplog.text

    def test_other_dynamo_error_propagates(self, dedup):
        app, mock_dedup_table, mock_window_table, mock_incident_table, _ = dedup
        mock_dedup_table.put_item.side_effect = _other_dynamo_error()
        with pytest.raises(ClientError):
            app.process(ALERT)

    def test_condition_accepts_missing_or_expired_fingerprint(self, dedup):
        # RC1-372: DynamoDB's TTL sweep is lazy (up to ~48 h), so the condition
        # itself must treat an expired row as absent.
        from boto3.dynamodb.conditions import ConditionExpressionBuilder
        app, mock_dedup_table, mock_window_table, mock_incident_table, _ = dedup
        mock_dedup_table.put_item.return_value = {}
        self._setup_new_incident(mock_window_table, mock_incident_table)
        with patch("time.time", return_value=1_700_000_000):
            app.process(ALERT)
        condition = mock_dedup_table.put_item.call_args[1]["ConditionExpression"]
        built = ConditionExpressionBuilder().build_expression(condition)
        names = {v: k for k, v in built.attribute_name_placeholders.items()}
        values = built.attribute_value_placeholders
        expr = built.condition_expression
        assert expr == f"(attribute_not_exists({names['pk']}) OR {names['ttl']} <= :v0)"
        assert values[":v0"] == 1_700_000_000

    def test_ttl_equals_now_plus_window(self, dedup):
        app, mock_dedup_table, mock_window_table, mock_incident_table, _ = dedup
        mock_dedup_table.put_item.return_value = {}
        self._setup_new_incident(mock_window_table, mock_incident_table)
        before = int(time.time()) + int(WINDOW_MINUTES) * 60
        app.process(ALERT)
        after = int(time.time()) + int(WINDOW_MINUTES) * 60
        written_ttl = mock_dedup_table.put_item.call_args[1]["Item"]["ttl"]
        assert before <= written_ttl <= after


# ── Window grouping tests ─────────────────────────────────────────────────────

class TestWindowGrouping:
    def test_new_service_opens_new_incident(self, dedup):
        app, mock_dedup_table, mock_window_table, mock_incident_table, _ = dedup
        mock_dedup_table.put_item.return_value = {}
        mock_window_table.put_item.return_value = {}
        mock_incident_table.put_item.return_value = {}
        result = app.process(ALERT)
        assert result["is_new"] is True
        assert result["alert_count"] == 1
        assert "incident_id" in result

    def test_second_alert_same_service_joins_existing_incident(self, dedup):
        app, mock_dedup_table, mock_window_table, mock_incident_table, _ = dedup
        mock_dedup_table.put_item.return_value = {}
        mock_window_table.put_item.side_effect = _conditional_check_failed_error()
        mock_window_table.update_item.return_value = {
            "Attributes": {
                "incident_id": "existing-inc-456",
                "alert_count": 2,
                "service_key": ALERT["affected_service"],
            }
        }
        mock_incident_table.update_item.return_value = {}
        result = app.process(ALERT)
        assert result["is_new"] is False
        assert result["incident_id"] == "existing-inc-456"
        assert result["alert_count"] == 2

    def test_different_services_get_different_incident_ids(self, dedup):
        app, mock_dedup_table, mock_window_table, mock_incident_table, _ = dedup
        mock_dedup_table.put_item.return_value = {}
        mock_window_table.put_item.return_value = {}
        mock_incident_table.put_item.return_value = {}

        result1 = app.process(ALERT)
        other_alert = {**ALERT, "affected_service": "checkout-service"}
        result2 = app.process(other_alert)

        assert result1["incident_id"] != result2["incident_id"]

    def test_window_table_put_stores_alert_summary_without_raw_payload(self, dedup):
        app, mock_dedup_table, mock_window_table, mock_incident_table, _ = dedup
        mock_dedup_table.put_item.return_value = {}
        mock_window_table.put_item.return_value = {}
        mock_incident_table.put_item.return_value = {}
        app.process(ALERT)
        item = mock_window_table.put_item.call_args[1]["Item"]
        assert "alert_summaries" in item
        summary = item["alert_summaries"][0]
        assert "raw_payload" not in summary
        assert summary["alert_id"] == ALERT["alert_id"]
        assert summary["source"] == ALERT["source"]

    def test_window_table_error_propagates(self, dedup):
        app, mock_dedup_table, mock_window_table, mock_incident_table, _ = dedup
        mock_dedup_table.put_item.return_value = {}
        mock_window_table.put_item.side_effect = _other_dynamo_error()
        with pytest.raises(ClientError):
            app.process(ALERT)


class TestAlertSummary:
    def test_monitor_id_kept_when_present(self, dedup):
        app, *_ = dedup
        summary = app._alert_summary({**ALERT, "monitor_id": "99999"})
        assert summary["monitor_id"] == "99999"
        assert "raw_payload" not in summary

    def test_monitor_id_omitted_when_absent(self, dedup):
        app, *_ = dedup
        assert "monitor_id" not in app._alert_summary({**ALERT, "monitor_id": None})
        assert "monitor_id" not in app._alert_summary(ALERT)


# ── Incident persistence tests ────────────────────────────────────────────────

class TestIncidentPersistence:
    def test_new_incident_written_with_status_open(self, dedup):
        app, mock_dedup_table, mock_window_table, mock_incident_table, _ = dedup
        mock_dedup_table.put_item.return_value = {}
        mock_window_table.put_item.return_value = {}
        mock_incident_table.put_item.return_value = {}
        app.process(ALERT)
        mock_incident_table.put_item.assert_called_once()
        item = mock_incident_table.put_item.call_args[1]["Item"]
        assert item["status"] == "open"
        assert item["affected_service"] == ALERT["affected_service"]
        assert item["severity"] == ALERT["severity"]
        assert len(item["source_alerts"]) == 1
        assert "raw_payload" not in item["source_alerts"][0]

    def test_new_incident_write_is_idempotent(self, dedup):
        app, mock_dedup_table, mock_window_table, mock_incident_table, _ = dedup
        mock_dedup_table.put_item.return_value = {}
        mock_window_table.put_item.return_value = {}
        mock_incident_table.put_item.side_effect = _conditional_check_failed_error()
        result = app.process(ALERT)
        assert result is not None

    def test_existing_incident_calls_update_item(self, dedup):
        app, mock_dedup_table, mock_window_table, mock_incident_table, _ = dedup
        mock_dedup_table.put_item.return_value = {}
        mock_window_table.put_item.side_effect = _conditional_check_failed_error()
        mock_window_table.update_item.return_value = {
            "Attributes": {"incident_id": "inc-123", "alert_count": 2, "service_key": ALERT["affected_service"]}
        }
        mock_incident_table.update_item.return_value = {}
        app.process(ALERT)
        mock_incident_table.update_item.assert_called_once()
        call_kwargs = mock_incident_table.update_item.call_args[1]
        assert call_kwargs["Key"] == {"incident_id": "inc-123"}

    def test_incident_table_error_propagates(self, dedup):
        app, mock_dedup_table, mock_window_table, mock_incident_table, _ = dedup
        mock_dedup_table.put_item.return_value = {}
        mock_window_table.put_item.return_value = {}
        mock_incident_table.put_item.side_effect = _other_dynamo_error()
        with pytest.raises(ClientError):
            app.process(ALERT)


# ── Service registry tests ────────────────────────────────────────────────────

class TestServiceRegistry:
    def _setup_new_incident(self, mock_dedup_table, mock_window_table, mock_incident_table):
        mock_dedup_table.put_item.return_value = {}
        mock_window_table.put_item.return_value = {}
        mock_incident_table.put_item.return_value = {}

    def test_new_incident_registers_service(self, dedup):
        app, mock_dedup_table, mock_window_table, mock_incident_table, _ = dedup
        self._setup_new_incident(mock_dedup_table, mock_window_table, mock_incident_table)
        app.process(ALERT)
        registry = registry_table(app)
        registry.update_item.assert_called_once()
        call_kwargs = registry.update_item.call_args[1]
        assert call_kwargs["Key"] == {"affected_service": ALERT["affected_service"]}

    def test_registry_write_sets_last_seen_and_preserves_first_seen(self, dedup):
        app, mock_dedup_table, mock_window_table, mock_incident_table, _ = dedup
        self._setup_new_incident(mock_dedup_table, mock_window_table, mock_incident_table)
        app.process(ALERT)
        call_kwargs = registry_table(app).update_item.call_args[1]
        expression = call_kwargs["UpdateExpression"]
        assert "last_seen_at = :ts" in expression
        assert "first_seen_at = if_not_exists(first_seen_at, :ts)" in expression
        assert set(call_kwargs["ExpressionAttributeValues"]) == {":ts"}

    def test_existing_incident_does_not_register(self, dedup):
        app, mock_dedup_table, mock_window_table, mock_incident_table, _ = dedup
        mock_dedup_table.put_item.return_value = {}
        mock_window_table.put_item.side_effect = _conditional_check_failed_error()
        mock_window_table.update_item.return_value = {
            "Attributes": {"incident_id": "inc-123", "alert_count": 2, "service_key": ALERT["affected_service"]}
        }
        mock_incident_table.update_item.return_value = {}
        app.process(ALERT)
        registry_table(app).update_item.assert_not_called()

    def test_duplicate_incident_write_does_not_register(self, dedup):
        app, mock_dedup_table, mock_window_table, mock_incident_table, _ = dedup
        mock_dedup_table.put_item.return_value = {}
        mock_window_table.put_item.return_value = {}
        mock_incident_table.put_item.side_effect = _conditional_check_failed_error()
        app.process(ALERT)
        registry_table(app).update_item.assert_not_called()

    def test_registry_failure_does_not_break_incident_flow(self, dedup):
        app, mock_dedup_table, mock_window_table, mock_incident_table, _ = dedup
        self._setup_new_incident(mock_dedup_table, mock_window_table, mock_incident_table)
        registry_table(app).update_item.side_effect = _other_dynamo_error()
        result = app.process(ALERT)
        assert result is not None
        assert result["is_new"] is True
        mock_incident_table.put_item.assert_called_once()

    def test_registry_failure_logs_warning(self, dedup, caplog):
        import logging
        app, mock_dedup_table, mock_window_table, mock_incident_table, _ = dedup
        self._setup_new_incident(mock_dedup_table, mock_window_table, mock_incident_table)
        registry_table(app).update_item.side_effect = _other_dynamo_error()
        with caplog.at_level(logging.WARNING):
            app.process(ALERT)
        assert "Service registry write failed" in caplog.text
        assert ALERT["affected_service"] in caplog.text

    def test_missing_registry_table_env_var_does_not_break_incident_flow(self, dedup, monkeypatch):
        app, mock_dedup_table, mock_window_table, mock_incident_table, _ = dedup
        self._setup_new_incident(mock_dedup_table, mock_window_table, mock_incident_table)
        # Simulate the window between deploying the function and the table existing.
        monkeypatch.delenv("SERVICE_REGISTRY_TABLE_NAME", raising=False)
        result = app.process(ALERT)
        assert result is not None
        mock_incident_table.put_item.assert_called_once()


# ── Recoveries (RC1-374) ─────────────────────────────────────────────────────

RESOLVED_ALERT = {**ALERT, "alert_id": "test-id-456", "status": "resolved", "severity": "low",
                  "received_at": "2024-01-15T10:45:00Z"}
OPEN_INCIDENT = {
    "incident_id": "inc-open-1",
    "affected_service": "payments-service",
    "severity": "high",
    "status": "open",
    "created_at": "2024-01-15T10:30:07Z",
    "source_alerts": [{"alert_id": "test-id-123", "source": "cloudwatch",
                       "alert_name": "payments-service-error-rate", "severity": "high",
                       "status": "open", "received_at": "2024-01-15T10:30:00Z"}],
}


class TestRecovery:
    def _incidents(self, mock_incident_table, *incidents):
        mock_incident_table.query.return_value = {"Items": list(incidents)}
        mock_incident_table.update_item.return_value = {}

    def test_recovery_closes_matching_open_incident(self, dedup):
        app, mock_dedup_table, mock_window_table, mock_incident_table, _ = dedup
        self._incidents(mock_incident_table, OPEN_INCIDENT)
        result = app.process(RESOLVED_ALERT)
        assert result["resolved"] is True
        assert result["incident_id"] == "inc-open-1"
        assert result["alert_count"] == 2
        kwargs = mock_incident_table.update_item.call_args[1]
        assert kwargs["Key"] == {"incident_id": "inc-open-1"}
        assert kwargs["ExpressionAttributeValues"][":resolved"] == "resolved"
        assert kwargs["ExpressionAttributeValues"][":s"][0]["status"] == "resolved"
        assert "resolved_at" in kwargs["UpdateExpression"]

    def test_recovery_looks_up_by_service_index(self, dedup):
        app, _, _, mock_incident_table, _ = dedup
        self._incidents(mock_incident_table, OPEN_INCIDENT)
        app.process(RESOLVED_ALERT)
        assert mock_incident_table.query.call_args[1]["IndexName"] == "service-created-index"

    def test_recovery_retires_window_and_fingerprint(self, dedup):
        app, mock_dedup_table, mock_window_table, mock_incident_table, _ = dedup
        self._incidents(mock_incident_table, OPEN_INCIDENT)
        app.process(RESOLVED_ALERT)
        mock_window_table.delete_item.assert_called_once()
        assert mock_window_table.delete_item.call_args[1]["Key"] == {"pk": "window#payments-service"}
        mock_dedup_table.delete_item.assert_called_once_with(Key={"pk": "fp#" + generate_fingerprint(
            ALERT["source"], ALERT["alert_name"], ALERT["affected_service"])})
        mock_dedup_table.put_item.assert_not_called()

    def test_recovery_with_no_open_incident_is_dropped(self, dedup, caplog):
        app, mock_dedup_table, mock_window_table, mock_incident_table, _ = dedup
        self._incidents(mock_incident_table)
        with caplog.at_level("INFO"):
            assert app.process(RESOLVED_ALERT) is None
        mock_incident_table.update_item.assert_not_called()
        mock_incident_table.put_item.assert_not_called()
        mock_dedup_table.put_item.assert_not_called()
        assert "no open incident" in caplog.text

    def test_recovery_ignores_resolved_and_unrelated_incidents(self, dedup):
        app, _, _, mock_incident_table, _ = dedup
        already = {**OPEN_INCIDENT, "incident_id": "inc-done", "status": "resolved"}
        other = {**OPEN_INCIDENT, "incident_id": "inc-other", "source_alerts": [
            {**OPEN_INCIDENT["source_alerts"][0], "alert_name": "different-alarm"}]}
        self._incidents(mock_incident_table, already, other)
        assert app.process(RESOLVED_ALERT) is None

    def test_duplicate_recovery_is_dropped_when_already_closed(self, dedup):
        app, _, mock_window_table, mock_incident_table, _ = dedup
        self._incidents(mock_incident_table, OPEN_INCIDENT)
        mock_incident_table.update_item.side_effect = _conditional_check_failed_error()
        assert app.process(RESOLVED_ALERT) is None
        mock_window_table.delete_item.assert_not_called()

    def test_window_owned_by_newer_incident_is_left_alone(self, dedup):
        app, _, mock_window_table, mock_incident_table, _ = dedup
        self._incidents(mock_incident_table, OPEN_INCIDENT)
        mock_window_table.delete_item.side_effect = _conditional_check_failed_error()
        result = app.process(RESOLVED_ALERT)
        assert result["resolved"] is True

    def test_open_alert_path_is_unchanged(self, dedup):
        # An open alert never looks for an incident to close; the only query
        # it makes is the recurrence lookup (RC1-437), which is bounded to the
        # last 7 days and touches nothing.
        app, mock_dedup_table, mock_window_table, mock_incident_table, _ = dedup
        mock_dedup_table.put_item.return_value = {}
        mock_window_table.put_item.return_value = {}
        mock_incident_table.put_item.return_value = {}
        result = app.process(ALERT)
        assert result["is_new"] is True
        assert "resolved" not in result
        mock_incident_table.update_item.assert_not_called()
        for call in mock_incident_table.query.call_args_list:
            assert "created_at" in _key_names(call.kwargs["KeyConditionExpression"])


# ── Recurrence (RC1-437) ──────────────────────────────────────────────────────

def _prior(incident_id, alert_name=ALERT["alert_name"], source=ALERT["source"], created_at="2024-01-14T10:30:00Z", jira=None):
    incident = {
        "incident_id": incident_id,
        "affected_service": ALERT["affected_service"],
        "status": "resolved",
        "created_at": created_at,
        "source_alerts": [{"alert_id": "x", "source": source, "alert_name": alert_name, "severity": "high", "status": "open", "received_at": created_at}],
    }
    if jira:
        incident["jira_ticket_id"] = jira
    return incident


class TestRecurrence:
    def _new_incident(self, dedup, prior_items):
        app, mock_dedup_table, mock_window_table, mock_incident_table, _ = dedup
        mock_dedup_table.put_item.return_value = {}
        mock_window_table.put_item.return_value = {}
        mock_incident_table.put_item.return_value = {}
        mock_incident_table.query.return_value = {"Items": prior_items}
        app.process(ALERT)
        return mock_incident_table

    def test_repeat_of_the_same_alert_is_counted_and_points_at_the_newest(self, dedup):
        table = self._new_incident(dedup, [
            _prior("inc-newest", created_at="2024-01-15T09:00:00Z", jira="INC-96"),
            _prior("inc-older", created_at="2024-01-14T09:00:00Z", jira="INC-94"),
        ])
        item = table.put_item.call_args[1]["Item"]
        assert item["recurrence"] == {
            "count_7d": 2,
            "previous_incident_id": "inc-newest",
            "previous_created_at": "2024-01-15T09:00:00Z",
            "previous_jira_ticket_id": "INC-96",
        }

    def test_other_alerts_for_the_service_do_not_count(self, dedup):
        table = self._new_incident(dedup, [
            _prior("inc-1", alert_name="some-other-alarm"),
            _prior("inc-2", source="datadog"),
        ])
        assert "recurrence" not in table.put_item.call_args[1]["Item"]

    def test_first_occurrence_has_no_recurrence_field(self, dedup):
        table = self._new_incident(dedup, [])
        assert "recurrence" not in table.put_item.call_args[1]["Item"]

    def test_previous_without_a_ticket_omits_the_key(self, dedup):
        table = self._new_incident(dedup, [_prior("inc-1")])
        recurrence = table.put_item.call_args[1]["Item"]["recurrence"]
        assert recurrence["count_7d"] == 1
        assert "previous_jira_ticket_id" not in recurrence

    def test_lookup_is_bounded_to_the_window_on_the_service_index(self, dedup):
        table = self._new_incident(dedup, [])
        kwargs = table.query.call_args[1]
        assert kwargs["IndexName"] == "service-created-index"
        assert kwargs["ScanIndexForward"] is False
        assert _key_names(kwargs["KeyConditionExpression"]) == {"affected_service", "created_at"}

    def test_lookup_failure_still_writes_the_incident(self, dedup, caplog):
        app, mock_dedup_table, mock_window_table, mock_incident_table, _ = dedup
        mock_dedup_table.put_item.return_value = {}
        mock_window_table.put_item.return_value = {}
        mock_incident_table.put_item.return_value = {}
        mock_incident_table.query.side_effect = _other_dynamo_error()
        with caplog.at_level("WARNING"):
            result = app.process(ALERT)
        assert result["is_new"] is True
        mock_incident_table.put_item.assert_called_once()
        assert "recurrence" not in mock_incident_table.put_item.call_args[1]["Item"]
        assert "Recurrence lookup failed" in caplog.text

    def test_joining_an_open_window_does_not_look_up(self, dedup):
        app, mock_dedup_table, mock_window_table, mock_incident_table, _ = dedup
        mock_dedup_table.put_item.return_value = {}
        mock_window_table.put_item.side_effect = _conditional_check_failed_error()
        mock_window_table.update_item.return_value = {
            "Attributes": {"incident_id": "inc-123", "alert_count": 2, "service_key": ALERT["affected_service"]}
        }
        mock_incident_table.update_item.return_value = {}
        app.process(ALERT)
        mock_incident_table.query.assert_not_called()
