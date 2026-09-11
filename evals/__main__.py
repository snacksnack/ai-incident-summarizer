"""`python -m evals` — the billed half (RC1-267).

Layer 1 is `pytest` and needs no key. This is layer 2: it binds each fixture
incident into the shipped prompt and calls the model the template pins. The
run/record/exit plumbing is `agent_evals.runner` (RC1-262); what lives here is
this repo's subject, its fixtures, and its key.

The Lambda resolves its key from Secrets Manager; the eval reads
`ANTHROPIC_API_KEY` from the environment instead — deliberately, so a suite
run never needs AWS credentials (ADR-0035: credential resolution stays in the
consumer, spelled the consumer's way).
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import UTC, datetime

from agent_evals import llmobs
from agent_evals.runner import UnknownCase, exit_code, print_result, record_run, select_cases

from evals import fixtures, subject, summarizer


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="evals", description=__doc__)
    parser.add_argument("--case", help="run a single fixture by id")
    parser.add_argument(
        "--show-prompt", action="store_true", help="print the bound prompt and exit"
    )
    args = parser.parse_args(argv)

    if args.show_prompt:
        fixture = fixtures.BY_ID[args.case or "single-cloudwatch"]
        print(summarizer.build_prompt(fixture.incident()))
        return 0

    key = os.environ.get("ANTHROPIC_API_KEY")
    try:
        subject.preflight(key)
    except Exception as exc:
        print(f"cannot run: {exc}", file=sys.stderr)
        return 2

    import anthropic

    client = anthropic.Anthropic(api_key=key, timeout=60.0, max_retries=3)
    try:
        cases = select_cases(subject.CASES, args.case)
    except UnknownCase as exc:
        print(exc, file=sys.stderr)
        return 2

    print(f"{len(cases)} case(s) against {summarizer.model()} — this spends money.\n")
    # RC1-420: billed spend is traced spend. Same ml_app as the Lambda, so the
    # fleet pane shows one row per app; `service:evals` is what keeps this run
    # out of the production spend guardrails (RC1-411). A no-op without
    # DD_API_KEY, so the suite still runs on a bare machine.
    llmobs.enable("incident-summarizer", service="evals")
    started = datetime.now(UTC)
    results = []
    for case in cases:
        with llmobs.case(case.id, input_data=case.input) as traced:
            result = subject.run(case, client)
            traced.record(result)
        results.append(result)
    for r in results:
        print_result(r)

    record_run(subject.version(), started, results)
    return exit_code(results)


if __name__ == "__main__":
    raise SystemExit(main())
