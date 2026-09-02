from common.duration import incident_duration


def test_hours_and_minutes():
    assert incident_duration({"created_at": "2024-01-15T10:00:00Z", "resolved_at": "2024-01-15T11:12:05Z"}) == "1h 12m"


def test_minutes_and_seconds():
    assert incident_duration({"created_at": "2024-01-15T10:00:00+00:00", "resolved_at": "2024-01-15T10:42:30+00:00"}) == "42m 30s"


def test_seconds_only():
    assert incident_duration({"created_at": "2024-01-15T10:00:00Z", "resolved_at": "2024-01-15T10:00:45Z"}) == "45s"


def test_missing_or_bad_timestamps():
    assert incident_duration({"created_at": "2024-01-15T10:00:00Z"}) is None
    assert incident_duration({"created_at": "nope", "resolved_at": "2024-01-15T10:00:45Z"}) is None
    assert incident_duration({}) is None


def test_clock_skew_never_negative():
    assert incident_duration({"created_at": "2024-01-15T10:00:10Z", "resolved_at": "2024-01-15T10:00:00Z"}) == "0s"
