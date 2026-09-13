"""Webhook authentication at the edge of the ingest function. What happens to
an accepted envelope is stubbed (`app._accept`) and covered in test_ingest.py."""
import hashlib
import hmac
import json
from unittest.mock import MagicMock, patch

import pytest

from common import aws
from tests.conftest import load_function_module

GITHUB_SECRET = "github-test-secret"
DATADOG_SECRET = "datadog-test-secret"
GITHUB_SECRET_ARN = "arn:aws:secretsmanager:us-east-1:123456789012:secret:github-webhook"
DATADOG_SECRET_ARN = "arn:aws:secretsmanager:us-east-1:123456789012:secret:datadog-webhook"


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


@pytest.fixture()
def mock_aws(monkeypatch):
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET_ARN", GITHUB_SECRET_ARN)
    monkeypatch.setenv("DATADOG_WEBHOOK_SECRET_ARN", DATADOG_SECRET_ARN)

    aws.reset()
    mock_secrets = MagicMock()
    mock_secrets.get_secret_value.side_effect = lambda SecretId, **_: {
        GITHUB_SECRET_ARN: {"SecretString": GITHUB_SECRET},
        DATADOG_SECRET_ARN: {"SecretString": DATADOG_SECRET},
    }[SecretId]
    aws._secrets_client = mock_secrets

    with patch("boto3.client"):
        app = load_function_module("ingest")
    accept = MagicMock(return_value=app.webhook.response(202, {"status": "accepted"}))
    app._accept = accept
    yield app, accept, mock_secrets


def _envelope(accept: MagicMock) -> dict:
    accept.assert_called_once()
    return accept.call_args[0][0]


GITHUB_BODY = json.dumps({"action": "completed", "workflow_run": {"conclusion": "failure"}})
DATADOG_BODY = json.dumps({"id": "abc-123", "title": "Error rate above threshold", "alert_type": "error"})


class TestGithubWebhook:
    def test_valid_signature_returns_202(self, mock_aws):
        app, accept, _ = mock_aws
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

    def test_hands_envelope_to_ingest(self, mock_aws):
        app, accept, _ = mock_aws
        sig = _github_sig(GITHUB_BODY)
        event = _make_event("/webhook/github", GITHUB_BODY, {"x-hub-signature-256": sig})
        app.handler(event, None)
        payload = _envelope(accept)
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

    def test_hands_envelope_to_ingest(self, mock_aws):
        app, accept, _ = mock_aws
        event = _make_event("/webhook/datadog", DATADOG_BODY, {"x-webhook-secret": DATADOG_SECRET})
        app.handler(event, None)
        payload = _envelope(accept)
        assert payload["source"] == "datadog"


class TestGitHubEventHeader:
    def test_github_event_name_is_forwarded(self, mock_aws):
        app, accept, _ = mock_aws
        sig = _github_sig(GITHUB_BODY)
        event = _make_event("/webhook/github", GITHUB_BODY, {"x-hub-signature-256": sig, "x-github-event": "workflow_job"})
        app.handler(event, None)
        payload = _envelope(accept)
        assert payload["github_event"] == "workflow_job"

    def test_datadog_envelope_has_no_github_event(self, mock_aws):
        app, accept, _ = mock_aws
        event = _make_event("/webhook/datadog", DATADOG_BODY, {"x-webhook-secret": DATADOG_SECRET})
        app.handler(event, None)
        payload = _envelope(accept)
        assert "github_event" not in payload


class TestUnknownPath:
    def test_unknown_path_returns_404(self, mock_aws):
        app, _, _ = mock_aws
        event = _make_event("/webhook/unknown", "{}", {})
        response = app.handler(event, None)
        assert response["statusCode"] == 404


class TestStagePrefix:
    """RC1-370: API Gateway prefixes rawPath with the stage name in production."""

    def test_stage_prefixed_raw_path_is_accepted(self, mock_aws):
        app, _, _ = mock_aws
        event = _make_event("/prod/webhook/datadog", DATADOG_BODY, {"x-webhook-secret": DATADOG_SECRET})
        event["requestContext"] = {"stage": "prod", "http": {"method": "POST", "path": "/prod/webhook/datadog"}}
        response = app.handler(event, None)
        assert response["statusCode"] == 202

    def test_route_key_wins_over_raw_path(self, mock_aws):
        app, _, _ = mock_aws
        event = _make_event("/prod/webhook/github", DATADOG_BODY, {"x-webhook-secret": DATADOG_SECRET})
        event["routeKey"] = "POST /webhook/datadog"
        response = app.handler(event, None)
        assert response["statusCode"] == 202

    def test_forwarded_envelope_path_has_no_stage(self, mock_aws):
        app, accept, _ = mock_aws
        event = _make_event("/prod/webhook/datadog", DATADOG_BODY, {"x-webhook-secret": DATADOG_SECRET})
        event["routeKey"] = "POST /webhook/datadog"
        app.handler(event, None)
        payload = _envelope(accept)
        assert payload["path"] == "/webhook/datadog"
        assert payload["source"] == "datadog"

    def test_default_stage_is_not_stripped(self, mock_aws):
        app, _, _ = mock_aws
        event = _make_event("/webhook/datadog", DATADOG_BODY, {"x-webhook-secret": DATADOG_SECRET})
        event["requestContext"] = {"stage": "$default"}
        assert app.handler(event, None)["statusCode"] == 202

    def test_unknown_path_is_logged(self, mock_aws, caplog):
        app, _, _ = mock_aws
        with caplog.at_level("WARNING"):
            app.handler(_make_event("/prod/webhook/nope", "{}", {}), None)
        assert "unknown path" in caplog.text


class TestSecretShape:
    """RC1-370: the stored secrets are one-key JSON objects; the bare value is what
    GitHub signs with and Datadog sends."""

    def test_json_wrapped_datadog_secret_matches_bare_header(self, mock_aws, monkeypatch):
        app, _, mock_secrets = mock_aws
        aws._secrets.clear()
        mock_secrets.get_secret_value.side_effect = None
        mock_secrets.get_secret_value.return_value = {"SecretString": json.dumps({"dd-webhook-secret": DATADOG_SECRET})}
        event = _make_event("/webhook/datadog", DATADOG_BODY, {"x-webhook-secret": DATADOG_SECRET})
        assert app.handler(event, None)["statusCode"] == 202

    def test_json_wrapped_github_secret_verifies_bare_signature(self, mock_aws):
        app, _, mock_secrets = mock_aws
        aws._secrets.clear()
        mock_secrets.get_secret_value.side_effect = None
        mock_secrets.get_secret_value.return_value = {"SecretString": json.dumps({"gh-webhook-secret": GITHUB_SECRET})}
        event = _make_event("/webhook/github", GITHUB_BODY, {"x-hub-signature-256": _github_sig(GITHUB_BODY)})
        assert app.handler(event, None)["statusCode"] == 202

    def test_plain_secret_still_works(self, mock_aws):
        app, _, _ = mock_aws
        event = _make_event("/webhook/datadog", DATADOG_BODY, {"x-webhook-secret": DATADOG_SECRET})
        assert app.handler(event, None)["statusCode"] == 202

    def test_multi_key_json_is_not_unwrapped(self, mock_aws):
        app, _, _ = mock_aws
        assert app.webhook.secret_value('{"a": "1", "b": "2"}') == '{"a": "1", "b": "2"}'

    def test_malformed_json_is_used_verbatim(self, mock_aws):
        app, _, _ = mock_aws
        assert app.webhook.secret_value("{not json") == "{not json"
