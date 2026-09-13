import pytest

from tests.conftest import load_function_module


@pytest.fixture()
def normalizer():
    return load_function_module("ingest", "normalize")


def _run(normalizer, event: dict) -> dict | None:
    """The normalized alert as the dict dedup receives, or None."""
    alert = normalizer.normalize(event)
    return None if alert is None else alert.to_dict()


# ── Fixtures ──────────────────────────────────────────────────────────────────

def _cw_event(alarm_name="payments-service-error-rate", state="ALARM", dimension_value="payments-service"):
    return {
        "version": "0",
        "id": "test-event-id-123",
        "source": "aws.cloudwatch",
        "account": "123456789012",
        "time": "2024-01-15T10:30:00Z",
        "region": "us-east-1",
        "detail-type": "CloudWatch Alarm State Change",
        "detail": {
            "alarmName": alarm_name,
            "state": {"value": state, "reason": "Threshold crossed", "timestamp": "2024-01-15T10:30:00Z"},
            "previousState": {"value": "OK", "reason": "OK", "timestamp": "2024-01-15T10:00:00Z"},
            "configuration": {
                "description": "Test alarm",
                "metrics": [
                    {
                        "id": "m1",
                        "metricStat": {
                            "metric": {
                                "namespace": "AWS/Lambda",
                                "name": "Errors",
                                "dimensions": {"FunctionName": dimension_value},
                            },
                            "period": 60,
                            "stat": "Sum",
                        },
                    }
                ],
            },
        },
    }


def _dd_envelope(priority="P1", transition="Triggered", tags=None, alert_type="error"):
    return {
        "source": "datadog",
        "received_at": "2024-01-15T10:30:00+00:00",
        "path": "/webhook/datadog",
        "raw_payload": {
            "id": "dd-alert-abc123",
            "title": "Error rate above threshold",
            "priority": priority,
            "alert_type": alert_type,
            "alert_transition": transition,
            "tags": tags or ["service:payments-service", "env:production"],
            "url": "https://app.datadoghq.com/monitors/99999",
        },
    }


def _gh_envelope(conclusion="failure", workflow_name="CI", repo="org/repo", action="completed", event="workflow_run"):
    return {
        "source": "github",
        "received_at": "2024-01-15T10:30:00+00:00",
        "path": "/webhook/github",
        "github_event": event,
        "raw_payload": {
            "action": action,
            "workflow_run": {
                "id": 1234567890,
                "name": workflow_name,
                "head_branch": "main",
                "conclusion": conclusion,
                "status": "completed",
            },
            "repository": {"full_name": repo, "name": repo.split("/")[-1]},
        },
    }


# ── CloudWatch tests ──────────────────────────────────────────────────────────

class TestCloudWatch:
    def test_alarm_state_returns_open(self, normalizer):
        result = _run(normalizer, _cw_event(state="ALARM"))
        assert result["status"] == "open"
        assert result["source"] == "cloudwatch"

    def test_ok_state_returns_resolved(self, normalizer):
        result = _run(normalizer, _cw_event(state="OK"))
        assert result["status"] == "resolved"

    def test_insufficient_data_returns_open(self, normalizer):
        result = _run(normalizer, _cw_event(state="INSUFFICIENT_DATA"))
        assert result["status"] == "open"

    def test_alarm_name_with_critical_keyword(self, normalizer):
        result = _run(normalizer, _cw_event(alarm_name="payments-service-critical-errors"))
        assert result["severity"] == "critical"

    def test_alarm_name_with_medium_keyword(self, normalizer):
        result = _run(normalizer, _cw_event(alarm_name="api-medium-latency"))
        assert result["severity"] == "medium"

    def test_alarm_default_severity_is_high_for_alarm_state(self, normalizer):
        result = _run(normalizer, _cw_event(alarm_name="no-keyword-alarm", state="ALARM"))
        assert result["severity"] == "high"

    def test_service_extracted_from_dimensions(self, normalizer):
        result = _run(normalizer, _cw_event(dimension_value="payments-service"))
        assert result["affected_service"] == "payments-service"

    def test_alert_id_from_event_id(self, normalizer):
        result = _run(normalizer, _cw_event())
        assert result["alert_id"] == "test-event-id-123"

    def test_raw_payload_is_full_event(self, normalizer):
        event = _cw_event()
        result = _run(normalizer, event)
        assert result["raw_payload"] == event

    def test_received_at_from_event_time(self, normalizer):
        result = _run(normalizer, _cw_event())
        assert result["received_at"] == "2024-01-15T10:30:00Z"


# ── Datadog tests ─────────────────────────────────────────────────────────────

class TestDatadog:
    def test_triggered_returns_open(self, normalizer):
        result = _run(normalizer, _dd_envelope(transition="Triggered"))
        assert result["status"] == "open"
        assert result["source"] == "datadog"

    def test_re_triggered_returns_open(self, normalizer):
        result = _run(normalizer, _dd_envelope(transition="Re-Triggered"))
        assert result["status"] == "open"

    def test_recovered_returns_resolved(self, normalizer):
        result = _run(normalizer, _dd_envelope(transition="Recovered"))
        assert result["status"] == "resolved"

    def test_p1_maps_to_critical(self, normalizer):
        result = _run(normalizer, _dd_envelope(priority="P1"))
        assert result["severity"] == "critical"

    def test_p2_maps_to_high(self, normalizer):
        result = _run(normalizer, _dd_envelope(priority="P2"))
        assert result["severity"] == "high"

    def test_p3_maps_to_medium(self, normalizer):
        result = _run(normalizer, _dd_envelope(priority="P3"))
        assert result["severity"] == "medium"

    def test_p4_maps_to_low(self, normalizer):
        result = _run(normalizer, _dd_envelope(priority="P4"))
        assert result["severity"] == "low"

    def test_alert_type_fallback_when_no_priority(self, normalizer):
        env = _dd_envelope(alert_type="warning")
        del env["raw_payload"]["priority"]
        result = _run(normalizer, env)
        assert result["severity"] == "medium"

    def test_service_extracted_from_tags(self, normalizer):
        result = _run(normalizer, _dd_envelope(tags=["service:checkout-service", "env:prod"]))
        assert result["affected_service"] == "checkout-service"

    def test_no_service_tag_returns_unknown(self, normalizer):
        result = _run(normalizer, _dd_envelope(tags=["env:production"]))
        assert result["affected_service"] == "unknown"

    def test_alert_id_from_payload_id(self, normalizer):
        result = _run(normalizer, _dd_envelope())
        assert result["alert_id"] == "dd-alert-abc123"

    # RC1-370: the real webhook template renders $TAGS as one string, carries
    # the monitor id as alert_id, and prefixes titles with the transition.
    def test_tags_as_comma_separated_string(self, normalizer):
        result = _run(normalizer, _dd_envelope(tags="env:prod, service:checkout-service,team:x"))
        assert result["affected_service"] == "checkout-service"

    def test_empty_tags_string_returns_unknown(self, normalizer):
        env = _dd_envelope()
        env["raw_payload"]["tags"] = ""  # a monitor with no tags renders $TAGS as ""
        result = _run(normalizer, env)
        assert result["affected_service"] == "unknown"

    def test_monitor_id_from_alert_id(self, normalizer):
        env = _dd_envelope()
        env["raw_payload"]["alert_id"] = "99999"
        result = _run(normalizer, env)
        assert result["monitor_id"] == "99999"

    def test_monitor_id_absent_when_payload_lacks_it(self, normalizer):
        result = _run(normalizer, _dd_envelope())
        assert result["monitor_id"] is None

    def test_monitor_id_absent_when_rendered_empty(self, normalizer):
        env = _dd_envelope()
        env["raw_payload"]["alert_id"] = ""
        result = _run(normalizer, env)
        assert result["monitor_id"] is None

    def test_transition_prefix_stripped_from_alert_name(self, normalizer):
        env = _dd_envelope()
        env["raw_payload"]["title"] = "[Triggered on {service:payments-service}] Error rate above threshold"
        result = _run(normalizer, env)
        assert result["alert_name"] == "Error rate above threshold"

    def test_recovered_and_triggered_share_alert_name(self, normalizer):
        a = _dd_envelope(transition="Triggered"); a["raw_payload"]["title"] = "[Triggered] Latency"
        b = _dd_envelope(transition="Recovered"); b["raw_payload"]["title"] = "[Recovered] Latency"
        assert _run(normalizer, a)["alert_name"] == _run(normalizer, b)["alert_name"] == "Latency"

    def test_warn_returns_open(self, normalizer):
        assert _run(normalizer, _dd_envelope(transition="Warn"))["status"] == "open"

    def test_no_data_returns_open(self, normalizer):
        assert _run(normalizer, _dd_envelope(transition="No Data"))["status"] == "open"

    def test_unknown_or_missing_transition_stays_open(self, normalizer):
        env = _dd_envelope()
        del env["raw_payload"]["alert_transition"]
        assert _run(normalizer, env)["status"] == "open"


# ── GitHub Actions tests ──────────────────────────────────────────────────────

class TestGitHub:
    def test_failure_returns_open_high(self, normalizer):
        result = _run(normalizer, _gh_envelope(conclusion="failure"))
        assert result["status"] == "open"
        assert result["severity"] == "high"
        assert result["source"] == "github"

    def test_timed_out_returns_open_high(self, normalizer):
        result = _run(normalizer, _gh_envelope(conclusion="timed_out"))
        assert result["status"] == "open"
        assert result["severity"] == "high"

    def test_cancelled_returns_open_medium(self, normalizer):
        result = _run(normalizer, _gh_envelope(conclusion="cancelled"))
        assert result["status"] == "open"
        assert result["severity"] == "medium"

    def test_startup_failure_returns_open_high(self, normalizer):
        result = _run(normalizer, _gh_envelope(conclusion="startup_failure"))
        assert result["status"] == "open"
        assert result["severity"] == "high"

    def test_unknown_non_success_conclusion_returns_open_medium(self, normalizer):
        result = _run(normalizer, _gh_envelope(conclusion="action_required"))
        assert result["status"] == "open"
        assert result["severity"] == "medium"

    # RC1-373: only completed workflow runs are alerts. RC1-374: a successful
    # one is a recovery (status resolved) that dedup closes an incident with.
    def test_success_is_a_recovery(self, normalizer):
        result = _run(normalizer, _gh_envelope(conclusion="success"))
        assert result["status"] == "resolved"
        assert result["severity"] == "low"
        assert result["alert_name"] == "CI"

    def test_skipped_and_neutral_are_recoveries(self, normalizer):
        assert _run(normalizer, _gh_envelope(conclusion="skipped"))["status"] == "resolved"
        assert _run(normalizer, _gh_envelope(conclusion="neutral"))["status"] == "resolved"

    def test_in_progress_run_is_ignored_without_error(self, normalizer, caplog):
        env = _gh_envelope(action="in_progress")
        env["raw_payload"]["workflow_run"]["conclusion"] = None
        env["raw_payload"]["workflow_run"]["status"] = "in_progress"
        with caplog.at_level("INFO"):
            assert _run(normalizer, env) is None
        assert "ERROR" not in caplog.text
        assert "Ignoring github workflow_run.in_progress" in caplog.text

    def test_requested_run_is_ignored(self, normalizer):
        assert _run(normalizer, _gh_envelope(action="requested")) is None

    def test_workflow_job_event_is_ignored_without_error(self, normalizer, caplog):
        env = {
            "source": "github",
            "received_at": "2024-01-15T10:30:00+00:00",
            "path": "/webhook/github",
            "github_event": "workflow_job",
            "raw_payload": {"action": "completed", "workflow_job": {"id": 1, "conclusion": "success"},
                            "repository": {"full_name": "org/repo"}},
        }
        with caplog.at_level("INFO"):
            assert _run(normalizer, env) is None
        assert "ERROR" not in caplog.text

    def test_push_event_is_ignored(self, normalizer):
        env = {
            "source": "github",
            "received_at": "2024-01-15T10:30:00+00:00",
            "path": "/webhook/github",
            "github_event": "push",
            "raw_payload": {"ref": "refs/heads/main", "commits": [], "repository": {"full_name": "org/repo"}},
        }
        assert _run(normalizer, env) is None

    def test_completed_run_without_conclusion_is_ignored(self, normalizer):
        env = _gh_envelope()
        env["raw_payload"]["workflow_run"]["conclusion"] = None
        assert _run(normalizer, env) is None

    def test_missing_event_header_falls_back_to_payload_shape(self, normalizer):
        env = _gh_envelope(conclusion="failure")
        del env["github_event"]
        assert _run(normalizer, env)["status"] == "open"

    def test_missing_event_header_and_no_workflow_run_is_ignored(self, normalizer):
        env = {"source": "github", "received_at": "2024-01-15T10:30:00+00:00", "path": "/webhook/github",
               "raw_payload": {"action": "completed", "workflow_job": {}}}
        assert _run(normalizer, env) is None

    def test_affected_service_is_repo_full_name(self, normalizer):
        result = _run(normalizer, _gh_envelope(repo="acme/payments-api"))
        assert result["affected_service"] == "acme/payments-api"

    def test_alert_name_is_workflow_name(self, normalizer):
        result = _run(normalizer, _gh_envelope(workflow_name="Deploy to Production"))
        assert result["alert_name"] == "Deploy to Production"

    def test_alert_id_from_run_id(self, normalizer):
        result = _run(normalizer, _gh_envelope())
        assert result["alert_id"] == "1234567890"


# ── Unknown source ────────────────────────────────────────────────────────────

class TestUnknownSource:
    def test_unknown_source_returns_none(self, normalizer):
        result = _run(normalizer, {"source": "pagerduty", "data": {}})
        assert result is None

    def test_missing_source_returns_none(self, normalizer):
        result = _run(normalizer, {"data": "some payload"})
        assert result is None
