"""delivery.jira: one ticket per incident, closed on recovery."""
import json
from unittest.mock import MagicMock, patch

import pytest
import requests

from common import aws
from tests.conftest import load_function_module

INCIDENT_TABLE = "test-incident-table"
JIRA_TOKEN_ARN = "arn:aws:secretsmanager:us-east-1:123456789012:secret:jira-token"
JIRA_TOKEN = "jira-api-token-value"
JIRA_BASE_URL = "https://hirereidcollins.atlassian.net"
JIRA_PROJECT_KEY = "INC"
JIRA_USER_EMAIL = "hire.reid.collins@gmail.com"
SLACK_CHANNEL_ID = "C0B4L4L5H4J"

INCIDENT = {
    "incident_id": "inc-123",
    "affected_service": "payments-service",
    "severity": "high",
    "status": "open",
    "created_at": "2024-01-15T10:00:00Z",
    "slack_thread_id": "1705312800.123456",
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

INCIDENT_NO_JIRA = {**INCIDENT}
INCIDENT_WITH_JIRA = {**INCIDENT, "jira_ticket_id": "INC-42"}
INCIDENT_NO_SUMMARY = {k: v for k, v in INCIDENT.items() if k != "llm_summary"}


def _mock_jira_response(ticket_key: str = "INC-1") -> MagicMock:
    mock_response = MagicMock()
    mock_response.json.return_value = {"key": ticket_key}
    mock_response.raise_for_status.return_value = None
    return mock_response


@pytest.fixture()
def jira(monkeypatch):
    monkeypatch.setenv("INCIDENT_TABLE_NAME", INCIDENT_TABLE)
    monkeypatch.setenv("JIRA_API_TOKEN_SECRET_ARN", JIRA_TOKEN_ARN)
    monkeypatch.setenv("JIRA_BASE_URL", JIRA_BASE_URL)
    monkeypatch.setenv("JIRA_PROJECT_KEY", JIRA_PROJECT_KEY)
    monkeypatch.setenv("JIRA_USER_EMAIL", JIRA_USER_EMAIL)
    monkeypatch.setenv("SLACK_CHANNEL_ID", SLACK_CHANNEL_ID)

    aws.reset()
    mock_table = MagicMock()
    mock_table.update_item.return_value = {}
    aws._tables[INCIDENT_TABLE] = mock_table
    mock_secrets = MagicMock()
    mock_secrets.get_secret_value.return_value = {"SecretString": JIRA_TOKEN}
    aws._secrets_client = mock_secrets

    mod = load_function_module("summarizer", "delivery.jira")
    yield mod, mock_table, mock_secrets


def _create(mod, incident=INCIDENT_NO_JIRA, ticket_key="INC-1"):
    with patch("requests.post", return_value=_mock_jira_response(ticket_key)) as mock_post:
        result = mod.deliver(dict(incident), False)
    return result, mock_post


# ── Delivery ──────────────────────────────────────────────────────────────────

class TestDeliver:
    def test_creates_ticket_and_returns_key(self, jira):
        mod, _, _ = jira
        result, _ = _create(mod, ticket_key="INC-7")
        assert result == "INC-7"

    def test_writes_ticket_key_to_dynamodb_and_the_incident(self, jira):
        mod, mock_table, _ = jira
        incident = dict(INCIDENT_NO_JIRA)
        with patch("requests.post", return_value=_mock_jira_response("INC-7")):
            mod.deliver(incident, False)
        mock_table.update_item.assert_called_once()
        call_kwargs = mock_table.update_item.call_args[1]
        assert call_kwargs["Key"] == {"incident_id": "inc-123"}
        assert call_kwargs["ExpressionAttributeValues"][":k"] == "INC-7"
        assert incident["jira_ticket_id"] == "INC-7"

    def test_skips_creation_when_ticket_already_exists(self, jira):
        mod, mock_table, _ = jira
        result, mock_post = _create(mod, INCIDENT_WITH_JIRA)
        mock_post.assert_not_called()
        mock_table.update_item.assert_not_called()
        assert result == "INC-42"

    def test_http_error_propagates_and_nothing_is_written(self, jira):
        mod, mock_table, _ = jira
        mock_response = MagicMock()
        mock_response.raise_for_status.side_effect = requests.HTTPError("500 Server Error")
        with patch("requests.post", return_value=mock_response):
            with pytest.raises(requests.HTTPError):
                mod.deliver(dict(INCIDENT_NO_JIRA), False)
        mock_table.update_item.assert_not_called()


# ── Jira API call ─────────────────────────────────────────────────────────────

class TestJiraApiCall:
    def test_posts_to_correct_url(self, jira):
        mod, _, _ = jira
        _, mock_post = _create(mod)
        assert mock_post.call_args[0][0] == f"{JIRA_BASE_URL}/rest/api/3/issue"

    def test_uses_basic_auth(self, jira):
        mod, _, _ = jira
        _, mock_post = _create(mod)
        auth = mock_post.call_args[1]["auth"]
        assert auth.username == JIRA_USER_EMAIL
        assert auth.password == JIRA_TOKEN

    def test_summary_includes_severity_and_service(self, jira):
        mod, _, _ = jira
        _, mock_post = _create(mod)
        fields = mock_post.call_args[1]["json"]["fields"]
        assert "HIGH" in fields["summary"]
        assert "payments-service" in fields["summary"]

    def test_summary_flags_a_repeat(self, jira):
        mod, _, _ = jira
        incident = {**INCIDENT_NO_JIRA, "recurrence": {"count_7d": 6, "previous_incident_id": "p", "previous_created_at": "x", "previous_jira_ticket_id": "INC-96"}}
        _, mock_post = _create(mod, incident)
        assert mock_post.call_args[1]["json"]["fields"]["summary"].endswith("(recurring: 7th time in 7 days)")

    def test_description_links_the_previous_ticket(self, jira):
        mod, _, _ = jira
        incident = {**INCIDENT_NO_JIRA, "recurrence": {"count_7d": 6, "previous_incident_id": "p", "previous_created_at": "2024-01-14T10:00:00Z", "previous_jira_ticket_id": "INC-96"}}
        _, mock_post = _create(mod, incident)
        description = json.dumps(mock_post.call_args[1]["json"]["fields"]["description"])
        assert "Recurring: This is the 7th incident" in description
        assert f"{JIRA_BASE_URL}/browse/INC-96" in description

    def test_project_key_set_correctly(self, jira):
        mod, _, _ = jira
        _, mock_post = _create(mod)
        assert mock_post.call_args[1]["json"]["fields"]["project"]["key"] == JIRA_PROJECT_KEY


# ── Priority mapping ──────────────────────────────────────────────────────────

class TestPriorityMapping:
    def _get_priority(self, mod, severity):
        _, mock_post = _create(mod, {**INCIDENT_NO_JIRA, "severity": severity})
        return mock_post.call_args[1]["json"]["fields"]["priority"]["name"]

    def test_critical_maps_to_highest(self, jira):
        mod, _, _ = jira
        assert self._get_priority(mod, "critical") == "Highest"

    def test_high_maps_to_high(self, jira):
        mod, _, _ = jira
        assert self._get_priority(mod, "high") == "High"

    def test_medium_maps_to_medium(self, jira):
        mod, _, _ = jira
        assert self._get_priority(mod, "medium") == "Medium"

    def test_low_maps_to_low(self, jira):
        mod, _, _ = jira
        assert self._get_priority(mod, "low") == "Low"

    def test_unknown_severity_defaults_to_medium(self, jira):
        mod, _, _ = jira
        assert self._get_priority(mod, "unknown") == "Medium"


# ── Description content ───────────────────────────────────────────────────────

class TestDescription:
    def _get_description_text(self, mod, incident):
        _, mock_post = _create(mod, incident)
        doc = mock_post.call_args[1]["json"]["fields"]["description"]
        texts = []
        def extract(node):
            if isinstance(node, dict):
                if node.get("type") == "text":
                    texts.append(node.get("text", ""))
                for v in node.values():
                    extract(v)
            elif isinstance(node, list):
                for item in node:
                    extract(item)
        extract(doc)
        return " ".join(texts)

    def test_description_includes_llm_summary(self, jira):
        mod, _, _ = jira
        assert "Payments service is down." in self._get_description_text(mod, INCIDENT_NO_JIRA)

    def test_description_includes_likely_cause(self, jira):
        mod, _, _ = jira
        assert "Database overload." in self._get_description_text(mod, INCIDENT_NO_JIRA)

    def test_description_includes_next_step(self, jira):
        mod, _, _ = jira
        assert "Restart the DB connection pool." in self._get_description_text(mod, INCIDENT_NO_JIRA)

    def test_description_includes_alert_names(self, jira):
        mod, _, _ = jira
        text = self._get_description_text(mod, INCIDENT_NO_JIRA)
        assert "high-error-rate" in text
        assert "latency-spike" in text

    def test_description_includes_slack_thread_link(self, jira):
        mod, _, _ = jira
        assert INCIDENT["slack_thread_id"] in self._get_description_text(mod, INCIDENT_NO_JIRA)

    def test_description_without_summary_still_lists_alerts(self, jira):
        mod, _, _ = jira
        text = self._get_description_text(mod, INCIDENT_NO_SUMMARY)
        assert "Summary:" not in text
        assert "high-error-rate" in text


# ── Secret shape ──────────────────────────────────────────────────────────────

class TestSecretShape:
    def _password_used(self, mod):
        _, mock_post = _create(mod)
        return mock_post.call_args[1]["auth"].password

    def test_bare_token_is_used_as_is(self, jira):
        mod, _, _ = jira
        assert self._password_used(mod) == JIRA_TOKEN

    def test_json_secret_yields_its_api_token(self, jira):
        mod, _, mock_secrets = jira
        mock_secrets.get_secret_value.return_value = {
            "SecretString": json.dumps({"email": "someone@example.com", "api_token": "inner-token"})
        }
        assert self._password_used(mod) == "inner-token"

    def test_surrounding_whitespace_is_stripped(self, jira):
        mod, _, mock_secrets = jira
        mock_secrets.get_secret_value.return_value = {"SecretString": f"  {JIRA_TOKEN}\n"}
        assert self._password_used(mod) == JIRA_TOKEN

    def test_rejection_body_is_logged_before_raising(self, jira, caplog):
        mod, _, _ = jira
        mock_response = MagicMock()
        mock_response.ok = False
        mock_response.status_code = 400
        mock_response.text = '{"errors":{"project":"The target project doesn\'t exist"}}'
        mock_response.raise_for_status.side_effect = requests.HTTPError("400 Client Error")
        with patch("requests.post", return_value=mock_response), caplog.at_level("ERROR"):
            with pytest.raises(requests.HTTPError):
                mod.deliver(dict(INCIDENT_NO_JIRA), False)
        assert "target project" in caplog.text
        assert "400" in caplog.text


# ── Token caching ─────────────────────────────────────────────────────────────

class TestTokenCaching:
    def test_token_fetched_from_secrets_manager(self, jira):
        mod, _, mock_secrets = jira
        _create(mod)
        mock_secrets.get_secret_value.assert_called_once_with(SecretId=JIRA_TOKEN_ARN)

    def test_token_cached_across_calls(self, jira):
        mod, _, mock_secrets = jira
        _create(mod)
        _create(mod)
        assert mock_secrets.get_secret_value.call_count == 1


# ── Recovery (RC1-374) ───────────────────────────────────────────────────────

RESOLVED_INCIDENT = {
    **INCIDENT_WITH_JIRA,
    "status": "resolved",
    "resolved_at": "2024-01-15T10:42:30Z",
    "recovery_summary": json.dumps({
        "summary": "Payments recovered after 42 minutes.",
        "likely_cause": "Pool exhaustion.",
        "next_step": "Raise the pool ceiling.",
    }),
}


def _ok(json_body=None):
    r = MagicMock()
    r.ok = True
    r.status_code = 200
    r.json.return_value = json_body or {}
    return r


class TestRecovery:
    def _run(self, mod, incident=RESOLVED_INCIDENT, transitions=None):
        transitions = transitions if transitions is not None else [
            {"id": "11", "name": "In Progress", "to": {"statusCategory": {"key": "indeterminate"}}},
            {"id": "31", "name": "Done", "to": {"statusCategory": {"key": "done"}}},
        ]
        with patch("requests.post", return_value=_ok()) as mock_post, \
             patch("requests.get", return_value=_ok({"transitions": transitions})) as mock_get:
            result = mod.deliver(dict(incident), True)
        return result, mock_post, mock_get

    def test_recovery_comments_on_the_ticket_instead_of_creating(self, jira):
        mod, mock_table, _ = jira
        result, mock_post, _ = self._run(mod)
        urls = [c[0][0] for c in mock_post.call_args_list]
        assert f"{JIRA_BASE_URL}/rest/api/3/issue/INC-42/comment" in urls
        assert f"{JIRA_BASE_URL}/rest/api/3/issue" not in urls
        mock_table.update_item.assert_not_called()
        assert result == "INC-42"

    def test_recovery_comment_carries_the_summary(self, jira):
        mod, _, _ = jira
        _, mock_post, _ = self._run(mod)
        comment_call = next(c for c in mock_post.call_args_list if c[0][0].endswith("/comment"))
        text = json.dumps(comment_call[1]["json"]["body"])
        assert "Incident resolved." in text
        assert "Payments recovered after 42 minutes." in text
        assert "2024-01-15T10:42:30Z" in text

    def test_recovery_transitions_to_done_category(self, jira):
        mod, _, _ = jira
        _, mock_post, _ = self._run(mod)
        transition_call = next(c for c in mock_post.call_args_list if c[0][0].endswith("/transitions"))
        assert transition_call[1]["json"] == {"transition": {"id": "31"}}

    def test_recovery_without_done_transition_only_comments(self, jira):
        mod, _, _ = jira
        _, mock_post, _ = self._run(mod, transitions=[
            {"id": "11", "name": "In Progress", "to": {"statusCategory": {"key": "indeterminate"}}}])
        assert not any(c[0][0].endswith("/transitions") for c in mock_post.call_args_list)

    def test_recovery_without_ticket_touches_nothing(self, jira):
        mod, _, _ = jira
        incident = {k: v for k, v in RESOLVED_INCIDENT.items() if k != "jira_ticket_id"}
        result, mock_post, mock_get = self._run(mod, incident=incident)
        mock_post.assert_not_called()
        mock_get.assert_not_called()
        assert result is None

    def test_jira_failure_on_recovery_does_not_raise(self, jira):
        """Best effort: a Jira hiccup must not stop the Datadog event that follows."""
        mod, _, _ = jira
        bad = MagicMock(); bad.ok = False; bad.status_code = 500; bad.text = "boom"
        with patch("requests.post", return_value=bad), patch("requests.get", return_value=bad):
            assert mod.deliver(dict(RESOLVED_INCIDENT), True) == "INC-42"
