"""Layer 1: the prompt/parser contract, checked without spending anything (RC1-267).

`_call_llm` parses the model's response with a bare `json.loads`. The prompt's
output-contract clauses — exactly three named fields, raw JSON, no markdown —
are the only thing keeping that parse alive. Edit the prompt without keeping
the contract and production silently degrades to `_fallback_summary`. These
tests make that edit fail the build instead.

No credentials, no token cost: everything here reads the shipped source and
the committed fixtures.
"""

import json
import re
import sys

import pytest

sys.path.insert(0, ".")

from evals import fixtures, summarizer  # noqa: E402


# ── The output contract the parser depends on ─────────────────────────────────

class TestOutputContract:
    def test_prompt_names_exactly_the_three_parsed_fields(self):
        prompt = summarizer.build_prompt(fixtures.FIXTURES[0].incident())
        for field in ("summary", "likely_cause", "next_step"):
            assert f'"{field}"' in prompt, f"the prompt no longer asks for {field!r}"

    def test_prompt_forbids_anything_but_raw_json(self):
        prompt = summarizer.build_prompt(fixtures.FIXTURES[0].incident())
        assert "Return only the JSON object" in prompt
        assert "markdown" in prompt.lower()

    def test_contract_shaped_response_survives_the_shipped_parse(self):
        response = json.dumps(
            {"summary": "s", "likely_cause": "c", "next_step": "n"}
        )
        parsed = json.loads(response)  # the parse `_call_llm` performs, verbatim
        assert set(parsed) == {"summary", "likely_cause", "next_step"}

    def test_fenced_response_is_what_the_contract_prevents(self):
        """Why the clauses above gate: the parse has no lenient path."""
        with pytest.raises(json.JSONDecodeError):
            json.loads('```json\n{"summary": "s"}\n```')


# ── The prompt states the facts the fixtures freeze ───────────────────────────

class TestPromptFacts:
    @pytest.mark.parametrize("fixture", fixtures.FIXTURES, ids=lambda f: f.id)
    def test_prompt_hands_over_every_fact(self, fixture):
        prompt = summarizer.build_prompt(fixture.incident())
        assert fixture.affected_service in prompt
        assert fixture.severity in prompt
        assert f"Alert count: {fixture.alert_count}" in prompt
        for alert in fixture.alerts:
            assert alert.alert_name in prompt
        assert fixture.alerts[0].received_at in prompt
        assert fixture.alerts[-1].received_at in prompt


# ── The fixtures mirror the system that exists ────────────────────────────────

class TestFixturesMirrorDedup:
    def test_fixture_severity_is_the_first_alerts(self):
        """Dedup's rule, restated so a fixture cannot drift from it."""
        for fixture in fixtures.FIXTURES:
            assert fixture.severity == fixture.alerts[0].severity

    def test_dedup_still_never_rewrites_severity_on_append(self):
        """The rule the fixtures mirror, asserted against the shipped source.

        Dedup sets `severity` when it creates an incident; the append path
        must not touch it. If this fails, incident severity has become
        mutable and the fixtures (and the severity check built on them) need
        rethinking — which is exactly the conversation this test forces.
        """
        source = (summarizer.REPO_ROOT / "functions" / "dedup" / "app.py").read_text()
        expressions = re.findall(r"UpdateExpression=\((.*?)\)", source, flags=re.DOTALL)
        assert expressions, "dedup no longer builds an UpdateExpression — re-read the append path"
        for expression in expressions:
            assert "severity" not in expression

    def test_severities_are_the_normalizer_vocabulary(self):
        source = (summarizer.REPO_ROOT / "functions" / "normalizer" / "app.py").read_text()
        match = re.search(r"_SEVERITY_KEYWORDS = \[(.*?)\]", source)
        assert match, "normalizer no longer declares _SEVERITY_KEYWORDS"
        shipped = tuple(re.findall(r'"(\w+)"', match.group(1)))
        assert fixtures.SEVERITIES == shipped


# ── The fallback path holds the same must-say facts ───────────────────────────

class TestFallback:
    def test_fallback_meets_the_contract(self):
        fixture = fixtures.BY_ID["github-storm"]
        fallback = summarizer.fallback(fixture.incident())
        assert set(fallback) == {"summary", "likely_cause", "next_step"}

    def test_fallback_states_service_and_count(self):
        fixture = fixtures.BY_ID["github-storm"]
        fallback = summarizer.fallback(fixture.incident())
        assert fixture.affected_service in fallback["summary"]
        assert str(fixture.alert_count) in fallback["summary"]


# ── Version attribution moves with the artifacts ──────────────────────────────

class TestVersions:
    def test_model_is_read_from_the_template_pin(self):
        assert summarizer.model().startswith("claude-")

    def test_versions_are_stable_within_a_run(self):
        assert summarizer.prompt_version() == summarizer.prompt_version()
        assert summarizer.prompt_version().startswith("prompt-sha256:")
        assert summarizer.code_version().startswith("app-sha256:")
