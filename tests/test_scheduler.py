from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from solution.scheduler import Campaign, LeadState, can_call_now, next_eligible_at, priority_order

IST = ZoneInfo("Asia/Kolkata")
NOW = datetime(2026, 11, 12, 11, 30, tzinfo=IST)


@pytest.fixture
def campaign():
    return Campaign(
        campaign_id="weekday",
        window_start=datetime.strptime("10:00", "%H:%M").time(),
        window_end=datetime.strptime("19:00", "%H:%M").time(),
        days=frozenset({0, 1, 2, 3, 4, 5}),
    )


def test_window_is_start_inclusive_end_exclusive(campaign):
    fresh = LeadState("lead")
    assert can_call_now(campaign, fresh, datetime(2026, 11, 12, 10, 0, tzinfo=IST))
    assert not can_call_now(campaign, fresh, datetime(2026, 11, 12, 19, 0, tzinfo=IST))


def test_regulatory_window_and_non_campaign_day_are_respected(campaign):
    fresh = LeadState("lead")
    assert not can_call_now(campaign, fresh, datetime(2026, 11, 12, 8, 59, tzinfo=IST))
    # Sunday is excluded by this campaign's Monday-Saturday schedule.
    assert next_eligible_at(campaign, fresh, datetime(2026, 11, 15, 12, 0, tzinfo=IST)) == datetime(
        2026, 11, 16, 10, 0, tzinfo=IST
    )


def test_retry_delay_and_window_projection(campaign):
    retry = LeadState(
        "lead", stage="retry", attempts=1, last_status="no_answer",
        ended_at=datetime(2026, 11, 12, 18, 30, tzinfo=IST),
    )
    # 60 simulated minutes later is after the campaign closes; next day opens at 10.
    assert next_eligible_at(campaign, retry, NOW) == datetime(2026, 11, 13, 10, 0, tzinfo=IST)


def test_failed_retry_waits_fifteen_minutes(campaign):
    retry = LeadState(
        "lead", stage="retry", attempts=1, last_status="failed",
        ended_at=datetime(2026, 11, 12, 11, 0, tzinfo=IST),
    )
    before_due = datetime(2026, 11, 12, 11, 5, tzinfo=IST)
    assert next_eligible_at(campaign, retry, before_due) == datetime(2026, 11, 12, 11, 15, tzinfo=IST)
    assert not can_call_now(campaign, retry, before_due)


def test_callback_waits_until_requested_time_then_window(campaign):
    callback = LeadState(
        "lead", stage="callback", attempts=1,
        callback_at=datetime(2026, 11, 12, 20, 0, tzinfo=IST),
    )
    assert next_eligible_at(campaign, callback, NOW) == datetime(2026, 11, 13, 10, 0, tzinfo=IST)


def test_due_callbacks_precede_retries_then_fresh(campaign):
    callback = LeadState("c", stage="callback", attempts=1, callback_at=NOW)
    retry = LeadState(
        "r", stage="retry", attempts=1, last_status="busy",
        ended_at=datetime(2026, 11, 12, 10, 0, tzinfo=IST),
    )
    fresh = LeadState("f")
    ordered = priority_order(
        [(campaign, fresh), (campaign, retry), (campaign, callback)], NOW
    )
    assert [state.lead_id for _, state in ordered] == ["c", "r", "f"]


def test_ineligible_and_finished_leads_are_excluded(campaign):
    future_callback = LeadState(
        "later", stage="callback", attempts=1,
        callback_at=datetime(2026, 11, 12, 12, 0, tzinfo=IST),
    )
    finished = LeadState("done", stage="completed")
    exhausted = LeadState("spent", attempts=campaign.max_attempts)
    ordered = priority_order(
        [(campaign, future_callback), (campaign, finished), (campaign, exhausted)], NOW
    )
    assert ordered == []


def test_malformed_retry_state_fails_loudly(campaign):
    with pytest.raises(ValueError, match="requires ended_at"):
        next_eligible_at(campaign, LeadState("bad", stage="retry"), NOW)
