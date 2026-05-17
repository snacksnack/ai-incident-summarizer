# ai-incident-summarizer

An AI-powered incident summarization system that ingests alerts from multiple observability sources, deduplicates and correlates them, summarizes incidents using an LLM, and delivers operational summaries to Slack and Jira.

Built with AWS Lambda (Python), SAM, DynamoDB, and Claude / GPT-4o.

---

## Alert sources

| Source | Role | Integration |
|---|---|---|
| **CloudWatch** | AWS infrastructure alarms (Lambda errors, timeouts, throttles) | Native EventBridge |
| **Datadog** | APM and application-level alerts (error rates, latency, service health) | Webhook via API Gateway |
| **GitHub Actions** | CI/CD pipeline failures | Webhook via API Gateway |

---

## Architecture

```
CloudWatch          Datadog             GitHub Actions
    │                   │                     │
    ▼                   ▼                     ▼
EventBridge        API Gateway
    │                   │
    │         Lambda webhook_receiver
    │         (HMAC validation · 401/400/202)
    │                   │
    └──────────┬─────────────┘
               ▼
        Lambda normalizer
        (stateless · shared schema)
               │
               ▼
    ┌─── Dedup + correlation ────────────────┐
    │  Fingerprinting → Time-window grouping │
    │  State store: DynamoDB TTL             │
    └────────────────────────────────────────┘
               │
        ┌──────┴──────┐
        ▼             ▼
    DynamoDB      LLM summarizer
    (persist      (Claude / GPT-4o)
    raw incident) │
        ▲         ├──► Slack  ──► write back thread_id
        │         └──► Jira   ──► write back ticket_id
        │
        ▼
  Incident history UI
```

### DynamoDB incident schema

**Table: `IncidentTable`** — on-demand billing, TTL enabled on `ttl` attribute.

| Field | Type | Description |
|---|---|---|
| `incident_id` | S — PK | UUID assigned when the incident window opens |
| `source_alerts[]` | L | Alert summaries (`alert_id`, `source`, `alert_name`, `severity`, `status`, `received_at`) — no raw payloads |
| `affected_service` | S | Service name; GSI partition key |
| `severity` | S | `critical` / `high` / `medium` / `low` |
| `status` | S — GSI PK | `open` / `acknowledged` / `resolved` |
| `llm_summary` | S | LLM-generated incident summary |
| `slack_thread_id` | S | Populated after Slack delivery; enables reply threading |
| `jira_ticket_id` | S | Populated after Jira ticket creation |
| `created_at` | S — GSI SK | ISO 8601 timestamp; sort key for both GSIs |
| `ttl` | N | Unix epoch; resolved incidents expire after 30 days |

**GSIs:**

| Index | PK | SK | Use case |
|---|---|---|---|
| `service-created-index` | `affected_service` | `created_at` | All incidents for a given service, newest first |
| `status-created-index` | `status` | `created_at` | All open (or resolved/acknowledged) incidents, newest first |

**Sample query — all open incidents for `payments-service`:**

```python
table.query(
    IndexName="service-created-index",
    KeyConditionExpression=Key("affected_service").eq("payments-service"),
    FilterExpression=Attr("status").eq("open"),
    ScanIndexForward=False,  # newest first
)
```

---

## Webhook endpoints

After `sam deploy`, retrieve the base URL from the stack outputs:

```bash
aws cloudformation describe-stacks --stack-name <stack-name> \
  --query "Stacks[0].Outputs[?OutputKey=='WebhookApiUrl'].OutputValue" \
  --output text
```

| Source | Method | Path |
|---|---|---|
| GitHub Actions | POST | `<WebhookApiUrl>/webhook/github` |
| Datadog | POST | `<WebhookApiUrl>/webhook/datadog` |

CloudWatch alerts are delivered via EventBridge and do not use these endpoints.

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
│   ├── webhook_receiver/      # HMAC validation + downstream forwarding
│   │   ├── app.py
│   │   └── requirements.txt
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
│   └── jira/                  # Jira ticket creation
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
- Python 3.11+
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

## Key design decisions

| Decision | Choice | Rationale |
|---|---|---|
| Runtime | Lambda (Python 3.11) | Stateless, zero cost at idle, easy to deploy |
| State management | DynamoDB TTL | Lambda is stateless; window state lives in DynamoDB |
| Secret management | AWS Secrets Manager | API keys never stored in plain text or env vars |
| Deployment | AWS SAM | Native AWS tooling, infrastructure-as-code |
| Observability | Datadog Lambda layer | APM traces, logs, and metrics auto-instrumented |

---

## Jira epic

This project is tracked under epic **RC1-31** at [hirereidcollins.atlassian.net](https://hirereidcollins.atlassian.net).