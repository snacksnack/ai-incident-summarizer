"""The summarizer handler: one model call per generation, then the delivery
chain in order, with the per-stage markers that make a retry resume rather
than repeat (RC1-384, RC1-431). The stages themselves are stubbed here; each
has its own test_delivery_*.py."""
import json
from unittest.mock import ANY, MagicMock, patch

import pytest
from botocore.exceptions import ClientError

from common import aws
from tests.conftest import load_function_module

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
GENERATION = len(INCIDENT["source_alerts"])

LLM_RESPONSE = {
    "summary": "Payments service is experiencing high error rates and latency spikes.",
    "likely_cause": "Possible database connection pool exhaustion.",
    "next_step": "Check database connection metrics and restart if needed.",
}

THREAD_TS = "1705312800.000001"
TICKET = "INC-7"
EVENT_ID = "555"


def _mock_anthropic(response: dict = None):
    mock = MagicMock()
    mock.return_value.messages.create.return_value.content = [
        MagicMock(text=json.dumps(response or LLM_RESPONSE))
    ]
    return mock


def _stage(key: str, value: str) -> MagicMock:
    """A delivery stage that, like the real ones, records its artifact on the incident."""
    def deliver(incident, recovered):
        incident[key] = value
        return value
    return MagicMock(side_effect=deliver)


def _serve(mock_table, incident: dict = INCIDENT) -> None:
    """get_item returns a copy, since the handler writes into what it reads."""
    mock_table.get_item.return_value = {"Item": dict(incident)}


def _serve_fresh(mock_table, incident: dict = INCIDENT) -> None:
    """A fresh copy per get_item, for tests that invoke the handler repeatedly."""
    mock_table.get_item.side_effect = lambda **_: {"Item": dict(incident)}


def _summary_write(mock_table) -> dict:
    writes = [
        c[1] for c in mock_table.update_item.call_args_list
        if c[1]["UpdateExpression"].startswith(("SET llm_summary", "SET recovery_summary"))
    ]
    assert len(writes) == 1, writes
    return writes[0]


def _marker_writes(mock_table) -> dict[str, int]:
    return {
        c[1]["UpdateExpression"].split()[1]: c[1]["ExpressionAttributeValues"][":g"]
        for c in mock_table.update_item.call_args_list
        if c[1]["UpdateExpression"].endswith("_delivered_count = :g")
    }


@pytest.fixture()
def summarizer(monkeypatch):
    monkeypatch.setenv("INCIDENT_TABLE_NAME", INCIDENT_TABLE)
    monkeypatch.setenv("ANTHROPIC_API_KEY_SECRET_ARN", API_KEY_SECRET_ARN)
    monkeypatch.setenv("MODEL_ID", MODEL_ID)

    aws.reset()
    mock_table = MagicMock()
    _serve(mock_table)
    mock_table.update_item.return_value = {}
    aws._tables[INCIDENT_TABLE] = mock_table

    mock_secrets = MagicMock()
    mock_secrets.get_secret_value.return_value = {"SecretString": API_KEY}
    aws._secrets_client = mock_secrets

    app = load_function_module("summarizer")
    app._anthropic_client = None
    app._STAGES = (
        ("slack", _stage("slack_thread_id", THREAD_TS)),
        ("jira", _stage("jira_ticket_id", TICKET)),
        ("datadog", _stage("datadog_event_id", EVENT_ID)),
    )
    yield app, mock_table, mock_secrets


def _stages(app) -> dict[str, MagicMock]:
    return dict(app._STAGES)


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
        stored = json.loads(_summary_write(mock_table)["ExpressionAttributeValues"][":s"])
        assert "summary" in stored
        assert "likely_cause" in stored
        assert "next_step" in stored

    def test_returns_incident_id_summary_and_artifact_ids(self, summarizer):
        app, _, _ = summarizer
        with patch("anthropic.Anthropic", _mock_anthropic()):
            result = app.handler({"incident_id": "inc-123"}, None)
        assert result["incident_id"] == "inc-123"
        assert json.loads(result["llm_summary"]) == LLM_RESPONSE
        assert result["slack_thread_id"] == THREAD_TS
        assert result["jira_ticket_id"] == TICKET
        assert result["datadog_event_id"] == EVENT_ID
        assert result["delivered"] == ["slack", "jira", "datadog"]

    def test_fallback_written_to_dynamodb_on_llm_error(self, summarizer):
        app, mock_table, _ = summarizer
        mock_anthropic = MagicMock()
        mock_anthropic.return_value.messages.create.side_effect = Exception("API error")
        with patch("anthropic.Anthropic", mock_anthropic):
            result = app.handler({"incident_id": "inc-123"}, None)
        assert result is not None
        stored = json.loads(_summary_write(mock_table)["ExpressionAttributeValues"][":s"])
        assert "summary" in stored
        assert "likely_cause" in stored
        assert "next_step" in stored
        # A fallback summary is still delivered; the responder must hear.
        assert result["delivered"] == ["slack", "jira", "datadog"]

    def test_returns_none_when_no_incident_id(self, summarizer):
        app, _, _ = summarizer
        assert app.handler({}, None) is None
        _stages(app)["slack"].assert_not_called()

    def test_returns_none_when_incident_not_found(self, summarizer):
        app, mock_table, _ = summarizer
        mock_table.get_item.return_value = {}
        assert app.handler({"incident_id": "nonexistent"}, None) is None
        _stages(app)["slack"].assert_not_called()


# ── API key caching tests ─────────────────────────────────────────────────────

class TestApiKey:
    def test_api_key_retrieved_from_secrets_manager(self, summarizer):
        app, _, mock_secrets = summarizer
        with patch("anthropic.Anthropic", _mock_anthropic()):
            app.handler({"incident_id": "inc-123"}, None)
        mock_secrets.get_secret_value.assert_called_once_with(SecretId=API_KEY_SECRET_ARN)

    def test_api_key_cached_across_calls(self, summarizer):
        app, mock_table, mock_secrets = summarizer
        _serve_fresh(mock_table)
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


# ── Recovery (RC1-374) ───────────────────────────────────────────────────────

RESOLVED_INCIDENT = {**INCIDENT, "status": "resolved", "resolved_at": "2024-01-15T10:42:30Z",
                     "llm_summary": json.dumps(LLM_RESPONSE)}
RECOVERY_RESPONSE = {
    "summary": "Payments recovered after 42 minutes.",
    "likely_cause": "Connection pool exhaustion, cleared by the restart.",
    "next_step": "Raise the pool ceiling and add an alarm on saturation.",
}


class TestRecovery:
    def _run(self, app, mock_table, response=RECOVERY_RESPONSE, incident=RESOLVED_INCIDENT):
        _serve(mock_table, incident)
        with patch("anthropic.Anthropic", _mock_anthropic(response)) as mock_anthropic:
            result = app.handler({"incident_id": "inc-123", "recovered": True}, None)
        return result, mock_anthropic

    def test_writes_recovery_summary_not_llm_summary(self, summarizer):
        app, mock_table, _ = summarizer
        self._run(app, mock_table)
        kwargs = _summary_write(mock_table)
        # The claim marker rides along in the same update (RC1-384); what this
        # test guards is that the *summary* lands in recovery_summary and
        # leaves llm_summary alone.
        assert "SET recovery_summary = :s" in kwargs["UpdateExpression"]
        assert "llm_summary" not in kwargs["UpdateExpression"]
        assert json.loads(kwargs["ExpressionAttributeValues"][":s"]) == RECOVERY_RESPONSE

    def test_recovery_prompt_describes_the_recovery(self, summarizer):
        app, mock_table, _ = summarizer
        _, mock_anthropic = self._run(app, mock_table)
        prompt = mock_anthropic.return_value.messages.create.call_args[1]["messages"][0]["content"]
        assert "RECOVERED" in prompt
        assert "42m 30s" in prompt
        assert "2024-01-15T10:42:30Z" in prompt
        assert LLM_RESPONSE["summary"] in prompt

    def test_recovery_delivers_every_stage_with_the_flag(self, summarizer):
        app, mock_table, _ = summarizer
        self._run(app, mock_table)
        for stage in _stages(app).values():
            stage.assert_called_once_with(ANY, True)

    def test_recovery_fallback_mentions_duration(self, summarizer):
        app, mock_table, _ = summarizer
        _serve(mock_table, RESOLVED_INCIDENT)
        failing = MagicMock()
        failing.return_value.messages.create.side_effect = RuntimeError("boom")
        with patch("anthropic.Anthropic", failing):
            app.handler({"incident_id": "inc-123", "recovered": True}, None)
        written = json.loads(_summary_write(mock_table)["ExpressionAttributeValues"][":s"])
        assert "recovered after 42m 30s" in written["summary"]

    def test_open_incident_delivery_carries_no_flag(self, summarizer):
        app, mock_table, _ = summarizer
        with patch("anthropic.Anthropic", _mock_anthropic()):
            app.handler({"incident_id": "inc-123"}, None)
        for stage in _stages(app).values():
            stage.assert_called_once_with(ANY, False)


# ── One summary per generation (RC1-384) ─────────────────────────────────────

class TestGenerations:
    """Ingest invokes this function asynchronously, so Lambda retries a failed
    invocation twice, and two alerts landing seconds apart run as concurrent
    invocations for consecutive generations. The summary write carries the
    generation it summarized and refuses to go backwards.
    """

    @staticmethod
    def _conditional_check_failed():
        return ClientError(
            {"Error": {"Code": "ConditionalCheckFailedException", "Message": "no"}},
            "UpdateItem",
        )

    def test_claims_the_generation_it_summarized(self, summarizer):
        app, mock_table, _ = summarizer
        with patch("anthropic.Anthropic", _mock_anthropic()):
            app.handler({"incident_id": "inc-123"}, None)
        kwargs = _summary_write(mock_table)
        assert kwargs["ExpressionAttributeValues"][":g"] == GENERATION
        assert "summarized_alert_count" in kwargs["UpdateExpression"]
        assert "attribute_not_exists(summarized_alert_count)" in kwargs["ConditionExpression"]
        assert "summarized_alert_count < :g" in kwargs["ConditionExpression"]

    def test_retry_of_a_summarized_generation_skips_the_model_and_resumes_delivery(self, summarizer):
        """The invocation died after Slack. The retry must not call the model
        or post to Slack again, and must still do Jira and Datadog."""
        app, mock_table, _ = summarizer
        _serve(mock_table, {**INCIDENT, "llm_summary": json.dumps(LLM_RESPONSE),
                            "summarized_alert_count": GENERATION, "slack_delivered_count": GENERATION,
                            "slack_thread_id": THREAD_TS})
        mock_anthropic = _mock_anthropic()
        with patch("anthropic.Anthropic", mock_anthropic):
            result = app.handler({"incident_id": "inc-123"}, None)
        mock_anthropic.return_value.messages.create.assert_not_called()
        stages = _stages(app)
        stages["slack"].assert_not_called()
        stages["jira"].assert_called_once()
        stages["datadog"].assert_called_once()
        assert result["delivered"] == ["jira", "datadog"]
        assert json.loads(result["llm_summary"]) == LLM_RESPONSE

    def test_superseded_generation_stops_before_delivery(self, summarizer):
        """A newer alert joined the window and its invocation already
        summarized generation 3. This generation-2 invocation would only post
        a stale summary."""
        app, mock_table, _ = summarizer
        _serve(mock_table, {**INCIDENT, "summarized_alert_count": GENERATION + 1})
        mock_anthropic = _mock_anthropic()
        with patch("anthropic.Anthropic", mock_anthropic):
            assert app.handler({"incident_id": "inc-123"}, None) is None
        mock_anthropic.return_value.messages.create.assert_not_called()
        for stage in _stages(app).values():
            stage.assert_not_called()

    def test_fully_delivered_generation_posts_nothing(self, summarizer):
        app, mock_table, _ = summarizer
        _serve(mock_table, {**INCIDENT, "llm_summary": json.dumps(LLM_RESPONSE),
                            "summarized_alert_count": GENERATION, "slack_delivered_count": GENERATION,
                            "jira_delivered_count": GENERATION, "datadog_delivered_count": GENERATION,
                            "slack_thread_id": THREAD_TS, "jira_ticket_id": TICKET, "datadog_event_id": EVENT_ID})
        with patch("anthropic.Anthropic", _mock_anthropic()):
            result = app.handler({"incident_id": "inc-123"}, None)
        for stage in _stages(app).values():
            stage.assert_not_called()
        mock_table.update_item.assert_not_called()
        assert result["delivered"] == []
        assert result["jira_ticket_id"] == TICKET

    def test_concurrent_claim_re_reads_and_delivers_the_stored_summary(self, summarizer):
        """Between this invocation's read and its write, another invocation
        claimed the same generation. Deliver what it stored, not ours."""
        app, mock_table, _ = summarizer
        theirs = {"summary": "theirs", "likely_cause": "theirs", "next_step": "theirs"}
        claimed = {**INCIDENT, "llm_summary": json.dumps(theirs), "summarized_alert_count": GENERATION}
        mock_table.get_item.side_effect = [{"Item": dict(INCIDENT)}, {"Item": dict(claimed)}]

        def update(**kwargs):
            if kwargs["UpdateExpression"].startswith("SET llm_summary"):
                raise self._conditional_check_failed()
            return {}
        mock_table.update_item.side_effect = update

        with patch("anthropic.Anthropic", _mock_anthropic()):
            result = app.handler({"incident_id": "inc-123"}, None)
        assert json.loads(result["llm_summary"]) == theirs
        assert _stages(app)["slack"].call_args[0][0]["llm_summary"] == json.dumps(theirs)

    def test_recovery_claims_a_separate_marker(self, summarizer):
        """A recovery summary is a different field and must not be blocked by
        the open-incident claim, or a resolved incident never announces itself."""
        app, mock_table, _ = summarizer
        _serve(mock_table, {**RESOLVED_INCIDENT, "summarized_alert_count": GENERATION})
        with patch("anthropic.Anthropic", _mock_anthropic()):
            app.handler({"incident_id": "inc-123", "recovered": True}, None)
        kwargs = _summary_write(mock_table)
        assert "recovery_summarized_count" in kwargs["UpdateExpression"]
        assert "summarized_alert_count" not in kwargs["UpdateExpression"]
        _stages(app)["slack"].assert_called_once()

    def test_a_real_dynamodb_error_is_not_swallowed(self, summarizer):
        """Only the conditional check means 'already claimed'. Anything else
        is a broken table and must page rather than look like a duplicate."""
        app, mock_table, _ = summarizer
        mock_table.update_item.side_effect = ClientError(
            {"Error": {"Code": "ProvisionedThroughputExceededException", "Message": "slow down"}},
            "UpdateItem",
        )
        with patch("anthropic.Anthropic", _mock_anthropic()):
            with pytest.raises(ClientError):
                app.handler({"incident_id": "inc-123"}, None)
        _stages(app)["slack"].assert_not_called()


# ── The delivery chain (RC1-431) ──────────────────────────────────────────────

class TestDeliveryChain:
    def test_stages_run_in_order_slack_jira_datadog(self, summarizer):
        """The Datadog event carries the Slack and Jira links, so it is last."""
        app, _, _ = summarizer
        order = []
        for name, stage in app._STAGES:
            stage.side_effect = lambda incident, recovered, name=name: order.append(name) or name
        with patch("anthropic.Anthropic", _mock_anthropic()):
            app.handler({"incident_id": "inc-123"}, None)
        assert order == ["slack", "jira", "datadog"]

    def test_each_stage_marks_its_generation(self, summarizer):
        app, mock_table, _ = summarizer
        with patch("anthropic.Anthropic", _mock_anthropic()):
            app.handler({"incident_id": "inc-123"}, None)
        assert _marker_writes(mock_table) == {
            "slack_delivered_count": GENERATION,
            "jira_delivered_count": GENERATION,
            "datadog_delivered_count": GENERATION,
        }

    def test_later_stages_see_what_earlier_ones_wrote(self, summarizer):
        app, _, _ = summarizer
        with patch("anthropic.Anthropic", _mock_anthropic()):
            app.handler({"incident_id": "inc-123"}, None)
        seen_by_datadog = _stages(app)["datadog"].call_args[0][0]
        assert seen_by_datadog["slack_thread_id"] == THREAD_TS
        assert seen_by_datadog["jira_ticket_id"] == TICKET

    def test_a_failing_stage_stops_the_chain_with_its_marker_unwritten(self, summarizer):
        """Slack failed after three attempts. The exception reaches Lambda, so
        it retries; Jira and Datadog wait for that retry rather than running
        ahead of the Slack post the Jira ticket links to."""
        app, mock_table, _ = summarizer
        stages = _stages(app)
        stages["slack"].side_effect = RuntimeError("slack is down")
        with patch("anthropic.Anthropic", _mock_anthropic()):
            with pytest.raises(RuntimeError):
                app.handler({"incident_id": "inc-123"}, None)
        stages["jira"].assert_not_called()
        stages["datadog"].assert_not_called()
        assert _marker_writes(mock_table) == {}
        # The summary itself was claimed, so the retry skips the model call.
        assert _summary_write(mock_table)["ExpressionAttributeValues"][":g"] == GENERATION

    def test_a_newer_generation_keeps_its_marker(self, summarizer):
        """The marker write is conditional: a generation-3 invocation that
        already marked Slack must not be rewound to 2 by this one."""
        app, mock_table, _ = summarizer

        def update(**kwargs):
            if kwargs["UpdateExpression"].startswith("SET slack_delivered_count"):
                raise ClientError({"Error": {"Code": "ConditionalCheckFailedException", "Message": "no"}}, "UpdateItem")
            return {}
        mock_table.update_item.side_effect = update
        with patch("anthropic.Anthropic", _mock_anthropic()):
            result = app.handler({"incident_id": "inc-123"}, None)
        assert result["delivered"] == ["slack", "jira", "datadog"]


# ── The Anthropic client is reused, not rebuilt (RC1-385) ─────────────────────

class TestClientReuse:
    """A client built per invocation leaves an httpx connection pool behind
    each time and pays for a fresh TLS handshake.

    Written while chasing the python3.14 hang (RC1-385) on the theory that the
    leak was its cause. It was not — the cause was the 128 MB memory ceiling,
    fixed in template.yaml. These tests stay because the reuse is right on its
    own terms, not because they guard against that bug.
    """

    def test_client_is_built_once_across_invocations(self, summarizer):
        app, mock_table, _ = summarizer
        _serve_fresh(mock_table)
        mock_anthropic = _mock_anthropic()
        with patch("anthropic.Anthropic", mock_anthropic):
            app.handler({"incident_id": "inc-123"}, None)
            app.handler({"incident_id": "inc-123"}, None)
            app.handler({"incident_id": "inc-123"}, None)
        assert mock_anthropic.call_count == 1
        assert mock_anthropic.return_value.messages.create.call_count == 3

    def test_client_is_cached_on_the_module(self, summarizer):
        app, _, _ = summarizer
        assert app._anthropic_client is None
        with patch("anthropic.Anthropic", _mock_anthropic()):
            app.handler({"incident_id": "inc-123"}, None)
        assert app._anthropic_client is not None

    def test_still_sends_the_model_from_the_env_var_when_reused(self, summarizer):
        """Reuse must not freeze configuration that is read per call."""
        app, mock_table, _ = summarizer
        _serve_fresh(mock_table)
        mock_anthropic = _mock_anthropic()
        with patch("anthropic.Anthropic", mock_anthropic):
            app.handler({"incident_id": "inc-123"}, None)
            app.handler({"incident_id": "inc-123"}, None)
        calls = mock_anthropic.return_value.messages.create.call_args_list
        assert len(calls) == 2
        for call in calls:
            assert call[1]["model"] == MODEL_ID
