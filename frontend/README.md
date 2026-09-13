# Incident history UI

Next.js app on Vercel that lists the incidents the pipeline wrote to DynamoDB
and shows one incident with its summary, Slack thread and Jira links. The API
routes under `app/api/` read DynamoDB directly; nothing else in the stack is
called.

## Running locally

```bash
vercel link                        # once
vercel env pull .env.local --yes   # table names, links, and a VERCEL_OIDC_TOKEN (~12 h)
npm run dev
```

AWS access is keyless: `lib/dynamodb.ts` exchanges the Vercel OIDC token for
credentials on the stack's `DashboardReadRole` (see the root README, "Dashboard
access to DynamoDB"). When the API routes start failing with a credentials
error after a long session, the token has expired; pull again. There is no
`AWS_ACCESS_KEY_ID` to set.

## Environment variables

| Variable | Purpose |
|---|---|
| `AWS_ROLE_ARN` | `DashboardReadRoleArn` output of the SAM stack |
| `AWS_REGION` | `us-east-1` |
| `INCIDENT_TABLE_NAME`, `SERVICE_REGISTRY_TABLE_NAME` | Physical table names from the stack outputs |
| `NEXT_PUBLIC_SLACK_CHANNEL_ID`, `NEXT_PUBLIC_JIRA_BASE_URL` | Deep links on the incident page |
| `VERCEL_OIDC_TOKEN` | Injected by Vercel; locally provisioned by `vercel env pull` |

## Deploying

`deploy.yml` at the repo root runs `vercel --prod` after the SAM deploy on every
push to `main`. Preview deployments cannot assume the read role by design.

This is Next.js 16; read `node_modules/next/dist/docs/` before changing app
code, the conventions differ from older versions.
