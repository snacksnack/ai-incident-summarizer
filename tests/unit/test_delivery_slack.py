"""delivery.slack: one thread per incident."""
import json
from unittest.mock import MagicMock, patch

import pytest
from slack_sdk.errors import SlackApiError

from common import aws
from tests.conftest import load_function_module

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


def _slack_api_error(error_code: str = "channel_not_found"):
    response = {"ok": False, "error": error_code, "headers": {}}
    return SlackApiError(message=error_code, response=response)


def _client(ts: str = "1705312800.000001") -> MagicMock:
    mock_slack = MagicMock()
    mock_slack.chat_postMessage.return_value = {"ts": ts}
    return mock_slack


@pytest.fixture()
def notifier(monkeypatch):
    monkeypatch.setenv("INCIDENT_TABLE_NAME", INCIDENT_TABLE)
    monkeypatch.setenv("SLACK_BOT_TOKEN_SECRET_ARN", SLACK_TOKEN_ARN)
    monkeypatch.setenv("SLACK_CHANNEL_ID", SLACK_CHANNEL)

    aws.reset()
    mock_table = MagicMock()
    mock_table.update_item.return_value = {}
    aws._tables[INCIDENT_TABLE] = mock_table
    mock_secrets = MagicMock()
    mock_secrets.get_secret_value.return_value = {"SecretString": SLACK_TOKEN}
    aws._secrets_client = mock_secrets

    slack = load_function_module("summarizer", "delivery.slack")
    yield slack, mock_table, mock_secrets


# ── Delivery ──────────────────────────────────────────────────────────────────

class TestDeliver:
    def test_new_incident_posts_message_and_returns_thread_id(self, notifier):
        slack, _, _ = notifier
        mock_slack = _client("1705312800.000001")
        with patch("delivery.slack.WebClient", return_value=mock_slack):
            result = slack.deliver(dict(INCIDENT), False)
        assert result == "1705312800.000001"
        kwargs = mock_slack.chat_postMessage.call_args[1]
        assert kwargs["channel"] == SLACK_CHANNEL
        assert "thread_ts" not in kwargs

    def test_new_incident_writes_thread_id_to_dynamodb_and_the_incident(self, notifier):
        slack, mock_table, _ = notifier
        incident = dict(INCIDENT)
        with patch("delivery.slack.WebClient", return_value=_client("1705312800.000001")):
            slack.deliver(incident, False)
        mock_table.update_item.assert_called_once()
        call_kwargs = mock_table.update_item.call_args[1]
        assert call_kwargs["Key"] == {"incident_id": "inc-123"}
        assert call_kwargs["ExpressionAttributeValues"][":ts"] == "1705312800.000001"
        assert incident["slack_thread_id"] == "1705312800.000001"

    def test_existing_incident_posts_reply_to_thread(self, notifier):
        slack, _, _ = notifier
        mock_slack = _client("1705312900.000001")
        with patch("delivery.slack.WebClient", return_value=mock_slack):
            result = slack.deliver(dict(INCIDENT_WITH_THREAD), False)
        call_kwargs = mock_slack.chat_postMessage.call_args[1]
        assert call_kwargs["thread_ts"] == INCIDENT_WITH_THREAD["slack_thread_id"]
        assert result == INCIDENT_WITH_THREAD["slack_thread_id"]

    def test_existing_incident_does_not_update_dynamodb(self, notifier):
        slack, mock_table, _ = notifier
        with patch("delivery.slack.WebClient", return_value=_client()):
            slack.deliver(dict(INCIDENT_WITH_THREAD), False)
        mock_table.update_item.assert_not_called()

    def test_failure_raises_and_writes_nothing(self, notifier):
        slack, mock_table, _ = notifier
        mock_slack = MagicMock()
        mock_slack.chat_postMessage.side_effect = _slack_api_error()
        with patch("delivery.slack.WebClient", return_value=mock_slack), patch("delivery.slack.time.sleep"):
            with pytest.raises(SlackApiError):
                slack.deliver(dict(INCIDENT), False)
        mock_table.update_item.assert_not_called()


# ── Message format ────────────────────────────────────────────────────────────

class TestMessageFormat:
    def test_message_includes_severity_emoji_for_high(self, notifier):
        slack, _, _ = notifier
        assert "🟠" in slack._build_message(INCIDENT)

    def test_message_includes_severity_emoji_for_critical(self, notifier):
        slack, _, _ = notifier
        assert "🔴" in slack._build_message({**INCIDENT, "severity": "critical"})

    def test_message_includes_severity_emoji_for_medium(self, notifier):
        slack, _, _ = notifier
        assert "🟡" in slack._build_message({**INCIDENT, "severity": "medium"})

    def test_message_includes_severity_emoji_for_low(self, notifier):
        slack, _, _ = notifier
        assert "🟢" in slack._build_message({**INCIDENT, "severity": "low"})

    def test_unknown_severity_has_no_emoji(self, notifier):
        slack, _, _ = notifier
        msg = slack._build_message({**INCIDENT, "severity": "unknown"})
        assert "🔴" not in msg
        assert "🟠" not in msg
        assert "🟡" not in msg
        assert "🟢" not in msg

    def test_message_includes_affected_service(self, notifier):
        slack, _, _ = notifier
        assert "payments-service" in slack._build_message(INCIDENT)

    def test_message_includes_created_at(self, notifier):
        slack, _, _ = notifier
        assert "2024-01-15T10:00:00Z" in slack._build_message(INCIDENT)

    def test_header_carries_the_repeat_and_the_previous_ticket(self, notifier):
        slack, _, _ = notifier
        incident = {**INCIDENT, "recurrence": {"count_7d": 6, "previous_incident_id": "p", "previous_created_at": "x", "previous_jira_ticket_id": "INC-96"}}
        text = slack._build_message(incident)
        assert text.startswith("🟠 *HIGH* | payments-service | 2024-01-15T10:00:00Z | 🔁 7th time in 7 days (previous: INC-96)")

    def test_header_without_recurrence_is_unchanged(self, notifier):
        slack, _, _ = notifier
        assert slack._build_message(INCIDENT).startswith("🟠 *HIGH* | payments-service | 2024-01-15T10:00:00Z\n")

    def test_message_includes_llm_summary_fields(self, notifier):
        slack, _, _ = notifier
        msg = slack._build_message(INCIDENT)
        assert "Payments service is down." in msg
        assert "Database overload." in msg
        assert "Restart the DB connection pool." in msg

    def test_message_falls_back_to_alert_list_when_no_summary(self, notifier):
        slack, _, _ = notifier
        msg = slack._build_message(INCIDENT_NO_SUMMARY)
        assert "high-error-rate" in msg
        assert "cloudwatch" in msg

    def test_message_falls_back_when_llm_summary_is_malformed(self, notifier):
        slack, _, _ = notifier
        msg = slack._build_message({**INCIDENT, "llm_summary": "not-valid-json"})
        assert "high-error-rate" in msg


# ── Token caching ─────────────────────────────────────────────────────────────

class TestTokenCaching:
    def test_token_fetched_from_secrets_manager(self, notifier):
        slack, _, mock_secrets = notifier
        with patch("delivery.slack.WebClient", return_value=_client()):
            slack.deliver(dict(INCIDENT), False)
        mock_secrets.get_secret_value.assert_called_once_with(SecretId=SLACK_TOKEN_ARN)

    def test_token_cached_across_calls(self, notifier):
        slack, _, mock_secrets = notifier
        with patch("delivery.slack.WebClient", return_value=_client()):
            slack.deliver(dict(INCIDENT), False)
            slack.deliver(dict(INCIDENT), False)
        assert mock_secrets.get_secret_value.call_count == 1


# ── Retry behaviour ───────────────────────────────────────────────────────────

class TestRetry:
    def test_retries_on_slack_api_error(self, notifier):
        slack, _, _ = notifier
        mock_slack = MagicMock()
        mock_slack.chat_postMessage.side_effect = [
            _slack_api_error("ratelimited"),
            {"ts": "1705312800.000001"},
        ]
        with patch("delivery.slack.WebClient", return_value=mock_slack), patch("time.sleep"):
            result = slack.deliver(dict(INCIDENT), False)
        assert mock_slack.chat_postMessage.call_count == 2
        assert result == "1705312800.000001"

    def test_raises_after_three_failures(self, notifier):
        slack, _, _ = notifier
        mock_slack = MagicMock()
        mock_slack.chat_postMessage.side_effect = _slack_api_error("ratelimited")
        with patch("delivery.slack.WebClient", return_value=mock_slack), patch("time.sleep"):
            with pytest.raises(SlackApiError):
                slack.deliver(dict(INCIDENT), False)
        assert mock_slack.chat_postMessage.call_count == 3


# ── Recovery (RC1-374) ───────────────────────────────────────────────────────

RESOLVED_INCIDENT = {
    **INCIDENT_WITH_THREAD,
    "status": "resolved",
    "resolved_at": "2024-01-15T10:42:30Z",
    "recovery_summary": json.dumps({
        "summary": "Payments recovered after 42 minutes.",
        "likely_cause": "Pool exhaustion, cleared by the restart.",
        "next_step": "Raise the pool ceiling.",
    }),
}


class TestRecovery:
    def _post(self, slack, incident=RESOLVED_INCIDENT):
        mock_slack = _client("1705312999.000001")
        with patch("delivery.slack.WebClient", return_value=mock_slack):
            slack.deliver(dict(incident), True)
        return mock_slack.chat_postMessage.call_args[1]

    def test_recovery_replies_in_the_existing_thread(self, notifier):
        slack, mock_table, _ = notifier
        kwargs = self._post(slack)
        assert kwargs["thread_ts"] == INCIDENT_WITH_THREAD["slack_thread_id"]
        mock_table.update_item.assert_not_called()

    def test_recovery_message_has_resolved_header_and_duration(self, notifier):
        slack, _, _ = notifier
        text = self._post(slack)["text"]
        assert text.startswith("🟢 *RESOLVED* | payments-service | 2024-01-15T10:42:30Z | open for 42m 30s")
        assert "Payments recovered after 42 minutes." in text
        assert "Raise the pool ceiling." in text

    def test_recovery_falls_back_to_alert_list_without_summary(self, notifier):
        slack, _, _ = notifier
        text = self._post(slack, {k: v for k, v in RESOLVED_INCIDENT.items() if k != "recovery_summary"})["text"]
        assert "Incident resolved." in text
        assert "high-error-rate" in text
