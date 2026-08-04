#!/usr/bin/env python3
"""Populate the service registry from incidents that already exist.

The dedup Lambda registers a service whenever it opens a new incident, so the
registry stays current by itself. This script seeds it for incidents written
before that write path existed.

This is the only time the incident table should be scanned to answer "which
services exist" — every other read goes to the registry, which costs O(services)
instead of O(total table bytes).

Safe to re-run. Each write fills in missing attributes only, so a backfill can
never regress timestamps already set by the live write path.

Usage:
    INCIDENT_TABLE=<incident-table-name> \
    SERVICE_REGISTRY_TABLE=<registry-table-name> \
    AWS_DEFAULT_REGION=us-east-1 \
    python3 scripts/backfill_service_registry.py
"""

import os
import sys

import boto3

INCIDENT_TABLE = os.environ.get("INCIDENT_TABLE")
REGISTRY_TABLE = os.environ.get("SERVICE_REGISTRY_TABLE")
REGION = os.environ.get("AWS_DEFAULT_REGION", "us-east-1")

for value, name in [(INCIDENT_TABLE, "INCIDENT_TABLE"), (REGISTRY_TABLE, "SERVICE_REGISTRY_TABLE")]:
    if not value:
        print(f"Error: {name} environment variable is not set.")
        sys.exit(1)

dynamodb = boto3.resource("dynamodb", region_name=REGION)


def _collect_services() -> dict[str, dict[str, str]]:
    """Scan incidents once, returning first/last created_at per service."""
    table = dynamodb.Table(INCIDENT_TABLE)
    services: dict[str, dict[str, str]] = {}
    scanned = 0
    kwargs = {"ProjectionExpression": "affected_service, created_at"}

    while True:
        response = table.scan(**kwargs)
        for item in response.get("Items", []):
            scanned += 1
            service = item.get("affected_service")
            created_at = item.get("created_at")
            if not service or not created_at:
                continue
            seen = services.setdefault(service, {"first": created_at, "last": created_at})
            seen["first"] = min(seen["first"], created_at)
            seen["last"] = max(seen["last"], created_at)

        last_key = response.get("LastEvaluatedKey")
        if not last_key:
            break
        kwargs["ExclusiveStartKey"] = last_key

    print(f"Scanned {scanned} incidents, found {len(services)} distinct services.")
    return services


def backfill() -> None:
    services = _collect_services()
    if not services:
        print("No services found — nothing to backfill.")
        return

    table = dynamodb.Table(REGISTRY_TABLE)
    for service, seen in sorted(services.items()):
        table.update_item(
            Key={"affected_service": service},
            # if_not_exists on both attributes keeps this idempotent and stops a
            # re-run from overwriting newer timestamps written by the dedup Lambda.
            UpdateExpression=(
                "SET first_seen_at = if_not_exists(first_seen_at, :first), "
                "last_seen_at = if_not_exists(last_seen_at, :last)"
            ),
            ExpressionAttributeValues={":first": seen["first"], ":last": seen["last"]},
        )
        print(f"  {service:24} first={seen['first']} last={seen['last']}")

    print(f"\nDone. Backfilled {len(services)} services into {REGISTRY_TABLE}.")


if __name__ == "__main__":
    backfill()
