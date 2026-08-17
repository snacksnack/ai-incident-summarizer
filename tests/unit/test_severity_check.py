"""The severity-assertion checker, frozen at its false positives (RC1-267).

The phrases here are not invented — the failing ones came from real model
output. The first billed run described the critical `multi-source` incident as
"a critical multi-signal degradation" and the checker, matching only adjacent
phrases, scored a correct assertion as absent. These tests keep that output
passing, and keep the guard phrases ("high error rates", "critical path")
failing, so the checker can only be changed with both directions in view.
"""

import sys

import pytest

sys.path.insert(0, ".")

# `evals.subject` needs agent-evals, a layer 2 dependency (requirements-evals.txt).
# CI installs only layer 1's requirements and skips these; they run wherever the
# billed suite can run — which is exactly where the checker matters.
pytest.importorskip("agent_evals")

from evals import fixtures, subject  # noqa: E402

MULTI_SOURCE = fixtures.BY_ID["multi-source"]  # severity: critical


def _check(text: str):
    return subject._restates_severity(text, MULTI_SOURCE)


class TestAssertions:
    def test_the_real_output_that_was_scored_wrongly_passes(self):
        """Verbatim from run incident-summary-20260817T020257: a correct
        critical assertion the adjacent-phrase matcher missed."""
        text = (
            "The checkout-api service is experiencing a critical multi-signal "
            "degradation starting at 09:14Z."
        )
        assert _check(text).passed

    def test_plain_phrasings_assert(self):
        for text in (
            "This is a critical incident affecting checkout-api.",
            "Severity: critical. Elevated error rates across sources.",
            "The severity is critical and customer impact is likely.",
            "A critical-severity outage on checkout-api.",
        ):
            assert _check(text).passed, text


class TestGuards:
    def test_high_error_rates_is_not_a_severity_claim(self):
        """The trap the multi-source fixture exists for: 'high' describing
        metrics, quoted from alert names, must not read as re-deciding."""
        result = _check(
            "A critical incident: high error rates alongside high p95 latency "
            "on checkout-api."
        )
        assert result.passed

    def test_critical_path_is_not_a_severity_claim(self):
        result = _check(
            "This critical incident sits on the checkout critical path; review "
            "critical dependencies."
        )
        assert result.passed

    def test_re_decided_severity_still_fails(self):
        result = _check("A high-severity incident on checkout-api.")
        assert not result.passed
        assert "asserts high" in result.detail

    def test_absent_severity_still_fails(self):
        result = _check("checkout-api is degraded; error rates are elevated.")
        assert not result.passed
        assert "never asserts" in result.detail
