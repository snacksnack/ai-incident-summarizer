from datetime import datetime


def incident_duration(incident: dict) -> str | None:
    """Human-readable time from created_at to resolved_at ("1h 12m", "45s"),
    or None when either timestamp is missing or unparseable."""
    started = _parse(incident.get("created_at"))
    ended = _parse(incident.get("resolved_at"))
    if started is None or ended is None:
        return None
    seconds = max(0, int((ended - started).total_seconds()))
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


def _parse(value) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
