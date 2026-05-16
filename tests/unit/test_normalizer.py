import json
import sys
import importlib

import pytest

# Realistic CloudWatch alarm state change event as delivered by EventBridge
CLOUDWATCH_ALARM_EVENT = {
    "version": "0",
    "id": "c4c1c1c9-6542-e61b-6ef0-8c4d36933a92",
    "source": "aws.cloudwatch",
    "account": "123456789012",
    "time": "2019-10-22T18:43:48Z",
    "region": "us-east-1",
    "detail-type": "CloudWatch Alarm State Change",
    "detail": {
        "alarmName": "my-alarm",
        "state": {
            "value": "ALARM",
            "reason": "Threshold Crossed: 1 out of the last 1 datapoints was greater than the threshold (0.0).",
            "reasonData": "{}",
            "timestamp": "2019-10-22T18:43:48.341+0000",
        },
        "previousState": {
            "value": "OK",
            "reason": "Threshold Crossed",
            "timestamp": "2019-10-22T18:42:06.479+0000",
        },
        "configuration": {
            "description": "Test alarm",
            "metrics": [
                {
                    "id": "m1",
                    "metricStat": {
                        "metric": {
                            "namespace": "AWS/Lambda",
                            "name": "Errors",
                            "dimensions": {"FunctionName": "my-function"},
                        },
                        "period": 60,
                        "stat": "Sum",
                    },
                }
            ],
        },
    },
}

# Webhook envelope as forwarded by the webhook_receiver Lambda
WEBHOOK_ENVELOPE_EVENT = {
    "source": "datadog",
    "raw_payload": {"id": "abc-123", "title": "Error rate above threshold"},
    "received_at": "2024-01-15T10:30:00+00:00",
    "path": "/webhook/datadog",
}


@pytest.fixture(autouse=True)
def fresh_module():
    for mod in list(sys.modules):
        if "normalizer" in mod and "webhook" not in mod:
            del sys.modules[mod]
    yield


@pytest.fixture()
def normalizer():
    sys.path.insert(0, "functions/normalizer")
    import app
    importlib.reload(app)
    return app


def test_handles_cloudwatch_alarm_event(normalizer):
    result = normalizer.handler(CLOUDWATCH_ALARM_EVENT, None)
    assert result is None


def test_handles_webhook_envelope_event(normalizer):
    result = normalizer.handler(WEBHOOK_ENVELOPE_EVENT, None)
    assert result is None
