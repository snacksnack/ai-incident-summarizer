"""The delivery chain: Slack, then Jira, then the Datadog event timeline.

Each module exposes `deliver(incident, recovered) -> str | None`: it posts or
updates its own artifact, writes that artifact's ID back to the incident item
and into the `incident` dict it was handed, and returns the ID. The order is
fixed by `app._deliver` so the Datadog event carries both links.
"""
