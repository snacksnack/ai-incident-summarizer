#!/usr/bin/env python3
"""Seed DynamoDB with realistic SaaS incident data for demo and portfolio use.

Usage:
    DYNAMODB_TABLE=<table-name> \
    AWS_DEFAULT_REGION=us-east-1 \
    SLACK_BOT_TOKEN=xoxb-... \
    SLACK_CHANNEL_ID=C0B4L4L5H4J \
    JIRA_API_TOKEN=... \
    JIRA_BASE_URL=https://your-org.atlassian.net \
    JIRA_PROJECT_KEY=INC \
    JIRA_USER_EMAIL=you@example.com \
    python scripts/seed_dynamo.py

Slack and Jira credentials are optional. When provided, resolved and
acknowledged incidents get real Slack thread links and Jira ticket links.
When omitted, those fields are left unpopulated.
"""

import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone

import boto3
import requests
from requests.auth import HTTPBasicAuth

TABLE_NAME = os.environ.get("DYNAMODB_TABLE")
REGION = os.environ.get("AWS_DEFAULT_REGION", "us-east-1")

if not TABLE_NAME:
    print("Error: DYNAMODB_TABLE environment variable is not set.")
    sys.exit(1)

dynamodb = boto3.resource("dynamodb", region_name=REGION)
table = dynamodb.Table(TABLE_NAME)

NOW = datetime.now(timezone.utc)
TTL_DAYS = 30

# 9 incidents per service: 3 open, 4 resolved, 2 acknowledged
STATUSES = [
    "open", "open", "open",
    "resolved", "resolved", "resolved", "resolved",
    "acknowledged", "acknowledged",
]

# Organic spread across last 7 days
DAY_OFFSETS = [0.2, 0.8, 1.3, 2.0, 2.7, 3.4, 4.2, 5.1, 6.3]
HOUR_OFFSETS = [3,   11,  19,  7,   22,  14,  2,   18,  9  ]

SEVERITY_EMOJI = {"critical": "🔴", "high": "🟠", "medium": "🟡", "low": "🟢"}
PRIORITY_MAP = {"critical": "Highest", "high": "High", "medium": "Medium", "low": "Low"}


def _iso_z(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _slack_message(incident: dict) -> str:
    severity = incident["severity"].lower()
    emoji = SEVERITY_EMOJI.get(severity, "")
    badge = f"{emoji} *{severity.upper()}*" if emoji else f"*{severity.upper()}*"
    llm = json.loads(incident["llm_summary"])
    return (
        f"{badge} | {incident['affected_service']} | {incident['created_at']}\n\n"
        f"*Summary:* {llm['summary']}\n"
        f"*Likely cause:* {llm['likely_cause']}\n"
        f"*Next step:* {llm['next_step']}"
    )


def _post_to_slack(token: str, channel: str, incident: dict) -> str:
    from slack_sdk import WebClient
    client = WebClient(token=token)
    result = client.chat_postMessage(channel=channel, text=_slack_message(incident))
    return result["ts"]


def _jira_description(incident: dict) -> dict:
    llm = json.loads(incident.get("llm_summary", "{}"))
    paragraphs = []
    for label, key in [("Summary", "summary"), ("Likely cause", "likely_cause"), ("Next step", "next_step")]:
        paragraphs.append({
            "type": "paragraph",
            "content": [{"type": "text", "text": f"{label}: {llm.get(key, '')}"}],
        })
    alerts = incident.get("source_alerts", [])
    if alerts:
        paragraphs.append({"type": "paragraph", "content": [{"type": "text", "text": "Alerts:"}]})
        paragraphs.append({
            "type": "bulletList",
            "content": [
                {"type": "listItem", "content": [{"type": "paragraph", "content": [
                    {"type": "text", "text": f"{a['alert_name']} ({a['source']})"}
                ]}]}
                for a in alerts
            ],
        })
    slack_thread_id = incident.get("slack_thread_id")
    if slack_thread_id:
        channel = os.environ.get("SLACK_CHANNEL_ID", "")
        url = f"https://slack.com/app_redirect?channel={channel}&message_ts={slack_thread_id}"
        paragraphs.append({
            "type": "paragraph",
            "content": [
                {"type": "text", "text": "Slack thread: "},
                {"type": "text", "text": url, "marks": [{"type": "link", "attrs": {"href": url}}]},
            ],
        })
    return {"version": 1, "type": "doc", "content": paragraphs}


def _create_jira_ticket(incident: dict) -> str:
    base_url = os.environ["JIRA_BASE_URL"].rstrip("/")
    severity = incident["severity"].lower()
    payload = {
        "fields": {
            "project": {"key": os.environ["JIRA_PROJECT_KEY"]},
            "summary": f"[{severity.upper()}] {incident['affected_service']} — {incident['incident_id']}",
            "description": _jira_description(incident),
            "issuetype": {"name": "Bug"},
            "priority": {"name": PRIORITY_MAP.get(severity, "Medium")},
        }
    }
    response = requests.post(
        f"{base_url}/rest/api/3/issue",
        json=payload,
        auth=HTTPBasicAuth(os.environ["JIRA_USER_EMAIL"], os.environ["JIRA_API_TOKEN"]),
        headers={"Accept": "application/json"},
        timeout=15,
    )
    response.raise_for_status()
    return response.json()["key"]


def _make_incident(idx: int, service_key: str, severity: str, scenario: dict) -> dict:
    status = STATUSES[idx]
    created_at = NOW - timedelta(days=DAY_OFFSETS[idx], hours=HOUR_OFFSETS[idx])
    ttl = int(created_at.timestamp()) + TTL_DAYS * 24 * 60 * 60
    incident_id = f"seed-{service_key}-{idx + 1:03d}"

    alert_time = created_at
    alert_status = "open" if status == "open" else "resolved"
    source_alerts = []
    for i, alert in enumerate(scenario["alerts"]):
        source_alerts.append({
            "alert_id": f"{incident_id}-a{i + 1}",
            "source": alert["source"],
            "alert_name": alert["name"],
            "severity": alert.get("severity", severity),
            "status": alert_status,
            "received_at": _iso_z(alert_time),
        })
        alert_time += timedelta(minutes=alert.get("delay_minutes", 3))

    return {
        "incident_id": incident_id,
        "affected_service": service_key,
        "severity": severity,
        "status": status,
        "source_alerts": source_alerts,
        "llm_summary": json.dumps({
            "summary": scenario["summary"],
            "likely_cause": scenario["likely_cause"],
            "next_step": scenario["next_step"],
        }),
        "created_at": _iso_z(created_at),
        "ttl": ttl,
    }


SERVICE_CONFIGS = [
    {
        "key": "payments-service",
        "severity": "critical",
        "scenarios": [
            {
                "summary": "Payments service is experiencing critical error rates with 45% of transactions failing. Database connection pool is fully exhausted, causing cascading timeouts across checkout, subscription renewal, and refund flows.",
                "likely_cause": "Connection leak introduced in v2.14.1 combined with a 3x spike in concurrent checkouts caused pool saturation within 8 minutes of traffic surge.",
                "next_step": "Immediately scale the RDS connection pool limit and restart the payments Lambda fleet to clear leaked connections. Roll back to v2.14.0 if error rate does not recover within 5 minutes.",
                "alerts": [
                    {"source": "cloudwatch", "name": "payments-error-rate-critical", "delay_minutes": 0},
                    {"source": "datadog",    "name": "db-connection-pool-exhausted", "delay_minutes": 2},
                    {"source": "pagerduty",  "name": "checkout-latency-p99-spike",   "delay_minutes": 5},
                    {"source": "cloudwatch", "name": "lambda-concurrent-throttle",   "delay_minutes": 7},
                ],
            },
            {
                "summary": "Payment gateway webhook processor is timing out on Stripe callbacks. Successful payment confirmations are delayed 90+ seconds, causing duplicate charge attempts from clients retrying.",
                "likely_cause": "Stripe increased their webhook retry interval, overwhelming the underpowered SQS consumer fleet that was not scaled after last month's merchant onboarding surge.",
                "next_step": "Scale the SQS consumer fleet from 2 to 8 instances. Add idempotency checks on Stripe event IDs to prevent duplicate processing during backlog drain.",
                "alerts": [
                    {"source": "datadog",    "name": "stripe-webhook-processing-delay", "delay_minutes": 0},
                    {"source": "cloudwatch", "name": "sqs-queue-depth-high",             "delay_minutes": 3},
                    {"source": "datadog",    "name": "duplicate-charge-rate-elevated",   "delay_minutes": 8},
                ],
            },
            {
                "summary": "Currency conversion service is returning stale exchange rates after the rate cache TTL expired without successful refresh. International transactions are processed at rates up to 4 hours out of date.",
                "likely_cause": "External currency API rate limit was hit during the scheduled cache refresh job. The fallback mechanism serves expired rates instead of surfacing an error.",
                "next_step": "Manually trigger a cache refresh using the backup currency API endpoint. Update fallback logic to reject transactions when rate staleness exceeds 30 minutes.",
                "alerts": [
                    {"source": "cloudwatch", "name": "currency-cache-refresh-failure", "delay_minutes": 0},
                    {"source": "datadog",    "name": "stale-rate-serving-detected",    "delay_minutes": 5},
                ],
            },
            {
                "summary": "Refund processing pipeline is backed up with 2,400 pending refunds. The batch processor is timing out due to an N+1 query pattern hitting the database for each refund line item individually.",
                "likely_cause": "A recent schema migration removed the eager-loading configuration. Each refund now triggers 12 individual database calls instead of a single join query.",
                "next_step": "Deploy hotfix adding eager loading to the refund processor. Manually trigger batch processing for the backlog once the fix is confirmed.",
                "alerts": [
                    {"source": "datadog",    "name": "refund-queue-depth-critical", "delay_minutes": 0},
                    {"source": "cloudwatch", "name": "batch-processor-timeout",     "delay_minutes": 4},
                    {"source": "datadog",    "name": "db-query-latency-spike",      "delay_minutes": 6},
                ],
            },
        ],
    },
    {
        "key": "auth-service",
        "severity": "high",
        "scenarios": [
            {
                "summary": "Auth service is rejecting all login attempts with 500 errors after the latest deployment. JWT signing key environment variable is missing from production configuration, causing token generation to fail for all users.",
                "likely_cause": "JWT_SIGNING_KEY was renamed to JWT_SECRET in the new config schema but the production Secrets Manager reference was not updated during the v3.2.0 deployment.",
                "next_step": "Immediately roll back to v3.1.9. Update the Secrets Manager reference to use the new key name before re-deploying v3.2.0.",
                "alerts": [
                    {"source": "cloudwatch", "name": "auth-500-error-spike",       "delay_minutes": 0},
                    {"source": "datadog",    "name": "login-failure-rate-100pct",  "delay_minutes": 2},
                    {"source": "pagerduty",  "name": "active-users-drop-critical", "delay_minutes": 4},
                ],
            },
            {
                "summary": "MFA verification is failing for TOTP-based authenticators after a time drift correction. The 30-second TOTP window is not accounting for clock skew properly, rejecting valid codes.",
                "likely_cause": "NTP sync interval was changed from 60s to 300s in the latest AMI update. Combined with a strict TOTP validation window, valid codes are rejected at the boundary.",
                "next_step": "Widen the TOTP acceptance window to ±90 seconds to account for client clock drift. Re-enable NTP sync at 60s intervals in the next AMI build.",
                "alerts": [
                    {"source": "datadog",    "name": "mfa-failure-rate-elevated", "delay_minutes": 0},
                    {"source": "cloudwatch", "name": "totp-validation-errors",    "delay_minutes": 3},
                ],
            },
            {
                "summary": "OAuth token refresh is failing for third-party integrations after the authorization server certificate was rotated. Partner API clients using cached certificates receive SSL handshake failures.",
                "likely_cause": "Certificate rotation was not propagated to CDN edge nodes within the expected 15-minute window. Partner integrations with certificate pinning fail while edges serve the old cert.",
                "next_step": "Force CDN cache invalidation for the auth endpoint certificates. Notify affected partners to disable certificate pinning or update their certificate stores.",
                "alerts": [
                    {"source": "cloudwatch", "name": "ssl-handshake-failure-rate",  "delay_minutes": 0},
                    {"source": "datadog",    "name": "oauth-token-refresh-failures", "delay_minutes": 5},
                    {"source": "datadog",    "name": "partner-api-error-rate",       "delay_minutes": 8},
                ],
            },
            {
                "summary": "Session invalidation is not propagating across regions after a Redis cluster failover. Users who log out in us-east-1 remain authenticated in eu-west-1 for up to 15 minutes.",
                "likely_cause": "Redis replication lag increased to 12 minutes during the failover event. Session invalidation events are queued but not flushed to replica nodes.",
                "next_step": "Force Redis replication sync across all clusters. Implement secondary session validation against DynamoDB for security-critical operations until replication stabilises.",
                "alerts": [
                    {"source": "datadog",    "name": "redis-replication-lag-high",   "delay_minutes": 0},
                    {"source": "cloudwatch", "name": "session-invalidation-delay",   "delay_minutes": 6},
                ],
            },
        ],
    },
    {
        "key": "api-gateway",
        "severity": "high",
        "scenarios": [
            {
                "summary": "API Gateway is experiencing elevated 504 timeout errors affecting 18% of requests. The upstream recommendation engine is responding in 28+ seconds due to a cold cache after a simultaneous AZ restart.",
                "likely_cause": "Scheduled maintenance restarted all recommendation service instances simultaneously across availability zones, causing a cold cache warm-up storm that exceeds the 29-second Lambda timeout.",
                "next_step": "Implement a rolling restart strategy with a 5-minute stagger between AZs. Add a cache pre-warm step to the deployment pipeline for the recommendation service.",
                "alerts": [
                    {"source": "cloudwatch", "name": "api-gateway-504-rate-elevated",      "delay_minutes": 0},
                    {"source": "datadog",    "name": "recommendation-engine-latency-p99",  "delay_minutes": 3},
                    {"source": "cloudwatch", "name": "lambda-timeout-errors",               "delay_minutes": 5},
                ],
            },
            {
                "summary": "API rate limiting is incorrectly throttling enterprise customers at the anonymous tier limit. A configuration change 2 hours ago applied the wrong tier mapping to API keys with custom rate limits.",
                "likely_cause": "A migration script for the new rate limit config format did not correctly parse the custom tier field for enterprise API keys, mapping them to the default anonymous limit.",
                "next_step": "Revert the rate limit configuration to the previous snapshot. Run the migration script in dry-run mode to identify all affected API keys before re-applying.",
                "alerts": [
                    {"source": "datadog",    "name": "enterprise-rate-limit-violations", "delay_minutes": 0},
                    {"source": "cloudwatch", "name": "429-error-rate-spike",              "delay_minutes": 2},
                ],
            },
            {
                "summary": "GraphQL API is returning partial data for nested relationship queries after a schema federation update. Subgraph stitching is failing silently for user profile to order history relationships.",
                "likely_cause": "The order-service subgraph changed the userId field type from String to ID without a migration period. The gateway schema was not re-stitched, causing silent null returns.",
                "next_step": "Re-stitch the GraphQL gateway schema to pick up the order-service type changes. Add schema compatibility checks to the order-service CI pipeline.",
                "alerts": [
                    {"source": "datadog",    "name": "graphql-partial-response-rate",  "delay_minutes": 0},
                    {"source": "cloudwatch", "name": "subgraph-resolver-errors",        "delay_minutes": 4},
                    {"source": "datadog",    "name": "order-history-null-rate",         "delay_minutes": 7},
                ],
            },
        ],
    },
    {
        "key": "notification-service",
        "severity": "medium",
        "scenarios": [
            {
                "summary": "Email delivery is delayed by 45+ minutes for all transactional notifications. A batch marketing campaign is consuming 94% of the available SES sending quota, starving the transactional email queue.",
                "likely_cause": "The marketing batch job was scheduled without a sending rate limit. It acquired the bulk of the SES quota within the first hour, leaving insufficient throughput for time-sensitive emails.",
                "next_step": "Pause the marketing batch job immediately. Implement quota partitioning that reserves 40% of SES capacity exclusively for transactional notifications.",
                "alerts": [
                    {"source": "cloudwatch", "name": "ses-sending-rate-quota-high",      "delay_minutes": 0},
                    {"source": "datadog",    "name": "transactional-email-queue-depth",  "delay_minutes": 8},
                    {"source": "cloudwatch", "name": "email-delivery-delay-p95",          "delay_minutes": 15},
                ],
            },
            {
                "summary": "Push notification delivery is failing for all iOS devices after an APNs certificate renewal. The notification service is still using the expired certificate, causing silent delivery failures.",
                "likely_cause": "APNs certificate renewal was completed in staging but the production certificate store was not updated. The 30-day expiry window lapsed 6 hours ago.",
                "next_step": "Update the APNs certificate in production Secrets Manager and restart the notification service. Implement certificate expiry monitoring with a 14-day warning threshold.",
                "alerts": [
                    {"source": "datadog",    "name": "ios-push-delivery-failure-rate", "delay_minutes": 0},
                    {"source": "cloudwatch", "name": "apns-connection-errors",          "delay_minutes": 3},
                ],
            },
            {
                "summary": "SMS OTP delivery is experiencing elevated failure rates in the APAC region after a carrier routing change. Verification codes are not reaching users in Singapore and Australia.",
                "likely_cause": "The SMS aggregator updated their carrier routing table and a failover to a regional carrier was not configured. APAC traffic is being routed through a US carrier with high international failure rates.",
                "next_step": "Switch APAC region SMS routing to the backup regional aggregator. Update the carrier routing configuration with APAC-specific primary and fallback carriers.",
                "alerts": [
                    {"source": "datadog",    "name": "sms-delivery-failure-apac",       "delay_minutes": 0},
                    {"source": "cloudwatch", "name": "otp-verification-timeout-rate",   "delay_minutes": 5},
                ],
            },
        ],
    },
    {
        "key": "search-service",
        "severity": "high",
        "scenarios": [
            {
                "summary": "Full-text search is returning stale results after the nightly Elasticsearch index rebuild job ran during peak hours. The rebuild triggered a massive GC pause, degrading search latency to 8+ seconds.",
                "likely_cause": "The cron schedule was incorrectly set to 10:00 UTC instead of 02:00 UTC after a daylight saving time configuration update, triggering during peak US East Coast business hours.",
                "next_step": "Kill the running index rebuild job. Reschedule to 02:00 UTC and add a peak-hours guard that prevents the job if p95 search latency exceeds 500ms.",
                "alerts": [
                    {"source": "cloudwatch", "name": "elasticsearch-heap-pressure-critical", "delay_minutes": 0},
                    {"source": "datadog",    "name": "search-latency-p95-spike",              "delay_minutes": 4},
                    {"source": "cloudwatch", "name": "index-rebuild-job-running",              "delay_minutes": 0},
                ],
            },
            {
                "summary": "Autocomplete search results are returning irrelevant suggestions after a stemming algorithm update. The new porter2 stemmer is over-aggressively matching unrelated terms for short 2-3 character queries.",
                "likely_cause": "The stemmer configuration was changed without updating the minimum query length threshold. Queries shorter than 4 characters should bypass stemming but the threshold was not applied.",
                "next_step": "Apply a minimum query length of 4 characters for the stemming pipeline. Roll back the stemmer config for queries under 4 characters to the previous behaviour.",
                "alerts": [
                    {"source": "datadog",    "name": "autocomplete-relevance-score-drop", "delay_minutes": 0},
                    {"source": "cloudwatch", "name": "search-click-through-rate-drop",     "delay_minutes": 10},
                ],
            },
            {
                "summary": "Search indexing is failing for new product listings after a schema migration. Products created after 14:00 UTC are not appearing in search results due to silent validation errors.",
                "likely_cause": "The product schema migration added a required non-nullable field that the search indexer does not populate. The indexer silently drops records that fail validation.",
                "next_step": "Update the search indexer to handle the new required field with a default value. Re-index all products created after 14:00 UTC from the source database.",
                "alerts": [
                    {"source": "datadog",    "name": "search-index-failure-rate",       "delay_minutes": 0},
                    {"source": "cloudwatch", "name": "new-product-search-visibility",   "delay_minutes": 20},
                    {"source": "datadog",    "name": "indexer-validation-errors",        "delay_minutes": 3},
                ],
            },
        ],
    },
    {
        "key": "billing-service",
        "severity": "critical",
        "scenarios": [
            {
                "summary": "Subscription renewal processing is failing for 34% of monthly renewals. The third-party billing API is experiencing 45+ second latency, causing Lambda timeouts and failed charge attempts.",
                "likely_cause": "Billing provider (Recurly) is experiencing a partial outage on their US-East charging infrastructure. Retry attempts are compounding load and triggering rate limiting on the provider side.",
                "next_step": "Switch to the EU Recurly endpoint as a failover. Implement exponential backoff and queue failed renewals for a 2-hour retry window to avoid provider rate limits.",
                "alerts": [
                    {"source": "cloudwatch", "name": "billing-api-timeout-rate-critical", "delay_minutes": 0},
                    {"source": "datadog",    "name": "renewal-failure-rate-34pct",         "delay_minutes": 5},
                    {"source": "pagerduty",  "name": "revenue-impact-critical",             "delay_minutes": 8},
                    {"source": "cloudwatch", "name": "lambda-concurrent-throttle",          "delay_minutes": 6},
                ],
            },
            {
                "summary": "Invoice generation is producing incorrect totals for enterprise customers with volume discounts. A tax calculation library update changed rounding behaviour, causing $0.01-$0.03 discrepancies per line item.",
                "likely_cause": "Tax library v4.2.0 changed from ROUND_HALF_UP to ROUND_HALF_EVEN by default. Enterprise invoices with high line-item counts amplify the rounding difference to noticeable amounts.",
                "next_step": "Pin the tax library to v4.1.9 and redeploy. Re-generate all invoices from the last 48 hours and issue corrected versions with credit notes for any overcharges.",
                "alerts": [
                    {"source": "datadog",    "name": "invoice-amount-discrepancy-detected", "delay_minutes": 0},
                    {"source": "cloudwatch", "name": "billing-validation-errors",             "delay_minutes": 10},
                ],
            },
            {
                "summary": "Usage-based billing metering is under-reporting API calls for pro-tier customers. The metering Lambda is dropping events when the Kinesis shard iterator expires during high-traffic windows.",
                "likely_cause": "Kinesis consumer was not updated to handle shard iterator expiration after stream retention was increased to 7 days. Iterator expiration during idle periods causes silent event drops on resumption.",
                "next_step": "Update the Kinesis consumer to handle expired iterators by fetching TRIM_HORIZON. Audit the last 48 hours of metering data against raw API logs to identify under-billed customers.",
                "alerts": [
                    {"source": "cloudwatch", "name": "kinesis-iterator-expired-errors", "delay_minutes": 0},
                    {"source": "datadog",    "name": "api-call-metering-gap-detected",  "delay_minutes": 15},
                    {"source": "cloudwatch", "name": "billing-event-drop-rate",          "delay_minutes": 5},
                ],
            },
        ],
    },
    {
        "key": "user-service",
        "severity": "medium",
        "scenarios": [
            {
                "summary": "User profile queries are timing out for accounts with more than 500 connections. A missing index on last_login_at is causing full table scans on the 40M-row follower relationships table.",
                "likely_cause": "The last_login_at column was added in migration v0042 without a corresponding index. It is used as a sort key in the connections feed query, now performing a full sequential scan.",
                "next_step": "Create a non-blocking index using CREATE INDEX CONCURRENTLY. Add query timeout protection to the connections feed endpoint to prevent cascading timeouts while the index builds.",
                "alerts": [
                    {"source": "datadog",    "name": "user-profile-query-timeout-rate", "delay_minutes": 0},
                    {"source": "cloudwatch", "name": "rds-cpu-utilization-high",         "delay_minutes": 3},
                    {"source": "datadog",    "name": "db-slow-query-count-spike",        "delay_minutes": 2},
                ],
            },
            {
                "summary": "User avatar uploads are failing for files larger than 2MB after a CDN configuration change. Presigned S3 URL expiry was reduced from 15 minutes to 30 seconds, breaking multi-part uploads.",
                "likely_cause": "CDN security hardening reduced the presigned URL TTL to 30 seconds. Multi-part uploads for large files take 45-90 seconds, causing URL expiry mid-upload.",
                "next_step": "Increase presigned URL TTL to 5 minutes for multi-part upload URLs. Add file size detection in the frontend to split large files into smaller parts before requesting URLs.",
                "alerts": [
                    {"source": "cloudwatch", "name": "s3-presigned-url-expiry-errors", "delay_minutes": 0},
                    {"source": "datadog",    "name": "avatar-upload-failure-rate",      "delay_minutes": 4},
                ],
            },
            {
                "summary": "Account deletion requests are not propagating to downstream services. Deleted users are still receiving emails and their data remains in the search index 24 hours after deletion.",
                "likely_cause": "A queue configuration change removed notification, search, and analytics subscriptions from the account deletion topic filter.",
                "next_step": "Restore the topic subscriptions for all three services. Manually trigger deletion events for all accounts deleted in the last 24 hours.",
                "alerts": [
                    {"source": "datadog",    "name": "account-deletion-propagation-failure", "delay_minutes": 0},
                    {"source": "cloudwatch", "name": "deleted-user-email-sent",               "delay_minutes": 30},
                ],
            },
        ],
    },
    {
        "key": "cdn",
        "severity": "low",
        "scenarios": [
            {
                "summary": "CDN cache hit rate dropped from 94% to 31% after a new product launch triggered a mass cache invalidation. Edge nodes are pulling from origin, causing elevated load and increased TTFB across all regions.",
                "likely_cause": "The product launch simultaneously invalidated all product cache keys across 47 edge locations. The cache warming strategy did not account for the volume of unique URLs in the new catalog.",
                "next_step": "Implement staggered cache warming by pre-populating the top 1,000 product URLs from origin to edge before the next launch. Rate-limit cache invalidation API to prevent simultaneous full-cache purges.",
                "alerts": [
                    {"source": "cloudwatch", "name": "cdn-cache-hit-rate-drop",   "delay_minutes": 0},
                    {"source": "datadog",    "name": "origin-request-rate-spike", "delay_minutes": 5},
                    {"source": "cloudwatch", "name": "ttfb-p95-elevated",          "delay_minutes": 8},
                ],
            },
            {
                "summary": "Static asset serving is returning stale JavaScript bundles to users in APAC. Edge nodes in Singapore and Tokyo are not invalidating after the latest frontend deployment.",
                "likely_cause": "The deployment pipeline CDN invalidation step only targets us-east-1 and eu-west-1 edge groups. APAC edge groups were excluded after a region expansion 3 months ago.",
                "next_step": "Update the deployment pipeline to include all edge groups in invalidation requests. Manually invalidate the APAC edge caches for the current release.",
                "alerts": [
                    {"source": "datadog",    "name": "apac-cache-version-mismatch",     "delay_minutes": 0},
                    {"source": "cloudwatch", "name": "js-bundle-version-inconsistency", "delay_minutes": 15},
                ],
            },
            {
                "summary": "Image optimization pipeline is serving oversized thumbnails after a sharp library upgrade. Product images that should be 80KB are being served at 450KB, increasing page load times by 2.1 seconds.",
                "likely_cause": "Sharp v0.33 changed the default WebP compression quality from 80 to 95. The image optimization configuration did not override the default, affecting all newly processed images.",
                "next_step": "Update the image optimization configuration to explicitly set WebP quality to 80. Re-process all product images added in the last 72 hours with the corrected settings.",
                "alerts": [
                    {"source": "datadog",    "name": "image-size-regression-detected", "delay_minutes": 0},
                    {"source": "cloudwatch", "name": "cdn-bandwidth-usage-spike",       "delay_minutes": 10},
                ],
            },
        ],
    },
    {
        "key": "data-pipeline",
        "severity": "high",
        "scenarios": [
            {
                "summary": "The nightly ETL pipeline failed at the transformation stage after the upstream events schema was changed without notice. 14 hours of event data is queued but blocked from processing.",
                "likely_cause": "The upstream events service added a required field event_version without a migration period. The pipeline schema validator rejects all records containing the new field.",
                "next_step": "Update the pipeline schema to accept event_version as optional. Process the backlog from oldest records first. Establish a schema change notification process with the events team.",
                "alerts": [
                    {"source": "cloudwatch", "name": "etl-transformation-failure-rate",        "delay_minutes": 0},
                    {"source": "datadog",    "name": "pipeline-queue-depth-critical",           "delay_minutes": 5},
                    {"source": "cloudwatch", "name": "downstream-data-freshness-sla-breach",    "delay_minutes": 60},
                ],
            },
            {
                "summary": "Real-time analytics aggregation is producing incorrect daily active user counts. A timezone handling bug is double-counting users who are active across the UTC midnight boundary.",
                "likely_cause": "A recent refactor changed session timestamp normalisation to use local timezone instead of UTC. Sessions spanning midnight UTC are counted in both the previous and current day's DAU.",
                "next_step": "Revert timezone normalisation to UTC. Re-aggregate DAU metrics for the last 30 days from raw session data to correct historical figures.",
                "alerts": [
                    {"source": "datadog",    "name": "dau-anomaly-detected",              "delay_minutes": 0},
                    {"source": "cloudwatch", "name": "metric-calculation-discrepancy",    "delay_minutes": 20},
                ],
            },
            {
                "summary": "The data warehouse export job is failing with OOM errors after the customer dataset grew past 50M rows. The Spark job is loading the full dataset into memory instead of processing in partitions.",
                "likely_cause": "Partition pruning configuration was removed during a Spark upgrade from 3.2 to 3.5. Without pruning, the full 50M-row table loads into executor memory.",
                "next_step": "Re-add date-based partition configuration to the Spark job. Increase executor memory to 16GB as a short-term mitigation while partition pruning is re-implemented.",
                "alerts": [
                    {"source": "cloudwatch", "name": "spark-executor-oom-errors",      "delay_minutes": 0},
                    {"source": "datadog",    "name": "warehouse-export-job-failure",   "delay_minutes": 8},
                    {"source": "cloudwatch", "name": "emr-cluster-task-failures",      "delay_minutes": 3},
                ],
            },
        ],
    },
    {
        "key": "websocket-service",
        "severity": "medium",
        "scenarios": [
            {
                "summary": "WebSocket connections are being dropped every 45 minutes for active users. A memory leak in v1.8.2 causes the connection manager to accumulate stale socket references until the process restarts.",
                "likely_cause": "The v1.8.2 event listener cleanup was incorrectly refactored. Disconnected clients are not removed from the internal connection registry, causing memory to grow until the 512MB limit triggers a restart.",
                "next_step": "Deploy hotfix v1.8.3 which adds explicit cleanup of disconnected client references. Monitor memory usage for 30 minutes post-deployment to confirm the leak is resolved.",
                "alerts": [
                    {"source": "datadog",    "name": "websocket-connection-drop-rate", "delay_minutes": 0},
                    {"source": "cloudwatch", "name": "node-process-memory-high",        "delay_minutes": 10},
                    {"source": "cloudwatch", "name": "ecs-task-restart-count",           "delay_minutes": 45},
                ],
            },
            {
                "summary": "Real-time collaborative editing is experiencing message ordering issues for users across regions. Messages from EU clients arrive out of order at US clients due to inconsistent WebSocket routing.",
                "likely_cause": "Load balancer sticky sessions were disabled during an upgrade. Clients in the same editing session now route to different WebSocket servers that do not share connection state.",
                "next_step": "Re-enable sticky sessions based on collaboration session ID. Add a sequence number to the WebSocket protocol to allow clients to detect and reorder out-of-order messages.",
                "alerts": [
                    {"source": "datadog",    "name": "message-ordering-violation-rate",  "delay_minutes": 0},
                    {"source": "cloudwatch", "name": "cross-server-session-conflicts",    "delay_minutes": 5},
                ],
            },
            {
                "summary": "WebSocket heartbeat timeouts are incorrectly disconnecting users on high-latency mobile connections. The 30-second heartbeat timeout is too aggressive for 4G connections with 300ms+ RTT.",
                "likely_cause": "Heartbeat timeout was reduced from 60s to 30s in a performance audit without accounting for mobile clients. Users on cellular with elevated latency are disconnected unnecessarily.",
                "next_step": "Increase heartbeat timeout to 60s for connections with RTT above 150ms. Implement adaptive timeout based on connection quality negotiated during the WebSocket handshake.",
                "alerts": [
                    {"source": "datadog",    "name": "mobile-websocket-disconnect-rate", "delay_minutes": 0},
                    {"source": "cloudwatch", "name": "heartbeat-timeout-errors",          "delay_minutes": 3},
                ],
            },
        ],
    },
]


def seed() -> None:
    slack_token = os.environ.get("SLACK_BOT_TOKEN")
    slack_channel = os.environ.get("SLACK_CHANNEL_ID")
    jira_token = os.environ.get("JIRA_API_TOKEN")
    use_slack = bool(slack_token and slack_channel)
    use_jira = bool(jira_token and os.environ.get("JIRA_BASE_URL") and
                    os.environ.get("JIRA_PROJECT_KEY") and os.environ.get("JIRA_USER_EMAIL"))

    if use_slack and use_jira:
        print("Slack and Jira integration enabled — creating real threads and tickets.")
    else:
        missing = []
        if not use_slack:
            missing.append("SLACK_BOT_TOKEN / SLACK_CHANNEL_ID")
        if not use_jira:
            missing.append("JIRA_API_TOKEN / JIRA_BASE_URL / JIRA_PROJECT_KEY / JIRA_USER_EMAIL")
        print(f"Warning: {', '.join(missing)} not set. Slack/Jira links will be omitted.")

    incidents = []
    for config in SERVICE_CONFIGS:
        scenarios = config["scenarios"]
        for idx in range(9):
            scenario = scenarios[idx % len(scenarios)]
            incident = _make_incident(idx, config["key"], config["severity"], scenario)

            if incident["status"] in ("resolved", "acknowledged"):
                if use_slack:
                    try:
                        ts = _post_to_slack(slack_token, slack_channel, incident)
                        incident["slack_thread_id"] = ts
                        print(f"  Slack thread created for {incident['incident_id']}: {ts}")
                        time.sleep(1.2)  # stay within Slack rate limit
                    except Exception as e:
                        print(f"  Warning: Slack post failed for {incident['incident_id']}: {e}")

                if use_jira:
                    try:
                        key = _create_jira_ticket(incident)
                        incident["jira_ticket_id"] = key
                        print(f"  Jira ticket created for {incident['incident_id']}: {key}")
                        time.sleep(0.5)
                    except Exception as e:
                        print(f"  Warning: Jira ticket creation failed for {incident['incident_id']}: {e}")

            incidents.append(incident)

    print(f"\nWriting {len(incidents)} incidents to {TABLE_NAME}...")
    with table.batch_writer() as batch:
        for item in incidents:
            batch.put_item(Item=item)
    print(f"Done. Seeded {len(incidents)} incidents across {len(SERVICE_CONFIGS)} services.")


if __name__ == "__main__":
    seed()
