import json
import time
from unittest.mock import patch

import boto3
import pytest
from moto import mock_aws

from tests.integration.conftest import INCIDENT_TABLE


def _make_alert(service, source="cloudwatch", alert_name="cpu-alarm", n=0):
    return {
        "alert_id": f"alert-{service}-{n}",
        "source": source,
        "alert_name": alert_name,
        "affected_service": service,
        "severity": "high",
        "status": "open",
        "raw_payload": {},
        "received_at": "2024-01-15T10:30:00Z",
    }


class TestBurstGrouping:
    def test_four_alerts_same_service_share_incident(self, dedup_app):
        alerts = [_make_alert("payments-service", alert_name=f"alarm-{i}", n=i) for i in range(4)]
        results = [dedup_app.handler(a, None) for a in alerts]

        assert all(r is not None for r in results)
        incident_ids = {r["incident_id"] for r in results}
        assert len(incident_ids) == 1, f"Expected 1 incident, got {incident_ids}"
        assert results[-1]["alert_count"] == 4


class TestServiceIsolation:
    def test_different_services_produce_different_incidents(self, dedup_app):
        result_a = dedup_app.handler(_make_alert("payments-service"), None)
        result_b = dedup_app.handler(_make_alert("checkout-service"), None)

        assert result_a is not None
        assert result_b is not None
        assert result_a["incident_id"] != result_b["incident_id"]
        assert result_a["is_new"] is True
        assert result_b["is_new"] is True


class TestPersistence:
    def test_new_incident_written_to_dynamodb_with_status_open(self, dedup_app, dynamodb_tables):
        _, _, incident_table = dynamodb_tables
        result = dedup_app.handler(_make_alert("payments-service"), None)
        item = incident_table.get_item(Key={"incident_id": result["incident_id"]})["Item"]
        assert item["status"] == "open"
        assert item["affected_service"] == "payments-service"
        assert len(item["source_alerts"]) == 1

    def test_second_alert_appends_to_source_alerts(self, dedup_app, dynamodb_tables):
        _, _, incident_table = dynamodb_tables
        alert1 = _make_alert("payments-service", alert_name="alarm-1", n=0)
        alert2 = _make_alert("payments-service", alert_name="alarm-2", n=1)
        result1 = dedup_app.handler(alert1, None)
        dedup_app.handler(alert2, None)
        item = incident_table.get_item(Key={"incident_id": result1["incident_id"]})["Item"]
        assert len(item["source_alerts"]) == 2

    def test_idempotent_write_does_not_duplicate_incident(self, dedup_app, dynamodb_tables):
        _, _, incident_table = dynamodb_tables
        alert = _make_alert("payments-service")
        result = dedup_app.handler(alert, None)
        # Manually call _persist_incident again with is_new=True to simulate a race
        dedup_app._persist_incident(alert, {"incident_id": result["incident_id"], "is_new": True})
        item = incident_table.get_item(Key={"incident_id": result["incident_id"]})["Item"]
        assert len(item["source_alerts"]) == 1  # still only one


class TestWindowExpiry:
    def test_alert_after_window_expires_opens_new_incident(self, dedup_app):
        alert1 = _make_alert("orders-service", alert_name="alarm-1")
        alert2 = _make_alert("orders-service", alert_name="alarm-2")
        alert3 = _make_alert("orders-service", alert_name="alarm-3")

        result1 = dedup_app.handler(alert1, None)
        result2 = dedup_app.handler(alert2, None)

        assert result1["is_new"] is True
        assert result2["is_new"] is False
        assert result2["incident_id"] == result1["incident_id"]

        # Advance time past the TTL
        window_seconds = 5 * 60
        future = int(time.time()) + window_seconds + 10
        with patch("functions.dedup.app.time") if False else patch("time.time", return_value=future):
            dedup_app._table = None
            dedup_app._window_table = None
            result3 = dedup_app.handler(alert3, None)

        assert result3["is_new"] is True
        assert result3["incident_id"] != result1["incident_id"]



class TestDedupExpiry:
    """RC1-372: a fingerprint row whose ttl has passed but which DynamoDB's lazy
    TTL sweep has not yet deleted must not suppress the alert."""

    def _seed_fingerprint(self, dedup_app, dynamodb_tables, alert, ttl):
        from common.fingerprint import generate_fingerprint
        dedup_table, _, _ = dynamodb_tables
        dedup_table.put_item(Item={
            "fingerprint": generate_fingerprint(
                source=alert["source"],
                alert_name=alert["alert_name"],
                affected_service=alert["affected_service"],
            ),
            "first_seen_at": "2024-01-15T10:00:00Z",
            "source": alert["source"],
            "alert_name": alert["alert_name"],
            "ttl": ttl,
        })

    def test_expired_but_unswept_fingerprint_does_not_suppress(self, dedup_app, dynamodb_tables):
        alert = _make_alert("payments-service", alert_name="error-rate")
        self._seed_fingerprint(dedup_app, dynamodb_tables, alert, ttl=int(time.time()) - 3600)

        result = dedup_app.handler(alert, None)

        assert result is not None
        assert result["is_new"] is True
        dedup_table, _, _ = dynamodb_tables
        row = dedup_table.get_item(Key={"fingerprint": dedup_app.generate_fingerprint(
            source=alert["source"], alert_name=alert["alert_name"], affected_service=alert["affected_service"],
        )})["Item"]
        assert int(row["ttl"]) > int(time.time())  # row refreshed, not just tolerated

    def test_live_fingerprint_still_suppresses(self, dedup_app, dynamodb_tables):
        alert = _make_alert("payments-service", alert_name="error-rate")
        self._seed_fingerprint(dedup_app, dynamodb_tables, alert, ttl=int(time.time()) + 120)

        assert dedup_app.handler(alert, None) is None
        dedup_app._lambda_client.invoke.assert_not_called()

    def test_same_alert_again_after_window_is_accepted(self, dedup_app):
        alert = _make_alert("payments-service", alert_name="error-rate")
        first = dedup_app.handler(alert, None)
        assert first["is_new"] is True

        future = int(time.time()) + 5 * 60 + 10
        with patch("time.time", return_value=future):
            again = dedup_app.handler(alert, None)

        assert again is not None
        assert again["is_new"] is True
        assert again["incident_id"] != first["incident_id"]


class TestRecoveryClosesIncident:
    """RC1-374: a resolved alert closes the open incident it belongs to."""

    def _recovery(self, alert):
        return {**alert, "alert_id": alert["alert_id"] + "-ok", "status": "resolved", "severity": "low",
                "received_at": "2024-01-15T10:45:00Z"}

    def test_recovery_marks_incident_resolved_and_appends_alert(self, dedup_app, dynamodb_tables):
        alert = _make_alert("payments-service", alert_name="error-rate")
        opened = dedup_app.handler(alert, None)
        closed = dedup_app.handler(self._recovery(alert), None)

        assert closed["resolved"] is True
        assert closed["incident_id"] == opened["incident_id"]
        _, _, incident_table = dynamodb_tables
        item = incident_table.get_item(Key={"incident_id": opened["incident_id"]})["Item"]
        assert item["status"] == "resolved"
        assert item["resolved_at"]
        assert [a["status"] for a in item["source_alerts"]] == ["open", "resolved"]

    def test_recovery_hands_off_to_summarizer_with_flag(self, dedup_app):
        alert = _make_alert("payments-service", alert_name="error-rate")
        dedup_app.handler(alert, None)
        dedup_app.handler(self._recovery(alert), None)
        payloads = [json.loads(c[1]["Payload"]) for c in dedup_app._lambda_client.invoke.call_args_list]
        assert payloads[-1]["recovered"] is True

    def test_recovery_retires_window_and_fingerprint_rows(self, dedup_app, dynamodb_tables):
        alert = _make_alert("payments-service", alert_name="error-rate")
        dedup_app.handler(alert, None)
        dedup_app.handler(self._recovery(alert), None)
        dedup_table, window_table, _ = dynamodb_tables
        assert "Item" not in window_table.get_item(Key={"service_key": "payments-service"})
        fp = dedup_app.generate_fingerprint(source=alert["source"], alert_name=alert["alert_name"],
                                            affected_service=alert["affected_service"])
        assert "Item" not in dedup_table.get_item(Key={"fingerprint": fp})

    def test_new_alert_after_recovery_opens_a_new_incident(self, dedup_app):
        alert = _make_alert("payments-service", alert_name="error-rate")
        first = dedup_app.handler(alert, None)
        dedup_app.handler(self._recovery(alert), None)
        again = dedup_app.handler({**alert, "alert_id": "alert-payments-service-2"}, None)
        assert again["is_new"] is True
        assert again["incident_id"] != first["incident_id"]

    def test_recovery_without_open_incident_is_dropped(self, dedup_app, dynamodb_tables):
        alert = _make_alert("payments-service", alert_name="error-rate")
        assert dedup_app.handler(self._recovery(alert), None) is None
        _, _, incident_table = dynamodb_tables
        assert incident_table.scan()["Count"] == 0
        dedup_app._lambda_client.invoke.assert_not_called()

    def test_second_recovery_is_dropped(self, dedup_app):
        alert = _make_alert("payments-service", alert_name="error-rate")
        dedup_app.handler(alert, None)
        assert dedup_app.handler(self._recovery(alert), None)["resolved"] is True
        assert dedup_app.handler(self._recovery(alert), None) is None

    def test_recovery_of_other_alert_does_not_close_incident(self, dedup_app, dynamodb_tables):
        alert = _make_alert("payments-service", alert_name="error-rate")
        opened = dedup_app.handler(alert, None)
        other = _make_alert("payments-service", alert_name="latency")
        assert dedup_app.handler(self._recovery(other), None) is None
        _, _, incident_table = dynamodb_tables
        assert incident_table.get_item(Key={"incident_id": opened["incident_id"]})["Item"]["status"] == "open"
