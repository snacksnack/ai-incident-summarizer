"""Wording for a recurring incident (RC1-437), shared by the prompt, Slack
and Jira so all three say the same thing.

`incident["recurrence"]` is written by ingest when the same alert opened
other incidents for the service in the last 7 days:
    {"count_7d": 7, "previous_incident_id": ..., "previous_created_at": ...,
     "previous_jira_ticket_id": "INC-96"}   # the ticket key when it had one
"""


def ordinal(n: int) -> str:
    suffix = "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def occurrence(incident: dict) -> int | None:
    """Which occurrence this incident is inside the window (2 for the first
    repeat), or None when it is not a repeat."""
    recurrence = incident.get("recurrence") or {}
    count = int(recurrence.get("count_7d") or 0)
    return count + 1 if count else None


def badge(incident: dict) -> str | None:
    """Short form for a header: '7th time in 7 days'."""
    n = occurrence(incident)
    return f"{ordinal(n)} time in 7 days" if n else None


def previous_ticket(incident: dict) -> str | None:
    return (incident.get("recurrence") or {}).get("previous_jira_ticket_id")


def sentence(incident: dict) -> str | None:
    """One sentence for a prompt or a ticket body."""
    n = occurrence(incident)
    if not n:
        return None
    recurrence = incident["recurrence"]
    text = (
        f"This is the {ordinal(n)} incident this alert has opened for this service in the last 7 days; "
        f"the previous one was {recurrence.get('previous_created_at', 'recently')}"
    )
    ticket = previous_ticket(incident)
    return text + (f" ({ticket})." if ticket else ".")
