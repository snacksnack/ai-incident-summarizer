#!/usr/bin/env python3
"""Add Slack threads to incidents that have a Jira ticket but no slack_thread_id.

Usage:
    DYNAMODB_TABLE=<table-name> \
    AWS_DEFAULT_REGION=us-east-1 \
    SLACK_BOT_TOKEN=xoxb-... \
    SLACK_CHANNEL_ID=C0B4L4L5H4J \
    python3 scripts/add_slack_threads.py
"""

import json
import os
import sys
import time

import boto3
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

TABLE_NAME = os.environ.get("DYNAMODB_TABLE")
REGION = os.environ.get("AWS_DEFAULT_REGION", "us-east-1")
SLACK_TOKEN = os.environ.get("SLACK_BOT_TOKEN")
SLACK_CHANNEL = os.environ.get("SLACK_CHANNEL_ID")

for var, name in [
    (TABLE_NAME, "DYNAMODB_TABLE"),
    (SLACK_TOKEN, "SLACK_BOT_TOKEN"),
    (SLACK_CHANNEL, "SLACK_CHANNEL_ID"),
]:
    if not var:
        print(f"Error: {name} environment variable is not set.")
        sys.exit(1)

dynamodb = boto3.resource("dynamodb", region_name=REGION)
table = dynamodb.Table(TABLE_NAME)
slack = WebClient(token=SLACK_TOKEN)

SEVERITY_EMOJI = {"critical": "🔴", "high": "🟠", "medium": "🟡", "low": "🟢"}


def _slack_message(incident: dict) -> str:
    severity = incident.get("severity", "").lower()
    emoji = SEVERITY_EMOJI.get(severity, "")
    badge = f"{emoji} *{severity.upper()}*" if emoji else f"*{severity.upper()}*"
    llm = json.loads(incident.get("llm_summary", "{}"))
    return (
        f"{badge} | {incident['affected_service']} | {incident['created_at']}\n\n"
        f"*Summary:* {llm.get('summary', '')}\n"
        f"*Likely cause:* {llm.get('likely_cause', '')}\n"
        f"*Next step:* {llm.get('next_step', '')}"
    )


def run() -> None:
    print(f"Scanning {TABLE_NAME} for incidents missing slack_thread_id...")

    paginator = dynamodb.meta.client.get_paginator("scan")
    incidents = []
    for page in paginator.paginate(TableName=TABLE_NAME):
        for item in page["Items"]:
            if item.get("jira_ticket_id") and not item.get("slack_thread_id"):
                incidents.append(item)

    print(f"Found {len(incidents)} incident(s) to update.")

    updated = 0
    for incident in incidents:
        incident_id = incident["incident_id"]
        try:
            result = slack.chat_postMessage(
                channel=SLACK_CHANNEL,
                text=_slack_message(incident),
            )
            ts = result["ts"]
            table.update_item(
                Key={"incident_id": incident_id},
                UpdateExpression="SET slack_thread_id = :ts",
                ExpressionAttributeValues={":ts": ts},
            )
            print(f"  {incident_id} → thread {ts}")
            updated += 1
            time.sleep(1.2)
        except SlackApiError as e:
            print(f"  Warning: Slack post failed for {incident_id}: {e.response['error']}")
        except Exception as e:
            print(f"  Warning: Failed for {incident_id}: {e}")

    print(f"\nDone. Updated {updated}/{len(incidents)} incidents with slack_thread_id.")


if __name__ == "__main__":
    run()
