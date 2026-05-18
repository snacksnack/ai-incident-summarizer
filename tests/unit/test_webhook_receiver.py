import hashlib
import hmac
import importlib
import json
import sys
from unittest.mock import MagicMock, patch

import pytest

GITHUB_SECRET = "github-test-secret"
DATADOG_SECRET = "datadog-test-secret"
GITHUB_SECRET_ARN = "arn:aws:secretsmanager:us-east-1:123456789012:secret:github-webhook"
DATADOG_SECRET_ARN = "arn:aws:secretsmanager:us-east-1:123456789012:secret:datadog-webhook"
NORMALIZER_FUNCTION_NAME = "normalizer-function"


def _github_sig(body: str, secret: str = GITHUB_SECRET) -> str:
    digest = hmac.new(secret.encode(), body.encode(), hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def _make_event(path: str, body: str, headers: dict) -> dict:
    return {
        "rawPath": path,
        "headers": headers,
        "body": body,
        "isBase64Encoded": False,
    }


@pytest.fixture(autouse=True)
def fresh_module():
    """Reload the module each test so the secret cache is cleared."""
    for mod in list(sys.modules):
        if "webhook_receiver" in mod:
            del sys.modules[mod]
    yield


@pytest.fixture()
def mock_aws(monkeypatch):
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET_ARN", GITHUB_SECRET_ARN)
    monkeypatch.setenv("DATADOG_WEBHOOK_SECRET_ARN", DATADOG_SECRET_ARN)
    monkeypatch.setenv("NORMALIZER_FUNCTION_NAME", NORMALIZER_FUNCTION_NAME)

    mock_secrets = MagicMock()
    mock_secrets.get_secret_value.side_effect = lambda SecretId, **_: {
        GITHUB_SECRET_ARN: {"SecretString": GITHUB_SECRET},
        DATADOG_SECRET_ARN: {"SecretString": DATADOG_SECRET},
    }[SecretId]

    mock_lambda = MagicMock()
    mock_lambda.invoke.return_value = {}

    with patch("boto3.client") as mock_boto3:
        def client_factory(service, **kwargs):
            if service == "secretsmanager":
                return mock_secrets
            if service == "lambda":
                return mock_lambda
            return MagicMock()

        mock_boto3.side_effect = client_factory
        sys.path.insert(0, "functions/webhook_receiver")
        import app
        importlib.reload(app)
        yield app, mock_lambda, mock_secrets


GITHUB_BODY = json.dumps({"action": "completed", "workflow_run": {"conclusion": "failure"}})
DATADOG_BODY = json.dumps({"id": "abc-123", "title": "Error rate above threshold", "alert_type": "error"})


class TestGithubWebhook:
    def test_valid_signature_returns_202(self, mock_aws):
        app, mock_lambda, _ = mock_aws
        sig = _github_sig(GITHUB_BODY)
        event = _make_event("/webhook/github", GITHUB_BODY, {"x-hub-signature-256": sig})
        response = app.handler(event, None)
        assert response["statusCode"] == 202
        assert json.loads(response["body"]) == {"status": "accepted"}

    def test_invalid_signature_returns_401(self, mock_aws):
        app, _, _ = mock_aws
        event = _make_event("/webhook/github", GITHUB_BODY, {"x-hub-signature-256": "sha256=badhash"})
        response = app.handler(event, None)
        assert response["statusCode"] == 401

    def test_missing_signature_header_returns_401(self, mock_aws):
        app, _, _ = mock_aws
        event = _make_event("/webhook/github", GITHUB_BODY, {})
        response = app.handler(event, None)
        assert response["statusCode"] == 401

    def test_valid_signature_wrong_secret_returns_401(self, mock_aws):
        app, _, _ = mock_aws
        sig = _github_sig(GITHUB_BODY, secret="wrong-secret")
        event = _make_event("/webhook/github", GITHUB_BODY, {"x-hub-signature-256": sig})
        response = app.handler(event, None)
        assert response["statusCode"] == 401

    def test_malformed_json_returns_400(self, mock_aws):
        app, _, _ = mock_aws
        bad_body = "not-json"
        sig = _github_sig(bad_body)
        event = _make_event("/webhook/github", bad_body, {"x-hub-signature-256": sig})
        response = app.handler(event, None)
        assert response["statusCode"] == 400

    def test_forwards_envelope_to_normalizer(self, mock_aws):
        app, mock_lambda, _ = mock_aws
        sig = _github_sig(GITHUB_BODY)
        event = _make_event("/webhook/github", GITHUB_BODY, {"x-hub-signature-256": sig})
        app.handler(event, None)
        mock_lambda.invoke.assert_called_once()
        call_kwargs = mock_lambda.invoke.call_args[1]
        assert call_kwargs["FunctionName"] == NORMALIZER_FUNCTION_NAME
        assert call_kwargs["InvocationType"] == "Event"
        payload = json.loads(call_kwargs["Payload"])
        assert payload["source"] == "github"
        assert payload["raw_payload"] == json.loads(GITHUB_BODY)
        assert "received_at" in payload


class TestDatadogWebhook:
    def test_valid_secret_header_returns_202(self, mock_aws):
        app, _, _ = mock_aws
        event = _make_event("/webhook/datadog", DATADOG_BODY, {"x-webhook-secret": DATADOG_SECRET})
        response = app.handler(event, None)
        assert response["statusCode"] == 202

    def test_wrong_secret_header_returns_401(self, mock_aws):
        app, _, _ = mock_aws
        event = _make_event("/webhook/datadog", DATADOG_BODY, {"x-webhook-secret": "wrong-secret"})
        response = app.handler(event, None)
        assert response["statusCode"] == 401

    def test_missing_secret_header_returns_401(self, mock_aws):
        app, _, _ = mock_aws
        event = _make_event("/webhook/datadog", DATADOG_BODY, {})
        response = app.handler(event, None)
        assert response["statusCode"] == 401

    def test_malformed_json_returns_400(self, mock_aws):
        app, _, _ = mock_aws
        event = _make_event("/webhook/datadog", "{invalid", {"x-webhook-secret": DATADOG_SECRET})
        response = app.handler(event, None)
        assert response["statusCode"] == 400

    def test_forwards_envelope_to_normalizer(self, mock_aws):
        app, mock_lambda, _ = mock_aws
        event = _make_event("/webhook/datadog", DATADOG_BODY, {"x-webhook-secret": DATADOG_SECRET})
        app.handler(event, None)
        payload = json.loads(mock_lambda.invoke.call_args[1]["Payload"])
        assert payload["source"] == "datadog"


class TestUnknownPath:
    def test_unknown_path_returns_404(self, mock_aws):
        app, _, _ = mock_aws
        event = _make_event("/webhook/unknown", "{}", {})
        response = app.handler(event, None)
        assert response["statusCode"] == 404
