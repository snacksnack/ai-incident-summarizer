"""common.recurrence: the one wording of a repeat, shared by prompt, Slack and Jira."""
import pytest

from common import recurrence

REPEAT = {
    "incident_id": "inc-9",
    "recurrence": {
        "count_7d": 6,
        "previous_incident_id": "inc-8",
        "previous_created_at": "2026-09-11T13:01:57+00:00",
        "previous_jira_ticket_id": "INC-96",
    },
}


@pytest.mark.parametrize("n,expected", [(1, "1st"), (2, "2nd"), (3, "3rd"), (4, "4th"), (11, "11th"), (12, "12th"), (13, "13th"), (21, "21st"), (22, "22nd"), (101, "101st"), (111, "111th")])
def test_ordinal(n, expected):
    assert recurrence.ordinal(n) == expected


def test_occurrence_is_count_plus_one():
    assert recurrence.occurrence(REPEAT) == 7


def test_no_recurrence_means_none_everywhere():
    incident = {"incident_id": "inc-1"}
    assert recurrence.occurrence(incident) is None
    assert recurrence.badge(incident) is None
    assert recurrence.sentence(incident) is None
    assert recurrence.previous_ticket(incident) is None


def test_zero_count_is_not_a_repeat():
    assert recurrence.occurrence({"recurrence": {"count_7d": 0}}) is None


def test_badge():
    assert recurrence.badge(REPEAT) == "7th time in 7 days"


def test_sentence_names_the_previous_incident_and_ticket():
    assert recurrence.sentence(REPEAT) == (
        "This is the 7th incident this alert has opened for this service in the last 7 days; "
        "the previous one was 2026-09-11T13:01:57+00:00 (INC-96)."
    )


def test_sentence_without_a_ticket():
    incident = {"recurrence": {"count_7d": 1, "previous_incident_id": "inc-8", "previous_created_at": "yesterday"}}
    assert recurrence.sentence(incident).endswith("the previous one was yesterday.")


def test_dynamodb_decimal_count_is_accepted():
    from decimal import Decimal
    assert recurrence.occurrence({"recurrence": {"count_7d": Decimal("2")}}) == 3
