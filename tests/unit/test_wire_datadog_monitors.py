import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "scripts"))

import wire_datadog_monitors as wire  # noqa: E402

TARGET = wire.Target(1, "Example", "hihelloreid.com", 2)


def test_message_gains_webhook_handle_and_keeps_email_handle():
    out = wire.plan_message("Site down.\n\n@hire.reid.collins@gmail.com")
    assert out == "Site down.\n\n@hire.reid.collins@gmail.com\n@webhook-incident-summarizer"


def test_message_already_wired_is_unchanged():
    msg = "Test signal.\n@webhook-incident-summarizer"
    assert wire.plan_message(msg) == msg


def test_tags_gain_service_once():
    assert wire.plan_tags(["portfolio"], "hihelloreid.com") == ["portfolio", "service:hihelloreid.com"]
    assert wire.plan_tags(["service:other"], "hihelloreid.com") == ["service:other"]


def test_plan_changes_reports_only_drift():
    current = {"message": "Down. @me", "tags": ["portfolio"], "priority": None}
    changes = wire.plan_changes(current, TARGET)
    assert set(changes) == {"message", "tags", "priority"}
    assert changes["priority"] == 2

    converged = {"message": changes["message"], "tags": changes["tags"], "priority": 2}
    assert wire.plan_changes(converged, TARGET) == {}


def test_every_target_has_a_service_and_a_priority():
    for target in wire.TARGETS:
        assert target.service
        assert target.priority in (1, 2, 3, 4)


def test_synthetics_endpoint_picked_from_check_type():
    assert wire.monitor_test_type({"tags": ["check_type:browser"]}) == "browser"
    assert wire.monitor_test_type({"tags": ["check_type:api-ssl"]}) == "api"
