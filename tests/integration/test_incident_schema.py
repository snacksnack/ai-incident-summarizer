"""
Integration tests for the IncidentTable schema.

Verifies that both GSIs are created correctly and that the canonical
"all open incidents for service X" query returns the right results.
Uses moto to spin up a real DynamoDB environment in-process.
"""
import time
import uuid
from datetime import datetime, timezone, timedelta

import boto3
import pytest
from boto3.dynamodb.conditions import Attr, Key
from moto import mock_aws

INCIDENT_TABLE = "integ-incident-table"


@pytest.fixture()
def aws_credentials(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SECURITY_TOKEN", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")


@pytest.fixture()
def incident_table(aws_credentials):
    with mock_aws():
        dynamodb = boto3.resource("dynamodb", region_name="us-east-1")
        table = dynamodb.create_table(
            TableName=INCIDENT_TABLE,
            KeySchema=[{"AttributeName": "incident_id", "KeyType": "HASH"}],
            AttributeDefinitions=[
                {"AttributeName": "incident_id", "AttributeType": "S"},
                {"AttributeName": "affected_service", "AttributeType": "S"},
                {"AttributeName": "status", "AttributeType": "S"},
                {"AttributeName": "created_at", "AttributeType": "S"},
            ],
            GlobalSecondaryIndexes=[
                {
                    "IndexName": "service-created-index",
                    "KeySchema": [
                        {"AttributeName": "affected_service", "KeyType": "HASH"},
                        {"AttributeName": "created_at", "KeyType": "RANGE"},
                    ],
                    "Projection": {"ProjectionType": "ALL"},
                },
                {
                    "IndexName": "status-created-index",
                    "KeySchema": [
                        {"AttributeName": "status", "KeyType": "HASH"},
                        {"AttributeName": "created_at", "KeyType": "RANGE"},
                    ],
                    "Projection": {"ProjectionType": "ALL"},
                },
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        yield table


def _put_incident(table, service, status, created_at, ttl=None):
    incident_id = str(uuid.uuid4())
    item = {
        "incident_id": incident_id,
        "affected_service": service,
        "status": status,
        "severity": "high",
        "created_at": created_at,
        "source_alerts": [],
    }
    if ttl is not None:
        item["ttl"] = ttl
    table.put_item(Item=item)
    return incident_id


class TestServiceCreatedIndex:
    def test_query_returns_only_matching_service(self, incident_table):
        _put_incident(incident_table, "payments-service", "open", "2024-01-15T10:00:00Z")
        _put_incident(incident_table, "checkout-service", "open", "2024-01-15T10:01:00Z")

        result = incident_table.query(
            IndexName="service-created-index",
            KeyConditionExpression=Key("affected_service").eq("payments-service"),
        )
        assert result["Count"] == 1
        assert result["Items"][0]["affected_service"] == "payments-service"

    def test_query_with_status_filter_returns_open_only(self, incident_table):
        _put_incident(incident_table, "payments-service", "open", "2024-01-15T10:00:00Z")
        _put_incident(incident_table, "payments-service", "resolved", "2024-01-15T10:01:00Z")
        _put_incident(incident_table, "payments-service", "open", "2024-01-15T10:02:00Z")

        result = incident_table.query(
            IndexName="service-created-index",
            KeyConditionExpression=Key("affected_service").eq("payments-service"),
            FilterExpression=Attr("status").eq("open"),
        )
        assert result["Count"] == 2
        assert all(item["status"] == "open" for item in result["Items"])

    def test_results_sorted_by_created_at(self, incident_table):
        _put_incident(incident_table, "payments-service", "open", "2024-01-15T10:00:00Z")
        _put_incident(incident_table, "payments-service", "open", "2024-01-15T10:02:00Z")
        _put_incident(incident_table, "payments-service", "open", "2024-01-15T10:01:00Z")

        result = incident_table.query(
            IndexName="service-created-index",
            KeyConditionExpression=Key("affected_service").eq("payments-service"),
            ScanIndexForward=False,
        )
        timestamps = [item["created_at"] for item in result["Items"]]
        assert timestamps == sorted(timestamps, reverse=True)


class TestStatusCreatedIndex:
    def test_query_all_open_incidents(self, incident_table):
        _put_incident(incident_table, "payments-service", "open", "2024-01-15T10:00:00Z")
        _put_incident(incident_table, "checkout-service", "open", "2024-01-15T10:01:00Z")
        _put_incident(incident_table, "orders-service", "resolved", "2024-01-15T10:02:00Z")

        result = incident_table.query(
            IndexName="status-created-index",
            KeyConditionExpression=Key("status").eq("open"),
        )
        assert result["Count"] == 2
        assert all(item["status"] == "open" for item in result["Items"])

    def test_query_resolved_incidents(self, incident_table):
        _put_incident(incident_table, "payments-service", "resolved", "2024-01-15T10:00:00Z")
        _put_incident(incident_table, "checkout-service", "open", "2024-01-15T10:01:00Z")

        result = incident_table.query(
            IndexName="status-created-index",
            KeyConditionExpression=Key("status").eq("resolved"),
        )
        assert result["Count"] == 1
        assert result["Items"][0]["affected_service"] == "payments-service"


class TestTtl:
    def test_ttl_attribute_stored_correctly(self, incident_table):
        ttl_value = int(time.time()) + 30 * 24 * 3600  # 30 days
        incident_id = _put_incident(
            incident_table, "payments-service", "resolved", "2024-01-15T10:00:00Z", ttl=ttl_value
        )
        item = incident_table.get_item(Key={"incident_id": incident_id})["Item"]
        assert item["ttl"] == ttl_value


class TestSampleQuery:
    def test_all_open_incidents_for_service_x(self, incident_table):
        """Canonical acceptance-criteria query: all open incidents for a given service."""
        target_service = "payments-service"
        open_id_1 = _put_incident(incident_table, target_service, "open", "2024-01-15T09:00:00Z")
        open_id_2 = _put_incident(incident_table, target_service, "open", "2024-01-15T10:00:00Z")
        _put_incident(incident_table, target_service, "resolved", "2024-01-15T08:00:00Z")
        _put_incident(incident_table, "other-service", "open", "2024-01-15T10:00:00Z")

        result = incident_table.query(
            IndexName="service-created-index",
            KeyConditionExpression=Key("affected_service").eq(target_service),
            FilterExpression=Attr("status").eq("open"),
            ScanIndexForward=False,
        )

        returned_ids = {item["incident_id"] for item in result["Items"]}
        assert returned_ids == {open_id_1, open_id_2}
        # newest first
        assert result["Items"][0]["created_at"] > result["Items"][1]["created_at"]
