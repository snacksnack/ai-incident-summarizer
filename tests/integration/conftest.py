import os
from unittest.mock import MagicMock, patch

import boto3
import pytest
from moto import mock_aws

from common import aws
from tests.conftest import load_function_module

ALERT_STATE_TABLE = "integ-alert-state-table"
INCIDENT_TABLE = "integ-incident-table"
SERVICE_REGISTRY_TABLE = "integ-service-registry-table"


@pytest.fixture()
def aws_credentials():
    os.environ["AWS_ACCESS_KEY_ID"] = "testing"
    os.environ["AWS_SECRET_ACCESS_KEY"] = "testing"
    os.environ["AWS_SECURITY_TOKEN"] = "testing"
    os.environ["AWS_SESSION_TOKEN"] = "testing"
    os.environ["AWS_DEFAULT_REGION"] = "us-east-1"


@pytest.fixture()
def dynamodb_tables(aws_credentials):
    with mock_aws():
        dynamodb = boto3.resource("dynamodb", region_name="us-east-1")

        state_table = dynamodb.create_table(
            TableName=ALERT_STATE_TABLE,
            KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )

        incident_table = dynamodb.create_table(
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

        dynamodb.create_table(
            TableName=SERVICE_REGISTRY_TABLE,
            KeySchema=[{"AttributeName": "affected_service", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "affected_service", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )

        yield state_table, incident_table


@pytest.fixture()
def dedup_app(dynamodb_tables, monkeypatch):
    """The ingest function against moto tables; `process_alert` is the entry
    point the normalized alerts go through."""
    monkeypatch.setenv("ALERT_STATE_TABLE_NAME", ALERT_STATE_TABLE)
    monkeypatch.setenv("INCIDENT_TABLE_NAME", INCIDENT_TABLE)
    monkeypatch.setenv("SERVICE_REGISTRY_TABLE_NAME", SERVICE_REGISTRY_TABLE)
    monkeypatch.setenv("SUMMARIZER_FUNCTION_NAME", "integ-summarizer")
    monkeypatch.setenv("CORRELATION_WINDOW_MINUTES", "5")

    aws.reset()  # inside mock_aws, so the resource it builds is moto's
    with patch("boto3.client"):
        app = load_function_module("ingest")
    app._lambda_client = MagicMock()

    yield app
