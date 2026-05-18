import importlib
import os
import sys
from unittest.mock import MagicMock

import boto3
import pytest
from moto import mock_aws

DEDUP_TABLE = "integ-dedup-table"
CORRELATION_TABLE = "integ-correlation-table"
INCIDENT_TABLE = "integ-incident-table"


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

        dedup_table = dynamodb.create_table(
            TableName=DEDUP_TABLE,
            KeySchema=[{"AttributeName": "fingerprint", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "fingerprint", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )

        window_table = dynamodb.create_table(
            TableName=CORRELATION_TABLE,
            KeySchema=[{"AttributeName": "service_key", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "service_key", "AttributeType": "S"}],
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

        yield dedup_table, window_table, incident_table


@pytest.fixture()
def dedup_app(dynamodb_tables, monkeypatch):
    monkeypatch.setenv("DEDUP_TABLE_NAME", DEDUP_TABLE)
    monkeypatch.setenv("CORRELATION_TABLE_NAME", CORRELATION_TABLE)
    monkeypatch.setenv("INCIDENT_TABLE_NAME", INCIDENT_TABLE)
    monkeypatch.setenv("SUMMARIZER_FUNCTION_NAME", "integ-summarizer")
    monkeypatch.setenv("CORRELATION_WINDOW_MINUTES", "5")

    for mod in list(sys.modules):
        if mod == "app" or mod.startswith("app."):
            del sys.modules[mod]

    sys.path.insert(0, "functions/dedup")
    import app
    importlib.reload(app)

    app._table = None
    app._window_table = None
    app._incident_table = None
    app._lambda_client = MagicMock()

    yield app
