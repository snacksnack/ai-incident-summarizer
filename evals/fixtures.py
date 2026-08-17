"""Fixture incidents, in the shape dedup stores them (RC1-267).

Each fixture is what `_get_incident_table().get_item` hands the summarizer:
the DynamoDB item dedup wrote. The severity is not chosen by hand — it is the
first alert's severity, because dedup sets `severity` only when it creates the
incident and the append path never touches it (`test_prompt_contract.py`
asserts that reading stays true of the shipped source). A later, worse alert
does not escalate the incident, so a fixture claiming it did would be testing
a system that does not exist.

The three LLM cases cover the shapes the pipeline actually produces:

* **single-cloudwatch** — one alarm, the everyday case
* **multi-source** — CloudWatch and Datadog correlated onto one service, and
  the trap that makes it worth freezing: the incident is *critical* (first
  alert was a P1) while two later alert names contain the word "high". A
  model that reads severity off the alert names instead of the handed-over
  field re-decides it — exactly the violation the severity check exists for
* **github-storm** — five distinct workflow failures on one repository, the
  alert-count case

`fallback-path` reuses the storm incident but scores `_fallback_summary`
deterministically — no model call, no key, no cost.
"""

from __future__ import annotations

from dataclasses import dataclass

SEVERITIES = ("critical", "high", "medium", "low")


@dataclass(frozen=True)
class Alert:
    alert_id: str
    source: str
    alert_name: str
    severity: str
    received_at: str
    status: str = "open"

    def summary(self) -> dict[str, str]:
        """Exactly dedup's `_alert_summary` shape."""
        return {
            "alert_id": self.alert_id,
            "source": self.source,
            "alert_name": self.alert_name,
            "severity": self.severity,
            "status": self.status,
            "received_at": self.received_at,
        }


@dataclass(frozen=True)
class Fixture:
    id: str
    affected_service: str
    alerts: tuple[Alert, ...]
    notes: str = ""

    @property
    def severity(self) -> str:
        """Dedup's rule: the first alert names the incident's severity."""
        return self.alerts[0].severity

    @property
    def alert_count(self) -> int:
        return len(self.alerts)

    def incident(self) -> dict:
        """The stored item the summarizer reads."""
        return {
            "incident_id": f"inc-eval-{self.id}",
            "affected_service": self.affected_service,
            "severity": self.severity,
            "status": "open",
            "source_alerts": [a.summary() for a in self.alerts],
            "created_at": self.alerts[0].received_at,
        }


FIXTURES: tuple[Fixture, ...] = (
    Fixture(
        id="single-cloudwatch",
        affected_service="payments-service",
        alerts=(
            Alert(
                alert_id="cw-1",
                source="cloudwatch",
                alert_name="payments-service-high-error-rate",
                severity="high",
                received_at="2026-08-12T14:23:00Z",
            ),
        ),
        notes="One alarm, one service — the everyday case.",
    ),
    Fixture(
        id="multi-source",
        affected_service="checkout-api",
        alerts=(
            Alert(
                alert_id="dd-1",
                source="datadog",
                alert_name="[P1] checkout-api error rate elevated",
                severity="critical",
                received_at="2026-08-12T09:14:00Z",
            ),
            Alert(
                alert_id="cw-2",
                source="cloudwatch",
                alert_name="checkout-api-high-latency",
                severity="high",
                received_at="2026-08-12T09:16:00Z",
            ),
            Alert(
                alert_id="dd-2",
                source="datadog",
                alert_name="checkout-api p95 latency degraded",
                severity="high",
                received_at="2026-08-12T09:18:00Z",
            ),
        ),
        notes=(
            "Critical incident (first alert was a P1) with 'high' in two later "
            "alert names. The severity check must catch a model that reads the "
            "names instead of the field."
        ),
    ),
    Fixture(
        id="github-storm",
        affected_service="snacksnack/deploy-pipeline",
        alerts=tuple(
            Alert(
                alert_id=f"gh-{i}",
                source="github",
                alert_name=name,
                severity="high",
                received_at=f"2026-08-12T11:{minute:02d}:00Z",
            )
            for i, (name, minute) in enumerate(
                [
                    ("CI", 2),
                    ("Deploy to staging", 5),
                    ("Integration tests", 9),
                    ("Deploy to production", 14),
                    ("Nightly build", 20),
                ],
                start=1,
            )
        ),
        notes="Five distinct workflow failures in twenty minutes — the alert-count case.",
    ),
)

BY_ID: dict[str, Fixture] = {f.id: f for f in FIXTURES}
