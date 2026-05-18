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
