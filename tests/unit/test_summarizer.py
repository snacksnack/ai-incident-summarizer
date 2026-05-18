import importlib
import json
import sys
from unittest.mock import MagicMock, patch

import pytest

INCIDENT_TABLE = "test-incident-table"
API_KEY_SECRET_ARN = "arn:aws:secretsmanager:us-east-1:123456789012:secret:anthropic-key"
API_KEY = "sk-ant-test-key"
MODEL_ID = "claude-sonnet-4-6"

INCIDENT = {
    "incident_id": "inc-123",
    "affected_service": "payments-service",
    "severity": "high",
    "status": "open",
    "source_alerts": [
        {
            "alert_id": "a1",
            "source": "cloudwatch",
            "alert_name": "high-error-rate",
            "severity": "high",
            "status": "open",
            "received_at": "2024-01-15T10:00:00Z",
        },
        {
            "alert_id": "a2",
            "source": "datadog",
            "alert_name": "latency-spike",
            "severity": "high",
            "status": "open",
            "received_at": "2024-01-15T10:02:00Z",
        },
    ],
    "created_at": "2024-01-15T10:00:00Z",
}

LLM_RESPONSE = {
    "summary": "Payments service is experiencing high error rates and latency spikes.",
    "likely_cause": "Possible database connection pool exhaustion.",
    "next_step": "Check database connection metrics and restart if needed.",
}


def _load_summarizer():
    for mod in list(sys.modules):
        if mod == "app" or mod.startswith("app."):
            del sys.modules[mod]
    sys.path.insert(0, "functions/summarizer")
    import app
    importlib.reload(app)
    return app


def _mock_anthropic(response: dict = None):
    mock = MagicMock()
    mock.return_value.messages.create.return_value.content = [
        MagicMock(text=json.dumps(response or LLM_RESPONSE))
    ]
    return mock


@pytest.fixture()
def summarizer(monkeypatch):
    monkeypatch.setenv("INCIDENT_TABLE_NAME", INCIDENT_TABLE)
    monkeypatch.setenv("ANTHROPIC_API_KEY_SECRET_ARN", API_KEY_SECRET_ARN)
    monkeypatch.setenv("MODEL_ID", MODEL_ID)

    mock_table = MagicMock()
    mock_table.get_item.return_value = {"Item": INCIDENT}
    mock_table.update_item.return_value = {}

    mock_secrets = MagicMock()
    mock_secrets.get_secret_value.return_value = {"SecretString": API_KEY}

    with patch("boto3.resource"), patch("boto3.client"):
        app = _load_summarizer()
        app._incident_table = mock_table
        app._secrets_client = mock_secrets
        app._api_key_cache.clear()
        yield app, mock_table, mock_secrets


# ── Handler tests ─────────────────────────────────────────────────────────────

class TestHandler:
    def test_calls_claude_with_model_from_env_var(self, summarizer):
        app, _, _ = summarizer
        mock_anthropic = _mock_anthropic()
        with patch("anthropic.Anthropic", mock_anthropic):
            app.handler({"incident_id": "inc-123"}, None)
        call_kwargs = mock_anthropic.return_value.messages.create.call_args[1]
        assert call_kwargs["model"] == MODEL_ID

    def test_writes_structured_json_to_dynamodb(self, summarizer):
        app, mock_table, _ = summarizer
        with patch("anthropic.Anthropic", _mock_anthropic()):
            app.handler({"incident_id": "inc-123"}, None)
        mock_table.update_item.assert_called_once()
        stored = json.loads(
            mock_table.update_item.call_args[1]["ExpressionAttributeValues"][":s"]
        )
        assert "summary" in stored
        assert "likely_cause" in stored
        assert "next_step" in stored

    def test_returns_incident_id_and_summary(self, summarizer):
        app, _, _ = summarizer
        with patch("anthropic.Anthropic", _mock_anthropic()):
            result = app.handler({"incident_id": "inc-123"}, None)
        assert result["incident_id"] == "inc-123"
        assert "llm_summary" in result
        parsed = json.loads(result["llm_summary"])
        assert parsed == LLM_RESPONSE

    def test_fallback_written_to_dynamodb_on_llm_error(self, summarizer):
        app, mock_table, _ = summarizer
        mock_anthropic = MagicMock()
        mock_anthropic.return_value.messages.create.side_effect = Exception("API error")
        with patch("anthropic.Anthropic", mock_anthropic):
            result = app.handler({"incident_id": "inc-123"}, None)
        assert result is not None
        mock_table.update_item.assert_called_once()
        stored = json.loads(
            mock_table.update_item.call_args[1]["ExpressionAttributeValues"][":s"]
        )
        assert "summary" in stored
        assert "likely_cause" in stored
        assert "next_step" in stored

    def test_returns_none_when_no_incident_id(self, summarizer):
        app, _, _ = summarizer
        result = app.handler({}, None)
        assert result is None

    def test_returns_none_when_incident_not_found(self, summarizer):
        app, mock_table, _ = summarizer
        mock_table.get_item.return_value = {}
        result = app.handler({"incident_id": "nonexistent"}, None)
        assert result is None


# ── API key caching tests ─────────────────────────────────────────────────────

class TestApiKey:
    def test_api_key_retrieved_from_secrets_manager(self, summarizer):
        app, _, mock_secrets = summarizer
        with patch("anthropic.Anthropic", _mock_anthropic()):
            app.handler({"incident_id": "inc-123"}, None)
        mock_secrets.get_secret_value.assert_called_once_with(SecretId=API_KEY_SECRET_ARN)

    def test_api_key_cached_across_calls(self, summarizer):
        app, _, mock_secrets = summarizer
        with patch("anthropic.Anthropic", _mock_anthropic()):
            app.handler({"incident_id": "inc-123"}, None)
            app.handler({"incident_id": "inc-123"}, None)
        assert mock_secrets.get_secret_value.call_count == 1


# ── Prompt tests ──────────────────────────────────────────────────────────────

class TestPrompt:
    def test_prompt_includes_affected_service(self, summarizer):
        app, _, _ = summarizer
        assert "payments-service" in app._build_prompt(INCIDENT)

    def test_prompt_includes_severity(self, summarizer):
        app, _, _ = summarizer
        assert "high" in app._build_prompt(INCIDENT)

    def test_prompt_includes_alert_names(self, summarizer):
        app, _, _ = summarizer
        prompt = app._build_prompt(INCIDENT)
        assert "high-error-rate" in prompt
        assert "latency-spike" in prompt

    def test_prompt_includes_time_range(self, summarizer):
        app, _, _ = summarizer
        prompt = app._build_prompt(INCIDENT)
        assert "2024-01-15T10:00:00Z" in prompt
        assert "2024-01-15T10:02:00Z" in prompt
