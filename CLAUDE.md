# CLAUDE.md — conventions for reviewers and AI sessions

One page, under 6,000 characters so the PR review agent reads it whole. The
long story is in the README; the hard-won reasons are in `template.yaml`'s
comments, which are worth reading before changing anything in `Globals`.

## What this is

An alert pipeline on AWS Lambda, deployed with SAM. CloudWatch, Datadog and
GitHub Actions alerts arrive at API Gateway or EventBridge, are normalised to
one schema, deduplicated and grouped into an incident, summarised by Claude,
then delivered to Slack, Jira and the Datadog event timeline. A Next.js
incident history UI on Vercel reads DynamoDB directly.

## Layout

```
template.yaml         SAM: seven functions, tables, API, the Globals block
functions/
  webhook_receiver/   HMAC / shared-secret validation at the edge
  normalizer/         any source -> the shared alert schema
  dedup/              fingerprint, time-window grouping, recovery close
  summarizer/         the Claude call and the fallback summary
  slack_notifier/ jira_creator/ datadog_events/    the delivery chain
layers/common/python/common/
  schema.py dynamo.py fingerprint.py duration.py
events/               sample payloads for `sam local invoke`
tests/unit/ tests/integration/   pytest; integration uses moto
evals/                the billed agent-evals subject (incident-summary)
frontend/             Next.js incident history UI, deployed to Vercel
```

Each function has its own `requirements.txt`; shared code goes in the layer.

## Conventions (hold a change to these)

- **Everything runs on python3.14 / Amazon Linux 2023**, deliberately the same
  interpreter locally, in both CI workflows and at runtime.
- **Never take a function back to 128 MB.** `Globals.MemorySize: 256` is a
  floor and the summarizer overrides it to 512. This is the RC1-385 finding,
  not a tuning preference: at 128 MB the summarizer finished its handler and
  then hung until the deadline with no exception. When diagnosing a silent
  Lambda hang, read `Max Memory Used` in the REPORT line first — equal to
  `Memory Size` means pinned at the limit, and that is the bug.
- **The Datadog layer version is pinned and must move with the runtime line.**
  The Python version is baked into the *layer name* and the handler is
  Datadog's wrapper, so a mismatch fails at import on the first invocation of
  every function, behind a green CloudFormation deploy. Datadog's resource
  policy denies `ListLayerVersions`, so probe upward with `get-layer-version`
  rather than listing.
- **Handlers are Datadog-wrapped**: `Handler: datadog_lambda.handler.handler`
  with the real entry point in `DD_LAMBDA_HANDLER`. A function that sets its
  own `Handler` loses tracing silently. LLM Observability rides the same
  wrapper: `DD_LLMOBS_ENABLED`, `DD_LLMOBS_ML_APP` and `DD_SERVICE` sit in
  Globals, the last two both `incident-summarizer` (RC1-419); keep them equal.
- **Secrets live in AWS Secrets Manager**, never in environment variables or
  the template. Secret *shape* matters — a JSON blob and a raw string are not
  interchangeable, and getting it wrong fails only in production (RC1-371).
- **Recoveries close, never open.** A resolved alert closes the newest open
  incident for that service holding the same alert, retires the window and
  fingerprint rows, and runs the delivery chain once more with a `recovered`
  flag. A recovery with nothing to close is **dropped**, not turned into an
  incident.
- **Never trust DynamoDB's TTL sweep for correctness.** Expired items are
  deleted within ~48 hours of expiry, not at expiry, so a condition expression
  must test `ttl` itself: `Attr("...").not_exists() | Attr("ttl").lte(now)`.
  Trusting the sweep turned a 5-minute dedup window into "5 minutes to 2 days"
  (RC1-372).
- **The delivery chain is summarizer → Slack → Jira → Datadog**, in that order,
  so the Datadog event carries both links. Every stage is idempotent about its
  own artifact (thread, ticket, event) and **always hands off**, so a
  re-summary of a live incident still reaches the timeline; `aggregation_key`
  rolls those up under one row.
- **The prompt's JSON clauses are load-bearing.** `_call_llm` parses with a
  bare `json.loads`, so editing the raw-three-field-JSON wording without
  keeping the contract degrades production to the fallback summary. A free
  pytest layer guards exactly this.
- **The model restates the computed severity**, it does not re-decide it.
  Deterministic Python owns severity, correlation and fingerprinting.
- **Function `requirements.txt` should pin, not floor.** Floors let two
  consecutive `sam build`s ship different `anthropic` versions (1.3.0 on 09-04,
  1.4.0 on 09-05, which pulled in httpx 2). RC1-386 is pinning them; add new
  dependencies pinned.

## Testing

- `python -m pytest tests/ -v` — `tests/unit/` is offline, `tests/integration/`
  uses moto for DynamoDB. Test deps: `pip install -r tests/requirements-test.txt`.
- `sam validate --lint` runs in CI and catches template errors that a green
  deploy would otherwise hide.
- `python -m evals` is **billed** and needs `ANTHROPIC_API_KEY` plus
  `requirements-evals.txt`; it scores the shipped prompt on real output and
  records to the shared agent-evals store as subject `incident-summary`.

## Commands

```bash
sam build && sam local invoke NormalizerFunction -e events/cloudwatch.json
sam validate --lint
python -m pytest tests/ -v
sam deploy --no-confirm-changeset          # CI does this on push to main
```

## Workflow

One branch per ticket, `rc1-NNN-slug`; never commit on `main`. Commit subject
`RC1-NNN: what changed`, short body, **no Co-Authored-By trailer**. Claude
opens the PR; Reid merges. `ci.yml` runs tests plus `sam validate` on every
push and PR; `deploy.yml` runs `sam build` + `sam deploy` on push to `main`,
then ships the frontend to Vercel. A green CloudFormation deploy is not proof
the functions run — verify an invocation.
