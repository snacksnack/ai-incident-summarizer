"""Point the real Datadog monitors at the incident summarizer (RC1-375).

TARGETS below is the source of truth for which monitors notify
@webhook-incident-summarizer, what `service:` tag each carries (the
normalizer files the incident under it; no tag means `service=unknown`)
and what priority it has ($ALERT_PRIORITY drives incident severity:
P1 critical, P2 high, P3 medium, P4 low; none falls back to alert_type,
where `error` reads as high). Re-run to converge after a monitor is
recreated or edited by hand; every change is idempotent.

    DD_API_KEY=... DD_APP_KEY=... python scripts/wire_datadog_monitors.py [--dry-run]

Synthetics-backed monitors cannot be edited through the monitor API
("Synthetics-managed", 400). Their message, tags and priority live on the
synthetics test, so those targets are resolved monitor -> test (the
monitor's `synthetics_check_id`) and updated with a full-body PUT on the
type-specific test endpoint. Query and ci-pipelines monitors take a
partial monitor PUT.

Deliberately NOT wired: the six Program KPI monitors (the sim keeps one
tripped by script for weeks) and the seven host-pack monitors (No Data,
unrouted). Test monitor 318762066 already notifies the webhook.
"""
import argparse
import os
import sys
from dataclasses import dataclass

import requests

API = "https://api.datadoghq.com/api/v1"
WEBHOOK_HANDLE = "@webhook-incident-summarizer"

# Keys the synthetics GET returns that the PUT rejects as read-only.
_SYNTHETICS_READ_ONLY = ("public_id", "monitor_id", "created_at", "modified_at", "creator")


@dataclass(frozen=True)
class Target:
    monitor_id: int
    name: str  # checked against Datadog so a wrong id is caught before a write
    service: str
    priority: int


TARGETS = (
    Target(317622270, "[Synthetics] Portfolio up — www.hihelloreid.com", "hihelloreid.com", 2),
    Target(317968584, "Portfolio render check — hihelloreid.com/work", "hihelloreid.com", 3),
    Target(317971078, "[Synthetics] SSL cert — www.hihelloreid.com", "hihelloreid.com", 3),
    Target(317622273, "[Synthetics] Incidents dashboard up — incidents.hihelloreid.com", "incidents.hihelloreid.com", 2),
    Target(317971081, "[Synthetics] SSL cert — incidents.hihelloreid.com", "incidents.hihelloreid.com", 3),
    # The two ci-pipelines monitors group by repository, and the alert's tags
    # carry git.repository.name, but the normalizer reads only service:, so
    # they share one fixed service rather than one per repo (agreed on RC1-375).
    Target(318355170, "Production deploy failed — Fly & Heroku paths", "delivery-pipeline", 2),
    Target(318355169, "CI pipeline failed — delivery repos", "delivery-pipeline", 3),
    Target(318097614, "Fleet LLM spend guardrail — daily", "agent-fleet", 3),
    Target(318833109, "Fleet LLM cost per call — price signal", "agent-fleet", 3),
)


def plan_message(message: str) -> str:
    """Append the webhook handle on its own line, keeping every existing @ handle."""
    if WEBHOOK_HANDLE in message:
        return message
    return message.rstrip() + "\n" + WEBHOOK_HANDLE


def plan_tags(tags: list[str], service: str) -> list[str]:
    """Add service:<service>; a monitor that already has a service tag keeps it."""
    tags = list(tags or [])
    if any(t.startswith("service:") for t in tags):
        return tags
    return tags + [f"service:{service}"]


def plan_changes(current: dict, target: Target) -> dict:
    """Return only the fields that differ from the target's wiring (empty = converged)."""
    changes = {}
    message = plan_message(current.get("message") or "")
    if message != current.get("message"):
        changes["message"] = message
    tags = plan_tags(current.get("tags") or [], target.service)
    if tags != (current.get("tags") or []):
        changes["tags"] = tags
    if current.get("priority") != target.priority:
        changes["priority"] = target.priority
    return changes


class Datadog:
    def __init__(self, api_key: str, app_key: str):
        self.headers = {"DD-API-KEY": api_key, "DD-APPLICATION-KEY": app_key}

    def get(self, path: str) -> dict:
        resp = requests.get(f"{API}{path}", headers=self.headers, timeout=15)
        resp.raise_for_status()
        return resp.json()

    def put(self, path: str, body: dict) -> dict:
        resp = requests.put(f"{API}{path}", headers=self.headers, json=body, timeout=15)
        if not resp.ok:
            raise RuntimeError(f"PUT {path} -> {resp.status_code}: {resp.text[:500]}")
        return resp.json()


def _synthetics_path(test_type: str, public_id: str) -> str:
    # The generic /synthetics/tests/{id} GET omits a browser test's steps, and a
    # full-body PUT without them would drop them; the typed endpoints carry everything.
    return f"/synthetics/tests/{test_type}/{public_id}"


def wire(dd: Datadog, target: Target, dry_run: bool) -> str:
    monitor = dd.get(f"/monitor/{target.monitor_id}")
    if monitor["name"] != target.name:
        raise RuntimeError(f"monitor {target.monitor_id} is {monitor['name']!r}, expected {target.name!r}")

    check_id = (monitor.get("options") or {}).get("synthetics_check_id")
    if check_id:
        test = dd.get(_synthetics_path(monitor_test_type(monitor), check_id))
        current = {"message": test["message"], "tags": test.get("tags"), "priority": test.get("options", {}).get("monitor_priority")}
    else:
        test = None
        current = {"message": monitor["message"], "tags": monitor.get("tags"), "priority": monitor.get("priority")}

    changes = plan_changes(current, target)
    label = f"{target.monitor_id} {target.name}" + (f" (synthetics {check_id})" if check_id else "")
    if not changes:
        return f"ok       {label}"
    summary = ", ".join(f"{k}={v!r}" if k != "message" else "message+webhook" for k, v in changes.items())
    if dry_run:
        return f"would    {label}: {summary}"

    if test is not None:
        body = {k: v for k, v in test.items() if k not in _SYNTHETICS_READ_ONLY}
        # Browser steps carry their own public_id, which the PUT rejects too.
        body["steps"] = [{k: v for k, v in step.items() if k != "public_id"} for step in body.get("steps", [])] or None
        if body["steps"] is None:
            del body["steps"]
        body["message"] = changes.get("message", body["message"])
        body["tags"] = changes.get("tags", body.get("tags"))
        body["options"]["monitor_priority"] = target.priority
        dd.put(_synthetics_path(test["type"], check_id), body)
    else:
        dd.put(f"/monitor/{target.monitor_id}", changes)
    return f"updated  {label}: {summary}"


def monitor_test_type(monitor: dict) -> str:
    # Monitor tags mirror the test's check_type (api, api-ssl, browser, ...).
    for tag in monitor.get("tags") or []:
        if tag.startswith("check_type:"):
            return "browser" if tag.endswith("browser") else "api"
    raise RuntimeError(f"monitor {monitor['id']} has no check_type tag; cannot pick the synthetics endpoint")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true", help="report what would change, write nothing")
    args = parser.parse_args()

    dd = Datadog(os.environ["DD_API_KEY"], os.environ["DD_APP_KEY"])
    failures = 0
    for target in TARGETS:
        try:
            print(wire(dd, target, args.dry_run))
        except Exception as exc:  # keep going: one bad monitor must not block the rest
            failures += 1
            print(f"FAILED   {target.monitor_id} {target.name}: {exc}", file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
