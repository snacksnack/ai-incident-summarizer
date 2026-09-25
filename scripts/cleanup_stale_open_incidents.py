#!/usr/bin/env python3
"""One-time cleanup for RC1-466: retire the stale "open" incidents.

The dashboard's open view had become 34 rows of debris — 30 seed incidents
(`seed-*`, from scripts/seed_dynamo.py) that were seeded as open and can never
receive a recovery, plus a handful of test-era incidents whose alerts recovered
before the recovery-close path existed. Seeds are deleted (their resolved and
acknowledged siblings stay as demo history); real-but-stale opens are
administratively resolved, mirroring the ingest close path's fields
(status/resolved_at/last_updated_at) with a resolution_note saying no recovery
alert was observed.

Usage:
    DYNAMODB_TABLE=<incident-table-name> \
    AWS_DEFAULT_REGION=us-east-1 \
    python scripts/cleanup_stale_open_incidents.py [--apply] [--resolve-before ISO]

Dry run by default: prints one line per open incident with the action it would
take. --apply performs the writes. Open incidents newer than --resolve-before
(default: 7 days ago) are kept — a genuinely open incident must not be closed
by a cleanup script.
"""

import argparse
import os
import sys
from datetime import datetime, timedelta, timezone

import boto3
from boto3.dynamodb.conditions import Attr, Key

TABLE_NAME = os.environ.get("DYNAMODB_TABLE")
REGION = os.environ.get("AWS_DEFAULT_REGION", "us-east-1")

RESOLUTION_NOTE = "Administratively closed as stale (RC1-466); no recovery alert was observed."


def open_incidents(table) -> list:
    items = []
    kwargs = {
        "IndexName": "status-created-index",
        "KeyConditionExpression": Key("status").eq("open"),
    }
    while True:
        resp = table.query(**kwargs)
        items.extend(resp["Items"])
        if "LastEvaluatedKey" not in resp:
            return items
        kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="perform the writes (default: dry run)")
    parser.add_argument(
        "--resolve-before",
        default=(datetime.now(timezone.utc) - timedelta(days=7)).isoformat(),
        help="only resolve non-seed opens created before this ISO timestamp (default: 7 days ago)",
    )
    args = parser.parse_args()

    if not TABLE_NAME:
        print("Error: DYNAMODB_TABLE environment variable is not set.")
        sys.exit(1)

    table = boto3.resource("dynamodb", region_name=REGION).Table(TABLE_NAME)
    now_iso = datetime.now(timezone.utc).isoformat()
    counts = {"DELETE": 0, "RESOLVE": 0, "KEEP": 0}

    for item in sorted(open_incidents(table), key=lambda i: i["created_at"]):
        incident_id = item["incident_id"]
        if incident_id.startswith("seed-"):
            action = "DELETE"
            if args.apply:
                # The status condition keeps every write in this script a no-op
                # against anything the pipeline touched since the query ran.
                table.delete_item(
                    Key={"incident_id": incident_id},
                    ConditionExpression=Attr("status").eq("open"),
                )
        elif item["created_at"] < args.resolve_before:
            action = "RESOLVE"
            if args.apply:
                table.update_item(
                    Key={"incident_id": incident_id},
                    UpdateExpression=(
                        "SET #st = :resolved, resolved_at = :ts, "
                        "last_updated_at = :ts, resolution_note = :note"
                    ),
                    ConditionExpression=Attr("status").eq("open"),
                    ExpressionAttributeNames={"#st": "status"},
                    ExpressionAttributeValues={
                        ":resolved": "resolved",
                        ":ts": now_iso,
                        ":note": RESOLUTION_NOTE,
                    },
                )
        else:
            action = "KEEP"
        counts[action] += 1
        print(f"{action:8} {item['created_at']}  {item['affected_service']}  {incident_id}")

    mode = "applied" if args.apply else "dry run — nothing written"
    print(f"\n{counts['DELETE']} deleted, {counts['RESOLVE']} resolved, {counts['KEEP']} kept ({mode})")


if __name__ == "__main__":
    main()
