import importlib
import json
import sys
from unittest.mock import MagicMock, patch

import pytest
import requests

INCIDENT_TABLE = "test-incident-table"
JIRA_TOKEN_ARN = "arn:aws:secretsmanager:us-east-1:123456789012:secret:jira-token"
JIRA_TOKEN = "jira-api-token-value"
JIRA_BASE_URL = "https://hirereidcollins.atlassian.net"
JIRA_PROJECT_KEY = "INC"
JIRA_USER_EMAIL = "hire.reid.collins@gmail.com"
SLACK_CHANNEL_ID = "C0B4L4L5H4J"
DATADOG_EVENTS_FUNCTION = "test-datadog-events"

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


def _load_jira_creator():
    for mod in list(sys.modules):
        if mod == "app" or mod.startswith("app."):
            del sys.modules[mod]
    sys.path.insert(0, "functions/jira_creator")
    import app
    importlib.reload(app)
    return app


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
    monkeypatch.setenv("DATADOG_EVENTS_FUNCTION_NAME", DATADOG_EVENTS_FUNCTION)

    mock_table = MagicMock()
    mock_table.get_item.return_value = {"Item": INCIDENT_NO_JIRA}
    mock_table.update_item.return_value = {}

    mock_secrets = MagicMock()
    mock_secrets.get_secret_value.return_value = {"SecretString": JIRA_TOKEN}

    with patch("boto3.resource"), patch("boto3.client"):
        app = _load_jira_creator()
        app._incident_table = mock_table
        app._secrets_client = mock_secrets
        app._lambda_client = MagicMock()
        app._token_cache.clear()
        yield app, mock_table, mock_secrets


# ── Handler routing ───────────────────────────────────────────────────────────

class TestHandler:
    def test_returns_none_when_no_incident_id(self, jira):
        app, _, _ = jira
        with patch("requests.post", return_value=_mock_jira_response()):
            result = app.handler({}, None)
        assert result is None

    def test_returns_none_when_incident_not_found(self, jira):
        app, mock_table, _ = jira
        mock_table.get_item.return_value = {}
        with patch("requests.post", return_value=_mock_jira_response()):
            result = app.handler({"incident_id": "nonexistent"}, None)
        assert result is None

    def test_creates_ticket_and_returns_key(self, jira):
        app, _, _ = jira
        with patch("requests.post", return_value=_mock_jira_response("INC-7")):
            result = app.handler({"incident_id": "inc-123"}, None)
        assert result["incident_id"] == "inc-123"
        assert result["jira_ticket_id"] == "INC-7"

    def test_writes_ticket_key_to_dynamodb(self, jira):
        app, mock_table, _ = jira
        with patch("requests.post", return_value=_mock_jira_response("INC-7")):
            app.handler({"incident_id": "inc-123"}, None)
        mock_table.update_item.assert_called_once()
        call_kwargs = mock_table.update_item.call_args[1]
        assert call_kwargs["Key"] == {"incident_id": "inc-123"}
        assert call_kwargs["ExpressionAttributeValues"][":k"] == "INC-7"

    def test_skips_creation_when_ticket_already_exists(self, jira):
        app, mock_table, _ = jira
        mock_table.get_item.return_value = {"Item": INCIDENT_WITH_JIRA}
        with patch("requests.post", return_value=_mock_jira_response()) as mock_post:
            result = app.handler({"incident_id": "inc-123"}, None)
        mock_post.assert_not_called()
        mock_table.update_item.assert_not_called()
        assert result["jira_ticket_id"] == "INC-42"

    def test_http_error_propagates(self, jira):
        app, _, _ = jira
        mock_response = MagicMock()
        mock_response.raise_for_status.side_effect = requests.HTTPError("500 Server Error")
        with patch("requests.post", return_value=mock_response):
            with pytest.raises(requests.HTTPError):
                app.handler({"incident_id": "inc-123"}, None)


# ── Delivery chain ────────────────────────────────────────────────────────────

class TestDatadogHandoff:
    def _invoke_payload(self, app):
        app._lambda_client.invoke.assert_called_once()
        kwargs = app._lambda_client.invoke.call_args[1]
        assert kwargs["FunctionName"] == DATADOG_EVENTS_FUNCTION
        assert kwargs["InvocationType"] == "Event"
        return json.loads(kwargs["Payload"])

    def test_invokes_datadog_events_after_creating_ticket(self, jira):
        app, _, _ = jira
        with patch("requests.post", return_value=_mock_jira_response("INC-7")):
            app.handler({"incident_id": "inc-123"}, None)
        assert self._invoke_payload(app) == {"incident_id": "inc-123"}

    def test_invokes_datadog_events_when_ticket_already_exists(self, jira):
        app, mock_table, _ = jira
        mock_table.get_item.return_value = {"Item": INCIDENT_WITH_JIRA}
        with patch("requests.post", return_value=_mock_jira_response()):
            app.handler({"incident_id": "inc-123"}, None)
        assert self._invoke_payload(app) == {"incident_id": "inc-123"}

    def test_does_not_invoke_when_ticket_creation_fails(self, jira):
        app, _, _ = jira
        mock_response = MagicMock()
        mock_response.raise_for_status.side_effect = requests.HTTPError("500 Server Error")
        with patch("requests.post", return_value=mock_response):
            with pytest.raises(requests.HTTPError):
                app.handler({"incident_id": "inc-123"}, None)
        app._lambda_client.invoke.assert_not_called()

    def test_does_not_invoke_when_incident_missing(self, jira):
        app, mock_table, _ = jira
        mock_table.get_item.return_value = {}
        app.handler({"incident_id": "nonexistent"}, None)
        app._lambda_client.invoke.assert_not_called()


# ── Jira API call ─────────────────────────────────────────────────────────────

class TestJiraApiCall:
    def test_posts_to_correct_url(self, jira):
        app, _, _ = jira
        with patch("requests.post", return_value=_mock_jira_response()) as mock_post:
            app.handler({"incident_id": "inc-123"}, None)
        url = mock_post.call_args[0][0]
        assert url == f"{JIRA_BASE_URL}/rest/api/3/issue"

    def test_uses_basic_auth(self, jira):
        app, _, _ = jira
        with patch("requests.post", return_value=_mock_jira_response()) as mock_post:
            app.handler({"incident_id": "inc-123"}, None)
        auth = mock_post.call_args[1]["auth"]
        assert auth.username == JIRA_USER_EMAIL
        assert auth.password == JIRA_TOKEN

    def test_summary_includes_severity_and_service(self, jira):
        app, _, _ = jira
        with patch("requests.post", return_value=_mock_jira_response()) as mock_post:
            app.handler({"incident_id": "inc-123"}, None)
        fields = mock_post.call_args[1]["json"]["fields"]
        assert "HIGH" in fields["summary"]
        assert "payments-service" in fields["summary"]

    def test_project_key_set_correctly(self, jira):
        app, _, _ = jira
        with patch("requests.post", return_value=_mock_jira_response()) as mock_post:
            app.handler({"incident_id": "inc-123"}, None)
        fields = mock_post.call_args[1]["json"]["fields"]
        assert fields["project"]["key"] == JIRA_PROJECT_KEY


# ── Priority mapping ──────────────────────────────────────────────────────────

class TestPriorityMapping:
    def _get_priority(self, app, severity):
        incident = {**INCIDENT_NO_JIRA, "severity": severity}
        with patch("requests.post", return_value=_mock_jira_response()) as mock_post:
            app._incident_table.get_item.return_value = {"Item": incident}
            app.handler({"incident_id": "inc-123"}, None)
        return mock_post.call_args[1]["json"]["fields"]["priority"]["name"]

    def test_critical_maps_to_highest(self, jira):
        app, _, _ = jira
        assert self._get_priority(app, "critical") == "Highest"

    def test_high_maps_to_high(self, jira):
        app, _, _ = jira
        assert self._get_priority(app, "high") == "High"

    def test_medium_maps_to_medium(self, jira):
        app, _, _ = jira
        assert self._get_priority(app, "medium") == "Medium"

    def test_low_maps_to_low(self, jira):
        app, _, _ = jira
        assert self._get_priority(app, "low") == "Low"

    def test_unknown_severity_defaults_to_medium(self, jira):
        app, _, _ = jira
        assert self._get_priority(app, "unknown") == "Medium"


# ── Description content ───────────────────────────────────────────────────────

class TestDescription:
    def _get_description_text(self, app, incident):
        app._incident_table.get_item.return_value = {"Item": incident}
        with patch("requests.post", return_value=_mock_jira_response()) as mock_post:
            app.handler({"incident_id": "inc-123"}, None)
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
        app, _, _ = jira
        text = self._get_description_text(app, INCIDENT_NO_JIRA)
        assert "Payments service is down." in text

    def test_description_includes_likely_cause(self, jira):
        app, _, _ = jira
        text = self._get_description_text(app, INCIDENT_NO_JIRA)
        assert "Database overload." in text

    def test_description_includes_next_step(self, jira):
        app, _, _ = jira
        text = self._get_description_text(app, INCIDENT_NO_JIRA)
        assert "Restart the DB connection pool." in text

    def test_description_includes_alert_names(self, jira):
        app, _, _ = jira
        text = self._get_description_text(app, INCIDENT_NO_JIRA)
        assert "high-error-rate" in text
        assert "latency-spike" in text

    def test_description_includes_slack_thread_link(self, jira):
        app, _, _ = jira
        text = self._get_description_text(app, INCIDENT_NO_JIRA)
        assert INCIDENT["slack_thread_id"] in text


# ── Secret shape ──────────────────────────────────────────────────────────────

class TestSecretShape:
    def _password_used(self, app):
        with patch("requests.post", return_value=_mock_jira_response()) as mock_post:
            app.handler({"incident_id": "inc-123"}, None)
        return mock_post.call_args[1]["auth"].password

    def test_bare_token_is_used_as_is(self, jira):
        app, _, _ = jira
        assert self._password_used(app) == JIRA_TOKEN

    def test_json_secret_yields_its_api_token(self, jira):
        app, _, mock_secrets = jira
        mock_secrets.get_secret_value.return_value = {
            "SecretString": json.dumps({"email": "someone@example.com", "api_token": "inner-token"})
        }
        assert self._password_used(app) == "inner-token"

    def test_surrounding_whitespace_is_stripped(self, jira):
        app, _, mock_secrets = jira
        mock_secrets.get_secret_value.return_value = {"SecretString": f"  {JIRA_TOKEN}\n"}
        assert self._password_used(app) == JIRA_TOKEN

    def test_rejection_body_is_logged_before_raising(self, jira, caplog):
        app, _, _ = jira
        mock_response = MagicMock()
        mock_response.ok = False
        mock_response.status_code = 400
        mock_response.text = '{"errors":{"project":"The target project doesn\'t exist"}}'
        mock_response.raise_for_status.side_effect = requests.HTTPError("400 Client Error")
        with patch("requests.post", return_value=mock_response), caplog.at_level("ERROR"):
            with pytest.raises(requests.HTTPError):
                app.handler({"incident_id": "inc-123"}, None)
        assert "target project" in caplog.text
        assert "400" in caplog.text


# ── Token caching ─────────────────────────────────────────────────────────────

class TestTokenCaching:
    def test_token_fetched_from_secrets_manager(self, jira):
        app, _, mock_secrets = jira
        with patch("requests.post", return_value=_mock_jira_response()):
            app.handler({"incident_id": "inc-123"}, None)
        mock_secrets.get_secret_value.assert_called_once_with(SecretId=JIRA_TOKEN_ARN)

    def test_token_cached_across_calls(self, jira):
        app, _, mock_secrets = jira
        with patch("requests.post", return_value=_mock_jira_response()):
            app.handler({"incident_id": "inc-123"}, None)
            app.handler({"incident_id": "inc-123"}, None)
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
    def _run(self, app, mock_table, incident=RESOLVED_INCIDENT, transitions=None):
        mock_table.get_item.return_value = {"Item": incident}
        transitions = transitions if transitions is not None else [
            {"id": "11", "name": "In Progress", "to": {"statusCategory": {"key": "indeterminate"}}},
            {"id": "31", "name": "Done", "to": {"statusCategory": {"key": "done"}}},
        ]
        with patch("requests.post", return_value=_ok()) as mock_post, \
             patch("requests.get", return_value=_ok({"transitions": transitions})) as mock_get:
            result = app.handler({"incident_id": "inc-123", "recovered": True}, None)
        return result, mock_post, mock_get

    def test_recovery_comments_on_the_ticket_instead_of_creating(self, jira):
        app, mock_table, _ = jira
        result, mock_post, _ = self._run(app, mock_table)
        urls = [c[0][0] for c in mock_post.call_args_list]
        assert f"{JIRA_BASE_URL}/rest/api/3/issue/INC-42/comment" in urls
        assert f"{JIRA_BASE_URL}/rest/api/3/issue" not in urls
        mock_table.update_item.assert_not_called()
        assert result == {"incident_id": "inc-123", "jira_ticket_id": "INC-42", "resolved": True}

    def test_recovery_comment_carries_the_summary(self, jira):
        app, mock_table, _ = jira
        _, mock_post, _ = self._run(app, mock_table)
        comment_call = next(c for c in mock_post.call_args_list if c[0][0].endswith("/comment"))
        text = json.dumps(comment_call[1]["json"]["body"])
        assert "Incident resolved." in text
        assert "Payments recovered after 42 minutes." in text
        assert "2024-01-15T10:42:30Z" in text

    def test_recovery_transitions_to_done_category(self, jira):
        app, mock_table, _ = jira
        _, mock_post, _ = self._run(app, mock_table)
        transition_call = next(c for c in mock_post.call_args_list if c[0][0].endswith("/transitions"))
        assert transition_call[1]["json"] == {"transition": {"id": "31"}}

    def test_recovery_without_done_transition_only_comments(self, jira):
        app, mock_table, _ = jira
        _, mock_post, _ = self._run(app, mock_table, transitions=[
            {"id": "11", "name": "In Progress", "to": {"statusCategory": {"key": "indeterminate"}}}])
        assert not any(c[0][0].endswith("/transitions") for c in mock_post.call_args_list)

    def test_recovery_hands_off_to_datadog_with_flag(self, jira):
        app, mock_table, _ = jira
        self._run(app, mock_table)
        payload = json.loads(app._lambda_client.invoke.call_args[1]["Payload"])
        assert payload == {"incident_id": "inc-123", "recovered": True}

    def test_recovery_without_ticket_skips_jira_but_hands_off(self, jira):
        app, mock_table, _ = jira
        incident = {k: v for k, v in RESOLVED_INCIDENT.items() if k != "jira_ticket_id"}
        result, mock_post, mock_get = self._run(app, mock_table, incident=incident)
        mock_post.assert_not_called()
        mock_get.assert_not_called()
        assert json.loads(app._lambda_client.invoke.call_args[1]["Payload"])["recovered"] is True
        assert result["jira_ticket_id"] is None

    def test_jira_failure_on_recovery_does_not_block_handoff(self, jira):
        app, mock_table, _ = jira
        mock_table.get_item.return_value = {"Item": RESOLVED_INCIDENT}
        bad = MagicMock(); bad.ok = False; bad.status_code = 500; bad.text = "boom"
        with patch("requests.post", return_value=bad), patch("requests.get", return_value=bad):
            app.handler({"incident_id": "inc-123", "recovered": True}, None)
        app._lambda_client.invoke.assert_called_once()
