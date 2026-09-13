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
        results = [dedup_app.process_alert(a) for a in alerts]

        assert all(r is not None for r in results)
        incident_ids = {r["incident_id"] for r in results}
        assert len(incident_ids) == 1, f"Expected 1 incident, got {incident_ids}"
        assert results[-1]["alert_count"] == 4


class TestServiceIsolation:
    def test_different_services_produce_different_incidents(self, dedup_app):
        result_a = dedup_app.process_alert(_make_alert("payments-service"))
        result_b = dedup_app.process_alert(_make_alert("checkout-service"))

        assert result_a is not None
        assert result_b is not None
        assert result_a["incident_id"] != result_b["incident_id"]
        assert result_a["is_new"] is True
        assert result_b["is_new"] is True


class TestPersistence:
    def test_new_incident_written_to_dynamodb_with_status_open(self, dedup_app, dynamodb_tables):
        _, incident_table = dynamodb_tables
        result = dedup_app.process_alert(_make_alert("payments-service"))
        item = incident_table.get_item(Key={"incident_id": result["incident_id"]})["Item"]
        assert item["status"] == "open"
        assert item["affected_service"] == "payments-service"
        assert len(item["source_alerts"]) == 1

    def test_second_alert_appends_to_source_alerts(self, dedup_app, dynamodb_tables):
        _, incident_table = dynamodb_tables
        alert1 = _make_alert("payments-service", alert_name="alarm-1", n=0)
        alert2 = _make_alert("payments-service", alert_name="alarm-2", n=1)
        result1 = dedup_app.process_alert(alert1)
        dedup_app.process_alert(alert2)
        item = incident_table.get_item(Key={"incident_id": result1["incident_id"]})["Item"]
        assert len(item["source_alerts"]) == 2

    def test_idempotent_write_does_not_duplicate_incident(self, dedup_app, dynamodb_tables):
        _, incident_table = dynamodb_tables
        alert = _make_alert("payments-service")
        result = dedup_app.process_alert(alert)
        # Manually call _persist_incident again with is_new=True to simulate a race
        dedup_app.dedup._persist_incident(alert, {"incident_id": result["incident_id"], "is_new": True})
        item = incident_table.get_item(Key={"incident_id": result["incident_id"]})["Item"]
        assert len(item["source_alerts"]) == 1  # still only one


class TestWindowExpiry:
    def test_alert_after_window_expires_opens_new_incident(self, dedup_app):
        alert1 = _make_alert("orders-service", alert_name="alarm-1")
        alert2 = _make_alert("orders-service", alert_name="alarm-2")
        alert3 = _make_alert("orders-service", alert_name="alarm-3")

        result1 = dedup_app.process_alert(alert1)
        result2 = dedup_app.process_alert(alert2)

        assert result1["is_new"] is True
        assert result2["is_new"] is False
        assert result2["incident_id"] == result1["incident_id"]

        # Advance time past the TTL
        window_seconds = 5 * 60
        future = int(time.time()) + window_seconds + 10
        with patch("time.time", return_value=future):
            result3 = dedup_app.process_alert(alert3)

        assert result3["is_new"] is True
        assert result3["incident_id"] != result1["incident_id"]



class TestDedupExpiry:
    """RC1-372: a fingerprint row whose ttl has passed but which DynamoDB's lazy
    TTL sweep has not yet deleted must not suppress the alert."""

    def _seed_fingerprint(self, dedup_app, dynamodb_tables, alert, ttl):
        from common.fingerprint import generate_fingerprint
        state_table, _ = dynamodb_tables
        fingerprint = generate_fingerprint(
            source=alert["source"],
            alert_name=alert["alert_name"],
            affected_service=alert["affected_service"],
        )
        state_table.put_item(Item={
            "pk": f"fp#{fingerprint}",
            "fingerprint": fingerprint,
            "first_seen_at": "2024-01-15T10:00:00Z",
            "source": alert["source"],
            "alert_name": alert["alert_name"],
            "ttl": ttl,
        })

    def test_expired_but_unswept_fingerprint_does_not_suppress(self, dedup_app, dynamodb_tables):
        alert = _make_alert("payments-service", alert_name="error-rate")
        self._seed_fingerprint(dedup_app, dynamodb_tables, alert, ttl=int(time.time()) - 3600)

        result = dedup_app.process_alert(alert)

        assert result is not None
        assert result["is_new"] is True
        state_table, _ = dynamodb_tables
        row = state_table.get_item(Key={"pk": "fp#" + dedup_app.dedup.generate_fingerprint(
            source=alert["source"], alert_name=alert["alert_name"], affected_service=alert["affected_service"],
        )})["Item"]
        assert int(row["ttl"]) > int(time.time())  # row refreshed, not just tolerated

    def test_live_fingerprint_still_suppresses(self, dedup_app, dynamodb_tables):
        alert = _make_alert("payments-service", alert_name="error-rate")
        self._seed_fingerprint(dedup_app, dynamodb_tables, alert, ttl=int(time.time()) + 120)

        assert dedup_app.process_alert(alert) is None
        dedup_app._lambda_client.invoke.assert_not_called()

    def test_same_alert_again_after_window_is_accepted(self, dedup_app):
        alert = _make_alert("payments-service", alert_name="error-rate")
        first = dedup_app.process_alert(alert)
        assert first["is_new"] is True

        future = int(time.time()) + 5 * 60 + 10
        with patch("time.time", return_value=future):
            again = dedup_app.process_alert(alert)

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
        opened = dedup_app.process_alert(alert)
        closed = dedup_app.process_alert(self._recovery(alert))

        assert closed["resolved"] is True
        assert closed["incident_id"] == opened["incident_id"]
        _, incident_table = dynamodb_tables
        item = incident_table.get_item(Key={"incident_id": opened["incident_id"]})["Item"]
        assert item["status"] == "resolved"
        assert item["resolved_at"]
        assert [a["status"] for a in item["source_alerts"]] == ["open", "resolved"]

    def test_recovery_hands_off_to_summarizer_with_flag(self, dedup_app):
        alert = _make_alert("payments-service", alert_name="error-rate")
        dedup_app.process_alert(alert)
        dedup_app.process_alert(self._recovery(alert))
        payloads = [json.loads(c[1]["Payload"]) for c in dedup_app._lambda_client.invoke.call_args_list]
        assert payloads[-1]["recovered"] is True

    def test_recovery_retires_window_and_fingerprint_rows(self, dedup_app, dynamodb_tables):
        alert = _make_alert("payments-service", alert_name="error-rate")
        dedup_app.process_alert(alert)
        dedup_app.process_alert(self._recovery(alert))
        state_table, _ = dynamodb_tables
        assert "Item" not in state_table.get_item(Key={"pk": "window#payments-service"})
        fp = dedup_app.dedup.generate_fingerprint(source=alert["source"], alert_name=alert["alert_name"],
                                            affected_service=alert["affected_service"])
        assert "Item" not in state_table.get_item(Key={"pk": f"fp#{fp}"})

    def test_new_alert_after_recovery_opens_a_new_incident(self, dedup_app):
        alert = _make_alert("payments-service", alert_name="error-rate")
        first = dedup_app.process_alert(alert)
        dedup_app.process_alert(self._recovery(alert))
        again = dedup_app.process_alert({**alert, "alert_id": "alert-payments-service-2"})
        assert again["is_new"] is True
        assert again["incident_id"] != first["incident_id"]

    def test_recovery_without_open_incident_is_dropped(self, dedup_app, dynamodb_tables):
        alert = _make_alert("payments-service", alert_name="error-rate")
        assert dedup_app.process_alert(self._recovery(alert)) is None
        _, incident_table = dynamodb_tables
        assert incident_table.scan()["Count"] == 0
        dedup_app._lambda_client.invoke.assert_not_called()

    def test_second_recovery_is_dropped(self, dedup_app):
        alert = _make_alert("payments-service", alert_name="error-rate")
        dedup_app.process_alert(alert)
        assert dedup_app.process_alert(self._recovery(alert))["resolved"] is True
        assert dedup_app.process_alert(self._recovery(alert)) is None

    def test_recovery_of_other_alert_does_not_close_incident(self, dedup_app, dynamodb_tables):
        alert = _make_alert("payments-service", alert_name="error-rate")
        opened = dedup_app.process_alert(alert)
        other = _make_alert("payments-service", alert_name="latency")
        assert dedup_app.process_alert(self._recovery(other)) is None
        _, incident_table = dynamodb_tables
        assert incident_table.get_item(Key={"incident_id": opened["incident_id"]})["Item"]["status"] == "open"
