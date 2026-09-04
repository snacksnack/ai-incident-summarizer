# ai-incident-summarizer

An AI-powered incident summarization system that ingests alerts from multiple observability sources, deduplicates and correlates them, summarizes incidents using an LLM, and delivers operational summaries to Slack, Jira, and back into Datadog's event timeline.

Built with AWS Lambda (Python), SAM, DynamoDB, Claude, Next.js, and Vercel.

**Live dashboard:** [incidents.hihelloreid.com](https://incidents.hihelloreid.com)

---

## Alert sources

| Source | Role | Integration |
|---|---|---|
| **CloudWatch** | AWS infrastructure alarms (Lambda errors, timeouts, throttles) | Native EventBridge |
| **Datadog** | APM and application-level alerts (error rates, latency, service health) | Webhook via API Gateway |
| **GitHub Actions** | CI/CD pipeline failures — only `workflow_run.completed` events; a failed run opens an incident, a successful one is a recovery that closes it; in-progress runs, `workflow_job` and `push` deliveries are ignored | Webhook via API Gateway |

---

## Architecture

```
CloudWatch          Datadog             GitHub Actions
    │                   │                     │
    ▼                   ▼                     ▼
EventBridge        API Gateway (HMAC validation)
    │                        │
    └──────────┬─────────────┘
               ▼
        Lambda normalizer
        (stateless · shared schema)
               │
               ▼
    ┌─── Dedup + correlation ────────────────┐
    │  Fingerprinting → Time-window grouping │
    │  State store: DynamoDB TTL             │
    │  Recovery → closes the open incident   │
    │  (or is dropped if there is none)      │
    └────────────────────────────────────────┘
               │
        ┌──────┴──────┐
        ▼             ▼
    DynamoDB      LLM summarizer
    (persist      (Claude / GPT-4o)
    raw incident) │
        ▲         ├──► Slack    ──► write back thread_id
        │         ├──► Jira     ──► write back ticket_id
        │         └──► Datadog  ──► write back event_id
        │              (Events API — the summary lands on the
        │               timeline beside the monitors that fed it)
        │
        ▼
  Incident history UI
```

### DynamoDB incident schema

| Field | Description |
|---|---|
| `incident_id` (PK) | Unique incident identifier |
| `source_alerts[]` | Per-alert summaries (id, source, name, severity, status, received_at; `monitor_id` for Datadog alerts) |
| `affected_service` | Service name |
| `severity` | critical / high / medium / low |
| `status` | open / acknowledged / resolved — set to `resolved` by the recovery of an alert the incident holds (CloudWatch OK, Datadog Recovered, GitHub success) |
| `resolved_at` | ISO timestamp of the recovery |
| `recovery_summary` | LLM-generated closing note (same three fields as `llm_summary`) |
| `llm_summary` | LLM-generated summary while open |
| `slack_thread_id` | Enables Slack reply threading |
| `jira_ticket_id` | Linked Jira ticket |
| `datadog_event_id` | Latest Datadog event posted for this incident (all its events share `aggregation_key` `incident:<id>`) |
| `created_at` | ISO timestamp |
| `ttl` | Optional expiry timestamp. TTL is enabled on the table, so any incident carrying this attribute is deleted by DynamoDB once it passes. Neither the pipeline nor the seed script sets it — omit it unless you want the incident to disappear. |

**GSIs:**
- `service-created-index` — query all incidents for a given service
- `status-created-index` — query all open incidents

---

## Project structure

```
ai-incident-summarizer/
├── template.yaml              # SAM template
├── README.md
├── .gitignore
├── events/                    # Sample payloads for local testing
│   ├── cloudwatch.json
│   ├── datadog.json
│   └── github-actions.json
├── functions/
│   ├── normalizer/            # Alert normalizer Lambda
│   │   ├── app.py
│   │   └── requirements.txt
│   ├── dedup/                 # Fingerprinting + time-window grouping
│   │   ├── app.py
│   │   └── requirements.txt
│   ├── summarizer/            # LLM summarizer
│   │   ├── app.py
│   │   └── requirements.txt
│   ├── slack/                 # Slack delivery
│   │   ├── app.py
│   │   └── requirements.txt
│   ├── jira/                  # Jira ticket creation
│   │   ├── app.py
│   │   └── requirements.txt
│   └── datadog_events/        # Datadog Events API write-back
│       ├── app.py
│       └── requirements.txt
├── layers/
│   └── common/                # Shared Lambda layer
│       └── python/
│           └── common/
│               ├── schema.py  # Normalised alert schema
│               └── dynamo.py  # DynamoDB client helpers
└── tests/
    ├── unit/
    └── integration/
```

---

## Prerequisites

- [AWS CLI](https://aws.amazon.com/cli/) configured (`aws configure`)
- [AWS SAM CLI](https://docs.aws.amazon.com/serverless-application-model/latest/developerguide/install-sam-cli.html)
- Python 3.14+
- A Datadog account with API key stored in AWS Secrets Manager
- A Slack app with `chat:write` and `chat:write.public` scopes
- A Jira API token

---

## Environment variables

| Variable | Description |
|---|---|
| `DYNAMODB_TABLE` | DynamoDB incident table name |
| `DD_API_KEY_SECRET_ARN` | Secrets Manager ARN for Datadog API key |
| `SLACK_BOT_TOKEN_SECRET_ARN` | Secrets Manager ARN for Slack bot token |
| `SLACK_CHANNEL_ID` | Target Slack channel for incident alerts |
| `JIRA_API_TOKEN_SECRET_ARN` | Secrets Manager ARN for Jira API token |
| `JIRA_BASE_URL` | Your Jira instance URL |
| `JIRA_PROJECT_KEY` | Jira project key for incident tickets |
| `INCIDENT_DASHBOARD_URL` | Base URL of the incident history UI, linked from Datadog events (empty omits the link) |
| `LLM_PROVIDER` | `claude` or `openai` |
| `CORRELATION_WINDOW_MINUTES` | Alert grouping window in minutes (default: 5) |

---

## Local development

```bash
# Build
sam build

# Run a function locally with a sample event
sam local invoke NormalizerFunction --event events/datadog.json

# Deploy to AWS
sam deploy --guided
```

---

## Seeding the incident dashboard

To populate the incident history UI with realistic demo data:

```bash
pip install boto3

DYNAMODB_TABLE=<table-name> AWS_DEFAULT_REGION=us-east-1 python scripts/seed_dynamo.py
```

Get the table name from the stack outputs:

```bash
aws cloudformation describe-stacks --stack-name ai-incident-summarizer \
  --query "Stacks[0].Outputs[?OutputKey=='IncidentTableName'].OutputValue" \
  --output text
```

The script seeds 90 incidents across 10 services (payments, auth, API gateway, notifications, search, billing, users, CDN, data pipeline, WebSocket) with a mix of open, resolved, and acknowledged statuses. Re-running the script is safe — it upserts by `incident_id` and does not create duplicates.

---

## Webhook endpoints

After `sam deploy`, retrieve the base URL from stack outputs:

```bash
aws cloudformation describe-stacks --stack-name ai-incident-summarizer \
  --query "Stacks[0].Outputs[?OutputKey=='WebhookApiUrl'].OutputValue" \
  --output text
```

| Source | Endpoint |
|---|---|
| GitHub Actions | `POST <WebhookApiUrl>/webhook/github` |
| Datadog | `POST <WebhookApiUrl>/webhook/datadog` |

`WebhookApiUrl` ends in the API stage (`/prod`). A URL without it returns `404 {"message":"Not Found"}` from API Gateway itself, with nothing in the receiver's logs — the symptom to look for when a webhook "sends but nothing arrives".

**Datadog payload template.** The webhook definition lives in Datadog, so `scripts/configure_datadog_webhook.py` is the source of truth for what it sends: the monitor id (`$ALERT_ID`), tags as one comma-separated string, `$ALERT_PRIORITY`, `$ALERT_TYPE`, `$ALERT_TRANSITION` and the event link, on top of Datadog's default fields. Datadog's default template carries none of those, and the normalizer needs them for service, severity, status and the `monitor_id:` tag on the written-back event (RC1-370). Re-run the script if the webhook is recreated:

```bash
DD_API_KEY=… DD_APP_KEY=… python scripts/configure_datadog_webhook.py
```

To notify the pipeline, add `@webhook-incident-summarizer` to a monitor's message. Monitor **318762066** ("incident-summarizer webhook test signal") exists for exactly that: push `incident_summarizer.test_signal` = 1 to trigger it, 0 to recover.

**Which real monitors notify it.** `scripts/wire_datadog_monitors.py` is the source of truth (RC1-375): eight monitors that mean a real outage and rarely flap — the five synthetics checks on www.hihelloreid.com and incidents.hihelloreid.com (uptime, /work render, TLS expiry), the two CI Visibility monitors (production deploy failed, CI pipeline failed) and the daily LLM spend guardrail. Each gets the webhook handle appended to its message, a `service:` tag (`hihelloreid.com`, `incidents.hihelloreid.com`, `delivery-pipeline`, `agent-fleet`) so the incident is filed under a service rather than `unknown`, and a priority (P2 for the two uptime checks and the deploy monitor, P3 for the rest), since `$ALERT_PRIORITY` sets incident severity. The six Program KPI monitors and the seven host-pack monitors are deliberately left out — the KPI sim keeps a monitor tripped by script for weeks, and each alert would be a Slack post, an INC ticket and a model call. Synthetics-backed monitors reject monitor-API edits, so the script writes their message, tags and priority to the synthetics test instead. Re-run it after a monitor is recreated or edited by hand; it changes nothing that already matches:

```bash
DD_API_KEY=… DD_APP_KEY=… python scripts/wire_datadog_monitors.py --dry-run   # then without --dry-run
```

---

## Key design decisions

| Decision | Choice | Rationale |
|---|---|---|
| Runtime | Lambda (Python 3.14, Amazon Linux 2023) | Stateless, zero cost at idle, easy to deploy |
| State management | DynamoDB TTL | Lambda is stateless; window state lives in DynamoDB |
| Secret management | AWS Secrets Manager | API keys never stored in plain text or env vars |
| Deployment | AWS SAM | Native AWS tooling, infrastructure-as-code |
| Observability | Datadog Lambda layer | APM traces, logs, and metrics auto-instrumented |
| Recoveries | Close, never open | A resolved alert (CloudWatch OK, Datadog Recovered, GitHub success) closes the newest open incident for that service holding the same alert, retires the window and fingerprint rows so the next alert starts fresh, and runs the delivery chain once more with a `recovered` flag: Slack reply in the thread, Jira comment plus a Done-category transition when the workflow offers one, Datadog `success` event on the same aggregation key. A recovery with nothing to close is dropped. |
| Datadog write-back | Events API v1, last stop in the delivery chain | The chain is summarizer → Slack → Jira → Datadog so the event carries both links. Each stage is idempotent about its own artifact (thread, ticket) and always hands off, so a re-summary of a live incident reaches the timeline too; `aggregation_key` rolls those up under one row. Reuses the Lambda extension's API key secret — Datadog API keys carry no scopes, so there is no narrower key to mint. |
| Incident history UI | Next.js on Vercel | Next.js API routes call DynamoDB directly as Vercel serverless functions — no API Gateway needed. A single `vercel deploy` produces a shareable URL. React handles the dashboard UI. Chosen over a static S3 + API Gateway approach for simplicity and to gain practical exposure to Vercel, which is widely used in the industry. |

---

## Known limitations


**Datadog webhook signature verification**
Datadog's webhook integration does not support HMAC payload signing natively, unlike GitHub Actions which uses `X-Hub-Signature-256`. Instead, a shared secret is passed via a custom `X-Webhook-Secret` header configured in the Datadog webhook settings and stored in AWS Secrets Manager. The receiver validates the header value using a timing-safe comparison. This is Datadog's recommended approach for webhook authentication.

---

## Evals

The incident summary runs under the shared [agent-evals](https://github.com/snacksnack/agent-evals)
harness (RC1-267), in two layers:

- **Layer 1 — free, on every push.** `pytest` covers the prompt/parser
  contract: `_call_llm` parses the model's response with a bare `json.loads`,
  so the prompt's raw-three-field-JSON clauses are load-bearing. Editing them
  without keeping the contract fails CI instead of silently degrading
  production to the fallback summary.
- **Layer 2 — billed, deliberate.** `python -m evals` binds fixture incidents
  into the shipped prompt, calls the model `template.yaml` pins, and scores
  the output: the contract on real output, the handed-over facts, and that the
  model restates the computed severity rather than re-deciding it. Needs
  `ANTHROPIC_API_KEY` in the environment and
  `pip install -r requirements-evals.txt`.

Runs land in the shared store and render on the public
[quality trend page](https://snacksnack.github.io/agent-evals/) as subject
`incident-summary`; [docs/measuring.md](https://github.com/snacksnack/agent-evals/blob/main/docs/measuring.md)
is the runbook for taking a measurement end to end.

---

## Jira epic

This project is tracked under epic **RC1-31** at [hirereidcollins.atlassian.net](https://hirereidcollins.atlassian.net). The eval suite is RC1-267 under epic RC1-230.
