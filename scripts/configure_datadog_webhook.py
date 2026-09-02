"""Push the incident-summarizer webhook's payload template to Datadog.

The webhook definition lives in Datadog, not in this repo, so this script is
the source of truth for what Datadog sends to /webhook/datadog. Run it after
changing PAYLOAD_TEMPLATE, or to repair a webhook that has drifted back to
Datadog's default payload (RC1-370: the default carries no tags, priority,
transition or monitor id, and the normalizer needs all four).

    DD_API_KEY=... DD_APP_KEY=... python scripts/configure_datadog_webhook.py [--name incident-summarizer]

The X-Webhook-Secret header is re-sent exactly as Datadog currently holds it,
so no secret is needed here. The URL is kept unless --url is given; it must
include the API stage (see the WebhookApiUrl stack output), or API Gateway
answers 404 before the receiver ever runs.
"""
import argparse
import json
import os
import sys

import requests

WEBHOOK_API = "https://api.datadoghq.com/api/v1/integration/webhooks/configuration/webhooks"

# Variables: https://docs.datadoghq.com/integrations/webhooks/#usage
# - alert_id is $ALERT_ID, the monitor's id (stable across trigger/recovery);
#   id is $ID, the event's id (one per transition).
# - tags renders as ONE comma-separated string; the normalizer splits it.
# - priority is $ALERT_PRIORITY (P1-P5, empty when the monitor has none);
#   alert_type is the fallback for severity.
# - title is $ALERT_TITLE, "[Triggered on {…}] Name"; the normalizer strips
#   the bracketed transition prefix so the fingerprint is the monitor name.
PAYLOAD_TEMPLATE = {
    "id": "$ID",
    "alert_id": "$ALERT_ID",
    "alert_cycle_key": "$ALERT_CYCLE_KEY",
    "title": "$ALERT_TITLE",
    "event_title": "$EVENT_TITLE",
    "body": "$EVENT_MSG",
    "priority": "$ALERT_PRIORITY",
    "alert_type": "$ALERT_TYPE",
    "alert_transition": "$ALERT_TRANSITION",
    "tags": "$TAGS",
    "url": "$LINK",
    "date": "$DATE",
    "last_updated": "$LAST_UPDATED",
    "event_type": "$EVENT_TYPE",
    "org": {"id": "$ORG_ID", "name": "$ORG_NAME"},
}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--name", default="incident-summarizer")
    parser.add_argument("--url", help="replace the target URL too (the WebhookApiUrl stack output + /webhook/datadog)")
    parser.add_argument("--dry-run", action="store_true", help="print the template, change nothing")
    args = parser.parse_args()

    payload = json.dumps(PAYLOAD_TEMPLATE, indent=4)
    if args.dry_run:
        print(payload)
        return 0

    headers = {"DD-API-KEY": os.environ["DD_API_KEY"], "DD-APPLICATION-KEY": os.environ["DD_APP_KEY"]}
    current = requests.get(f"{WEBHOOK_API}/{args.name}", headers=headers, timeout=15)
    current.raise_for_status()
    current = current.json()

    body = {
        "name": args.name,
        "url": args.url or current["url"],
        "custom_headers": current.get("custom_headers"),
        "encode_as": "json",
        "payload": payload,
    }
    resp = requests.put(f"{WEBHOOK_API}/{args.name}", headers=headers, json=body, timeout=15)
    if not resp.ok:
        print(f"Datadog rejected the update ({resp.status_code}): {resp.text[:500]}", file=sys.stderr)
        return 1
    print(f"Updated webhook {args.name!r} -> {body['url']}")
    print(resp.json()["payload"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
