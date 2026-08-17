"""Layer 2: the shipped prompt, run for real and scored (RC1-267).

Binds a fixture incident into `_build_prompt`, calls the Messages API with the
model and max_tokens the Lambda ships, and scores the response. No AWS, no
DynamoDB, no Slack — and nothing is written anywhere but the run store.

## The strongest check is the cheapest one

`returns-the-contracted-json` runs the real model's real output through the
shipped parse — `json.loads`, nothing more forgiving. The Lambda has no lenient
path: a fenced or chatty response throws, production falls back to
`_fallback_summary`, and the on-call engineer gets "Investigate the alert list
manually". Layer 1 proves the prompt still *asks* for raw three-field JSON;
this proves the model actually produces it.

## The model restates severity, it does not decide it

Dedup sets an incident's severity from its first alert and never revisits it.
The prompt hands that answer over. So an output asserting a different level is
a factual error, not a stylistic one — and the `multi-source` fixture is built
to tempt exactly that mistake, pairing a critical incident with alert names
containing the word "high".

Following RC1-258's finding, the check demands the other levels be *absent as
assertions*, not just the right one present — "critical incident, though
individual alerts are high severity" contains both. And severity words inside
quoted alert names must not count as assertions, which is why matching works
on severity phrases rather than bare level words: the alarm
"payments-service-high-error-rate" flattens to "high error", never to
"high severity".

Both guards below were bought by running it, the same way RC1-258's were:

* **an assertion survives an intervening modifier.** The first real run
  described the critical incident as "a critical multi-signal degradation" —
  a correct assertion that adjacent-phrase matching missed, failing the case
  wrongly. Levels now match within two words of an incident noun.
* **known gap, recorded rather than gated:** the same output claimed "three
  concurrent P1 alerts" when the input held one P1 and two highs. Alert-level
  claims are not yet checked; `numbers-trace-to-the-input` stays advisory
  precisely because deciding which derived claims are inventions is the part
  that is not yet precise enough to gate.
"""

from __future__ import annotations

import json
import re
import time

from agent_evals import pricing
from agent_evals.case import Case
from agent_evals.record import CaseResult, CharacteristicResult, SubjectVersion, Usage

from evals import fixtures, summarizer

NAME = "incident-summary"

CONTRACT_FIELDS = ("summary", "likely_cause", "next_step")

_GATED = (
    "returns-the-contracted-json",
    "states-the-facts",
    "restates-the-computed-severity",
)

CASES: tuple[Case, ...] = tuple(
    Case(
        id=f.id,
        input={"fixture": f.id},
        expect=_GATED,
        tags=("incident-summary", f.severity),
    )
    for f in fixtures.FIXTURES
) + (
    Case(
        id="fallback-path",
        input={"fixture": "github-storm"},
        expect=("returns-the-contracted-json", "states-the-facts"),
        tags=("incident-summary", "fallback", "deterministic"),
    ),
)


def preflight(api_key: str | None) -> None:
    if not api_key:
        raise RuntimeError(
            "ANTHROPIC_API_KEY is not set. Layer 1 (`pytest`) needs no key and covers "
            "the prompt/parser contract; this layer calls the model. (The Lambda reads "
            "its key from Secrets Manager; the eval deliberately does not reach into AWS.)"
        )


def version() -> SubjectVersion:
    return SubjectVersion(
        subject=NAME,
        code_version=summarizer.code_version(),
        model=summarizer.model(),
        prompt_version=summarizer.prompt_version(),
    )


def _flatten(text: str) -> str:
    """Collapse separators so hyphens and colons cannot hide a phrase —
    "critical-severity" and "Severity: critical" both flatten to plain words.
    Same normalisation RC1-258 landed on after enumerating spellings lost."""
    return re.sub(r"[\s\-–—_:]+", " ", text.lower())


def _contract(text: str) -> tuple[CharacteristicResult, dict | None]:
    """The shipped parse, verbatim: `json.loads` on the raw response."""
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        return (
            CharacteristicResult(
                name="returns-the-contracted-json",
                passed=False,
                detail=f"json.loads raises ({exc.msg}) — production would fall back: {text[:60]!r}",
            ),
            None,
        )
    keys = set(parsed) if isinstance(parsed, dict) else set()
    ok = keys == set(CONTRACT_FIELDS) and all(
        isinstance(parsed[k], str) and parsed[k].strip() for k in CONTRACT_FIELDS
    )
    return (
        CharacteristicResult(
            name="returns-the-contracted-json",
            passed=ok,
            detail=(
                "raw JSON with exactly the three contracted fields"
                if ok
                else f"fields {sorted(keys)} — the contract is {list(CONTRACT_FIELDS)}"
            ),
        ),
        parsed if ok else None,
    )


#: Digits or digit-words — "5 alerts" and "five alerts" are both the right
#: count. Spelled numbers stop at twelve; no fixture goes higher.
_NUMBER_WORDS = {
    1: "one", 2: "two", 3: "three", 4: "four", 5: "five", 6: "six",
    7: "seven", 8: "eight", 9: "nine", 10: "ten", 11: "eleven", 12: "twelve",
}


def _states_facts(text: str, fixture: fixtures.Fixture) -> CharacteristicResult:
    flat = _flatten(text)
    missing = []
    if _flatten(fixture.affected_service) not in flat:
        missing.append(f"service {fixture.affected_service!r}")
    count = fixture.alert_count
    spelled = _NUMBER_WORDS.get(count, "")
    if str(count) not in text and (not spelled or spelled not in flat):
        missing.append(f"alert count {count}")
    return CharacteristicResult(
        name="states-the-facts",
        passed=not missing,
        detail=(
            "names the service and the alert count"
            if not missing
            else f"missing: {', '.join(missing)}"
        ),
    )


#: The nouns a severity adjective attaches to when it is being *asserted* of
#: this incident. Deliberately excludes the words real outputs use for other
#: things — "rates", "latency", "path", "dependencies" — so "high error rates"
#: and "critical path" never read as severity claims.
_SEVERITY_NOUNS = "incident|outage|degradation|failure|situation|severity"


def _asserts_level(flat: str, level: str) -> bool:
    """Is `level` claimed as this incident's severity?

    Three shapes, precision over recall: "severity [is/of] <level>",
    "<level> priority", and the adjective form — the level word within two
    words of an incident noun, so "critical multi-signal degradation" matches
    without "high error rates" ever doing so.
    """
    if re.search(rf"\bseverity(?:\s+\w+)?\s+{level}\b", flat):
        return True
    if re.search(rf"\b{level}\s+priority\b", flat):
        return True
    return bool(re.search(rf"\b{level}\b(?:\s+[\w/]+){{0,2}}\s+(?:{_SEVERITY_NOUNS})\b", flat))


def _restates_severity(text: str, fixture: fixtures.Fixture) -> CharacteristicResult:
    flat = _flatten(text)
    asserted = [level for level in fixtures.SEVERITIES if _asserts_level(flat, level)]
    correct = fixture.severity in asserted
    contradicting = [s for s in asserted if s != fixture.severity]
    return CharacteristicResult(
        name="restates-the-computed-severity",
        passed=correct and not contradicting,
        detail=(
            f"restates {fixture.severity}"
            if correct and not contradicting
            else (
                f"asserts {', '.join(contradicting)} — dedup set {fixture.severity}"
                if contradicting
                else f"never asserts {fixture.severity} severity"
            )
        ),
    )


def _numbers_trace(text: str, prompt: str) -> CharacteristicResult:
    """Advisory. Every digit run in the output should trace to the prompt.

    Advisory because a derived figure can be legitimate — "five failures over
    eighteen minutes" computes a span the prompt only implies. Reported rather
    than gated so an invented metric is visible without a flaky gate; RC1-230's
    rule is that a flaky gate gets disabled within a week.
    """
    untraced = sorted({m for m in re.findall(r"\d+", text) if m not in prompt})
    return CharacteristicResult(
        name="numbers-trace-to-the-input",
        passed=not untraced,
        detail=(
            "every number appears in the input"
            if not untraced
            else f"not in the input: {', '.join(untraced)}"
        ),
        advisory=True,
    )


def _score(text: str, fixture: fixtures.Fixture, prompt: str) -> list[CharacteristicResult]:
    contract, parsed = _contract(text)
    # Facts are scored on the parsed fields when the contract held, so a fact
    # smuggled into JSON keys or stray prose cannot satisfy the check.
    prose = " ".join(parsed[k] for k in CONTRACT_FIELDS) if parsed else text
    return [
        contract,
        _states_facts(prose, fixture),
        _restates_severity(prose, fixture),
        _numbers_trace(prose, prompt),
    ]


def run(case: Case, client) -> CaseResult:
    fixture = fixtures.BY_ID[case.input["fixture"]]
    incident = fixture.incident()
    prompt = summarizer.build_prompt(incident)

    if "fallback" in case.tags:
        started = time.perf_counter()
        text = json.dumps(summarizer.fallback(incident))
        contract, parsed = _contract(text)
        prose = " ".join(parsed[k] for k in CONTRACT_FIELDS) if parsed else text
        return CaseResult(
            case_id=case.id,
            characteristics=[contract, _states_facts(prose, fixture)],
            usage=Usage(latency_ms=(time.perf_counter() - started) * 1000),
            observations={"output": text, "deterministic": True},
        )

    started = time.perf_counter()
    try:
        response = client.messages.create(
            model=summarizer.model(),
            max_tokens=summarizer.MAX_TOKENS,
            messages=[{"role": "user", "content": prompt}],
        )
        text = "".join(b.text for b in response.content if getattr(b, "type", None) == "text")
    except Exception as exc:
        return CaseResult(
            case_id=case.id,
            usage=Usage(latency_ms=(time.perf_counter() - started) * 1000),
            error=f"{type(exc).__name__}: {exc}",
        )
    latency_ms = (time.perf_counter() - started) * 1000

    input_tokens = getattr(response.usage, "input_tokens", 0)
    output_tokens = getattr(response.usage, "output_tokens", 0)
    return CaseResult(
        case_id=case.id,
        characteristics=_score(text, fixture, prompt),
        usage=Usage(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            # Priced, not counted: the first runs recorded $0, which the trend
            # page dutifully displayed as "free" for a billed suite (RC1-254's
            # exact finding, reproduced here).
            cost_usd=pricing.cost_usd(summarizer.model(), input_tokens, output_tokens),
            latency_ms=latency_ms,
        ),
        observations={
            "severity_expected": fixture.severity,
            # The output itself. A golden whose failures cannot be read
            # afterwards is half a golden.
            "output": text,
        },
    )
