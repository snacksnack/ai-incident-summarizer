import importlib
import json
import sys
from unittest.mock import MagicMock, patch

import pytest
import requests

INCIDENT_TABLE = "test-incident-table"
DD_API_KEY_ARN = "arn:aws:secretsmanager:us-east-1:123456789012:secret:dd-api-key"
DD_API_KEY = "dd-api-key-value"
SLACK_CHANNEL_ID = "C0B4L4L5H4J"
JIRA_BASE_URL = "https://hirereidcollins.atlassian.net"
DASHBOARD_URL = "https://incidents.example.com"

INCIDENT = {
    "incident_id": "inc-123",
    "affected_service": "payments-service",
    "severity": "high",
    "status": "open",
    "created_at": "2024-01-15T10:00:00Z",
    "slack_thread_id": "1705312800.123456",
    "jira_ticket_id": "INC-42",
    "source_alerts": [
        {"alert_id": "a1", "alert_name": "high-error-rate", "source": "cloudwatch"},
        {"alert_id": "7788990011", "alert_name": "latency-spike", "source": "datadog"},
    ],
    "llm_summary": json.dumps({
        "summary": "Payments service is down.",
        "likely_cause": "Database overload.",
        "next_step": "Restart the DB connection pool.",
    }),
}

INCIDENT_NO_SUMMARY = {k: v for k, v in INCIDENT.items() if k != "llm_summary"}
INCIDENT_NO_LINKS = {k: v for k, v in INCIDENT.items() if k not in ("slack_thread_id", "jira_ticket_id")}


def _load_datadog_events():
    for mod in list(sys.modules):
        if mod == "app" or mod.startswith("app."):
            del sys.modules[mod]
    sys.path.insert(0, "functions/datadog_events")
    import app
    importlib.reload(app)
    return app


def _mock_dd_response(event_id: int = 555) -> MagicMock:
    mock_response = MagicMock()
    mock_response.json.return_value = {"status": "ok", "event": {"id": event_id, "url": "https://app.datadoghq.com/event/event?id=555"}}
    mock_response.raise_for_status.return_value = None
    return mock_response


@pytest.fixture()
def dd(monkeypatch):
    monkeypatch.setenv("INCIDENT_TABLE_NAME", INCIDENT_TABLE)
    monkeypatch.setenv("DD_API_KEY_SECRET_ARN", DD_API_KEY_ARN)
    monkeypatch.setenv("DD_SITE", "datadoghq.com")
    monkeypatch.setenv("DD_ENV", "dev")
    monkeypatch.setenv("SLACK_CHANNEL_ID", SLACK_CHANNEL_ID)
    monkeypatch.setenv("JIRA_BASE_URL", JIRA_BASE_URL)
    monkeypatch.setenv("INCIDENT_DASHBOARD_URL", DASHBOARD_URL)

    mock_table = MagicMock()
    mock_table.get_item.return_value = {"Item": INCIDENT}
    mock_table.update_item.return_value = {}

    mock_secrets = MagicMock()
    mock_secrets.get_secret_value.return_value = {"SecretString": DD_API_KEY}

    with patch("boto3.resource"), patch("boto3.client"):
        app = _load_datadog_events()
        app._incident_table = mock_table
        app._secrets_client = mock_secrets
        app._api_key_cache.clear()
        yield app, mock_table, mock_secrets


def _posted_event(app, incident=INCIDENT) -> dict:
    app._incident_table.get_item.return_value = {"Item": incident}
    with patch("requests.post", return_value=_mock_dd_response()) as mock_post:
        app.handler({"incident_id": "inc-123"}, None)
    return mock_post.call_args[1]["json"]


# ── Handler routing ───────────────────────────────────────────────────────────

class TestHandler:
    def test_returns_none_when_no_incident_id(self, dd):
        app, _, _ = dd
        with patch("requests.post", return_value=_mock_dd_response()) as mock_post:
            result = app.handler({}, None)
        assert result is None
        mock_post.assert_not_called()

    def test_returns_none_when_incident_not_found(self, dd):
        app, mock_table, _ = dd
        mock_table.get_item.return_value = {}
        with patch("requests.post", return_value=_mock_dd_response()) as mock_post:
            result = app.handler({"incident_id": "nonexistent"}, None)
        assert result is None
        mock_post.assert_not_called()

    def test_posts_event_and_returns_event_id(self, dd):
        app, _, _ = dd
        with patch("requests.post", return_value=_mock_dd_response(9001)):
            result = app.handler({"incident_id": "inc-123"}, None)
        assert result == {"incident_id": "inc-123", "datadog_event_id": "9001"}

    def test_writes_event_id_to_dynamodb(self, dd):
        app, mock_table, _ = dd
        with patch("requests.post", return_value=_mock_dd_response(9001)):
            app.handler({"incident_id": "inc-123"}, None)
        mock_table.update_item.assert_called_once()
        call_kwargs = mock_table.update_item.call_args[1]
        assert call_kwargs["Key"] == {"incident_id": "inc-123"}
        assert call_kwargs["ExpressionAttributeValues"][":e"] == "9001"

    def test_http_error_propagates_and_nothing_is_written(self, dd):
        app, mock_table, _ = dd
        mock_response = MagicMock()
        mock_response.raise_for_status.side_effect = requests.HTTPError("403 Forbidden")
        with patch("requests.post", return_value=mock_response):
            with pytest.raises(requests.HTTPError):
                app.handler({"incident_id": "inc-123"}, None)
        mock_table.update_item.assert_not_called()


# ── Datadog API call ──────────────────────────────────────────────────────────

class TestApiCall:
    def test_posts_to_v1_events_on_configured_site(self, dd):
        app, _, _ = dd
        with patch("requests.post", return_value=_mock_dd_response()) as mock_post:
            app.handler({"incident_id": "inc-123"}, None)
        assert mock_post.call_args[0][0] == "https://api.datadoghq.com/api/v1/events"

    def test_site_env_selects_the_endpoint(self, dd, monkeypatch):
        app, _, _ = dd
        monkeypatch.setenv("DD_SITE", "datadoghq.eu")
        with patch("requests.post", return_value=_mock_dd_response()) as mock_post:
            app.handler({"incident_id": "inc-123"}, None)
        assert mock_post.call_args[0][0] == "https://api.datadoghq.eu/api/v1/events"

    def test_sends_api_key_header(self, dd):
        app, _, _ = dd
        with patch("requests.post", return_value=_mock_dd_response()) as mock_post:
            app.handler({"incident_id": "inc-123"}, None)
        assert mock_post.call_args[1]["headers"]["DD-API-KEY"] == DD_API_KEY


# ── Event payload ─────────────────────────────────────────────────────────────

class TestPayload:
    def test_title_includes_severity_service_and_incident_id(self, dd):
        app, _, _ = dd
        title = _posted_event(app)["title"]
        assert "HIGH" in title
        assert "payments-service" in title
        assert "inc-123" in title

    def test_aggregation_key_is_the_incident(self, dd):
        app, _, _ = dd
        assert _posted_event(app)["aggregation_key"] == "incident:inc-123"

    def test_text_is_markdown_fenced(self, dd):
        app, _, _ = dd
        text = _posted_event(app)["text"]
        assert text.startswith("%%%")
        assert text.rstrip().endswith("%%%")

    def test_text_includes_llm_summary_fields(self, dd):
        app, _, _ = dd
        text = _posted_event(app)["text"]
        assert "Payments service is down." in text
        assert "Database overload." in text
        assert "Restart the DB connection pool." in text

    def test_text_includes_alert_names(self, dd):
        app, _, _ = dd
        text = _posted_event(app)["text"]
        assert "high-error-rate (cloudwatch)" in text
        assert "latency-spike (datadog)" in text

    def test_text_links_slack_jira_and_dashboard(self, dd):
        app, _, _ = dd
        text = _posted_event(app)["text"]
        assert f"https://slack.com/app_redirect?channel={SLACK_CHANNEL_ID}&message_ts={INCIDENT['slack_thread_id']}" in text
        assert f"{JIRA_BASE_URL}/browse/INC-42" in text
        assert f"{DASHBOARD_URL}/incidents/inc-123" in text

    def test_text_omits_links_that_do_not_exist_yet(self, dd, monkeypatch):
        app, _, _ = dd
        monkeypatch.setenv("INCIDENT_DASHBOARD_URL", "")
        text = _posted_event(app, INCIDENT_NO_LINKS)["text"]
        assert "slack.com" not in text
        assert "/browse/" not in text
        assert "/incidents/" not in text

    def test_text_falls_back_when_no_summary(self, dd):
        app, _, _ = dd
        text = _posted_event(app, INCIDENT_NO_SUMMARY)["text"]
        assert "No LLM summary available" in text
        assert "high-error-rate (cloudwatch)" in text

    def test_text_falls_back_when_summary_is_malformed(self, dd):
        app, _, _ = dd
        text = _posted_event(app, {**INCIDENT, "llm_summary": "not json"})["text"]
        assert "No LLM summary available" in text

    def test_text_is_truncated_to_the_datadog_limit_keeping_links(self, dd):
        app, _, _ = dd
        huge = {**INCIDENT, "llm_summary": json.dumps({
            "summary": "x" * 6000, "likely_cause": "y", "next_step": "z",
        })}
        text = _posted_event(app, huge)["text"]
        assert len(text) <= 4000
        assert f"{JIRA_BASE_URL}/browse/INC-42" in text
        assert text.rstrip().endswith("%%%")

    def test_tags_identify_incident_service_severity_and_origin(self, dd):
        app, _, _ = dd
        tags = _posted_event(app)["tags"]
        for tag in (
            "incident_id:inc-123",
            "service:payments-service",
            "severity:high",
            "source:incident-summarizer",
            "kind:incident-summary",
            "env:dev",
            "jira_ticket:INC-42",
        ):
            assert tag in tags

    def test_tags_carry_datadog_alert_ids_only(self, dd):
        app, _, _ = dd
        tags = _posted_event(app)["tags"]
        assert "alert_id:7788990011" in tags
        assert "alert_id:a1" not in tags

    def test_no_jira_tag_before_the_ticket_exists(self, dd):
        app, _, _ = dd
        tags = _posted_event(app, INCIDENT_NO_LINKS)["tags"]
        assert not any(t.startswith("jira_ticket:") for t in tags)


# ── Severity mapping ──────────────────────────────────────────────────────────

class TestSeverityMapping:
    @pytest.mark.parametrize("severity,alert_type,priority", [
        ("critical", "error", "normal"),
        ("high", "error", "normal"),
        ("medium", "warning", "low"),
        ("low", "info", "low"),
        ("unknown", "info", "low"),
    ])
    def test_severity_maps_to_alert_type_and_priority(self, dd, severity, alert_type, priority):
        app, _, _ = dd
        event = _posted_event(app, {**INCIDENT, "severity": severity})
        assert event["alert_type"] == alert_type
        assert event["priority"] == priority


# ── API key caching ───────────────────────────────────────────────────────────

class TestApiKeyCaching:
    def test_key_fetched_from_secrets_manager(self, dd):
        app, _, mock_secrets = dd
        with patch("requests.post", return_value=_mock_dd_response()):
            app.handler({"incident_id": "inc-123"}, None)
        mock_secrets.get_secret_value.assert_called_once_with(SecretId=DD_API_KEY_ARN)

    def test_key_cached_across_calls(self, dd):
        app, _, mock_secrets = dd
        with patch("requests.post", return_value=_mock_dd_response()):
            app.handler({"incident_id": "inc-123"}, None)
            app.handler({"incident_id": "inc-123"}, None)
        assert mock_secrets.get_secret_value.call_count == 1
