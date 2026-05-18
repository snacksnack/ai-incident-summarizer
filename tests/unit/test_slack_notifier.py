import importlib
import json
import sys
from unittest.mock import MagicMock, patch

import pytest
from slack_sdk.errors import SlackApiError

INCIDENT_TABLE = "test-incident-table"
SLACK_TOKEN_ARN = "arn:aws:secretsmanager:us-east-1:123456789012:secret:slack-token"
SLACK_TOKEN = "xoxb-test-token"
SLACK_CHANNEL = "C01234567"

INCIDENT = {
    "incident_id": "inc-123",
    "affected_service": "payments-service",
    "severity": "high",
    "status": "open",
    "created_at": "2024-01-15T10:00:00Z",
    "source_alerts": [
        {"alert_id": "a1", "alert_name": "high-error-rate", "source": "cloudwatch"},
        {"alert_id": "a2", "alert_name": "latency-spike", "source": "datadog"},
    ],
    "llm_summary": json.dumps({
        "summary": "Payments service is down.",
        "likely_cause": "Database overload.",
        "next_step": "Restart the DB connection pool.",
    }),
}

INCIDENT_NO_SUMMARY = {**INCIDENT, "llm_summary": None}
INCIDENT_WITH_THREAD = {**INCIDENT, "slack_thread_id": "1705312800.123456"}


def _load_slack_notifier():
    for mod in list(sys.modules):
        if mod == "app" or mod.startswith("app."):
            del sys.modules[mod]
    sys.path.insert(0, "functions/slack_notifier")
    import app
    importlib.reload(app)
    return app


def _slack_api_error(error_code: str = "channel_not_found"):
    response = {"ok": False, "error": error_code, "headers": {}}
    return SlackApiError(message=error_code, response=response)


JIRA_CREATOR_FUNCTION = "test-jira-creator"


@pytest.fixture()
def notifier(monkeypatch):
    monkeypatch.setenv("INCIDENT_TABLE_NAME", INCIDENT_TABLE)
    monkeypatch.setenv("SLACK_BOT_TOKEN_SECRET_ARN", SLACK_TOKEN_ARN)
    monkeypatch.setenv("SLACK_CHANNEL_ID", SLACK_CHANNEL)
    monkeypatch.setenv("JIRA_CREATOR_FUNCTION_NAME", JIRA_CREATOR_FUNCTION)

    mock_table = MagicMock()
    mock_table.get_item.return_value = {"Item": INCIDENT}
    mock_table.update_item.return_value = {}

    mock_secrets = MagicMock()
    mock_secrets.get_secret_value.return_value = {"SecretString": SLACK_TOKEN}

    mock_lambda_client = MagicMock()

    with patch("boto3.resource"), patch("boto3.client"):
        app = _load_slack_notifier()
        app._incident_table = mock_table
        app._secrets_client = mock_secrets
        app._lambda_client = mock_lambda_client
        app._token_cache.clear()
        yield app, mock_table, mock_secrets


# ── Handler routing ───────────────────────────────────────────────────────────

class TestHandler:
    def test_returns_none_when_no_incident_id(self, notifier):
        app, _, _ = notifier
        mock_slack = MagicMock()
        with patch("app.WebClient", return_value=mock_slack):
            result = app.handler({}, None)
        assert result is None

    def test_returns_none_when_incident_not_found(self, notifier):
        app, mock_table, _ = notifier
        mock_table.get_item.return_value = {}
        mock_slack = MagicMock()
        with patch("app.WebClient", return_value=mock_slack):
            result = app.handler({"incident_id": "nonexistent"}, None)
        assert result is None

    def test_new_incident_posts_message_and_returns_thread_id(self, notifier):
        app, mock_table, _ = notifier
        mock_table.get_item.return_value = {"Item": INCIDENT}
        mock_slack = MagicMock()
        mock_slack.chat_postMessage.return_value = {"ts": "1705312800.000001"}
        with patch("app.WebClient", return_value=mock_slack):
            result = app.handler({"incident_id": "inc-123"}, None)
        assert result["incident_id"] == "inc-123"
        assert result["slack_thread_id"] == "1705312800.000001"

    def test_new_incident_writes_thread_id_to_dynamodb(self, notifier):
        app, mock_table, _ = notifier
        mock_table.get_item.return_value = {"Item": INCIDENT}
        mock_slack = MagicMock()
        mock_slack.chat_postMessage.return_value = {"ts": "1705312800.000001"}
        with patch("app.WebClient", return_value=mock_slack):
            app.handler({"incident_id": "inc-123"}, None)
        mock_table.update_item.assert_called_once()
        call_kwargs = mock_table.update_item.call_args[1]
        assert call_kwargs["Key"] == {"incident_id": "inc-123"}
        assert call_kwargs["ExpressionAttributeValues"][":ts"] == "1705312800.000001"

    def test_existing_incident_posts_reply_to_thread(self, notifier):
        app, mock_table, _ = notifier
        mock_table.get_item.return_value = {"Item": INCIDENT_WITH_THREAD}
        mock_slack = MagicMock()
        mock_slack.chat_postMessage.return_value = {"ts": "1705312900.000001"}
        with patch("app.WebClient", return_value=mock_slack):
            result = app.handler({"incident_id": "inc-123"}, None)
        call_kwargs = mock_slack.chat_postMessage.call_args[1]
        assert call_kwargs["thread_ts"] == INCIDENT_WITH_THREAD["slack_thread_id"]
        assert result["slack_thread_id"] == INCIDENT_WITH_THREAD["slack_thread_id"]

    def test_existing_incident_does_not_update_dynamodb(self, notifier):
        app, mock_table, _ = notifier
        mock_table.get_item.return_value = {"Item": INCIDENT_WITH_THREAD}
        mock_slack = MagicMock()
        mock_slack.chat_postMessage.return_value = {"ts": "1705312900.000001"}
        with patch("app.WebClient", return_value=mock_slack):
            app.handler({"incident_id": "inc-123"}, None)
        mock_table.update_item.assert_not_called()


# ── Message format ────────────────────────────────────────────────────────────

class TestMessageFormat:
    def test_message_includes_severity_emoji_for_high(self, notifier):
        app, _, _ = notifier
        msg = app._build_message(INCIDENT)
        assert "🟠" in msg

    def test_message_includes_severity_emoji_for_critical(self, notifier):
        app, _, _ = notifier
        msg = app._build_message({**INCIDENT, "severity": "critical"})
        assert "🔴" in msg

    def test_message_includes_severity_emoji_for_medium(self, notifier):
        app, _, _ = notifier
        msg = app._build_message({**INCIDENT, "severity": "medium"})
        assert "🟡" in msg

    def test_message_includes_severity_emoji_for_low(self, notifier):
        app, _, _ = notifier
        msg = app._build_message({**INCIDENT, "severity": "low"})
        assert "🟢" in msg

    def test_unknown_severity_has_no_emoji(self, notifier):
        app, _, _ = notifier
        msg = app._build_message({**INCIDENT, "severity": "unknown"})
        assert "🔴" not in msg
        assert "🟠" not in msg
        assert "🟡" not in msg
        assert "🟢" not in msg

    def test_message_includes_affected_service(self, notifier):
        app, _, _ = notifier
        assert "payments-service" in app._build_message(INCIDENT)

    def test_message_includes_created_at(self, notifier):
        app, _, _ = notifier
        assert "2024-01-15T10:00:00Z" in app._build_message(INCIDENT)

    def test_message_includes_llm_summary_fields(self, notifier):
        app, _, _ = notifier
        msg = app._build_message(INCIDENT)
        assert "Payments service is down." in msg
        assert "Database overload." in msg
        assert "Restart the DB connection pool." in msg

    def test_message_falls_back_to_alert_list_when_no_summary(self, notifier):
        app, _, _ = notifier
        msg = app._build_message(INCIDENT_NO_SUMMARY)
        assert "high-error-rate" in msg
        assert "cloudwatch" in msg

    def test_message_falls_back_when_llm_summary_is_malformed(self, notifier):
        app, _, _ = notifier
        incident = {**INCIDENT, "llm_summary": "not-valid-json"}
        msg = app._build_message(incident)
        assert "high-error-rate" in msg


# ── Token caching ─────────────────────────────────────────────────────────────

class TestTokenCaching:
    def test_token_fetched_from_secrets_manager(self, notifier):
        app, mock_table, mock_secrets = notifier
        mock_slack = MagicMock()
        mock_slack.chat_postMessage.return_value = {"ts": "1705312800.000001"}
        with patch("app.WebClient", return_value=mock_slack):
            app.handler({"incident_id": "inc-123"}, None)
        mock_secrets.get_secret_value.assert_called_once_with(SecretId=SLACK_TOKEN_ARN)

    def test_token_cached_across_calls(self, notifier):
        app, mock_table, mock_secrets = notifier
        mock_slack = MagicMock()
        mock_slack.chat_postMessage.return_value = {"ts": "1705312800.000001"}
        with patch("app.WebClient", return_value=mock_slack):
            app.handler({"incident_id": "inc-123"}, None)
            app.handler({"incident_id": "inc-123"}, None)
        assert mock_secrets.get_secret_value.call_count == 1


# ── Retry behaviour ───────────────────────────────────────────────────────────

class TestRetry:
    def test_retries_on_slack_api_error(self, notifier):
        app, mock_table, _ = notifier
        mock_slack = MagicMock()
        mock_slack.chat_postMessage.side_effect = [
            _slack_api_error("ratelimited"),
            {"ts": "1705312800.000001"},
        ]
        with patch("app.WebClient", return_value=mock_slack), patch("time.sleep"):
            result = app.handler({"incident_id": "inc-123"}, None)
        assert mock_slack.chat_postMessage.call_count == 2
        assert result["slack_thread_id"] == "1705312800.000001"

    def test_raises_after_three_failures(self, notifier):
        app, mock_table, _ = notifier
        mock_slack = MagicMock()
        mock_slack.chat_postMessage.side_effect = _slack_api_error("ratelimited")
        with patch("app.WebClient", return_value=mock_slack), patch("time.sleep"):
            with pytest.raises(SlackApiError):
                app.handler({"incident_id": "inc-123"}, None)
        assert mock_slack.chat_postMessage.call_count == 3
