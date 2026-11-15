"""Provider-independent scheduling rules for Assignment B.

This module answers when one lead is eligible and orders currently eligible
leads. Persistence, provider calls, retries of HTTP requests, and concurrency
reservations belong to the dispatcher.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta
from typing import Iterable, Mapping, Sequence
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
DAY_NAMES = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
RETRY_DELAYS = {
    "no_answer": timedelta(minutes=60),
    "busy": timedelta(minutes=60),
    "failed": timedelta(minutes=15),
}
FINAL_STATES = {"completed", "exhausted", "dnd_blocked"}


def _as_ist(value: datetime) -> datetime:
    """Normalize an instant to IST; naive datetimes are interpreted as IST."""
    if value.tzinfo is None:
        return value.replace(tzinfo=IST)
    return value.astimezone(IST)


def _clock(value: str) -> time:
    hour, minute = (int(part) for part in value.split(":"))
    return time(hour, minute)


def _datetime(value: datetime | str | None) -> datetime | None:
    if value is None or isinstance(value, datetime):
        return value
    # Fixture/provider timestamps may use Z, an explicit offset, or no offset.
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


@dataclass(frozen=True)
class Campaign:
    campaign_id: str
    window_start: time
    window_end: time
    days: frozenset[int]
    max_attempts: int = 3
    regulatory_start: time = time(9, 0)
    regulatory_end: time = time(21, 0)

    @classmethod
    def from_mapping(cls, data: Mapping) -> "Campaign":
        """Build from a campaign object in fixtures/campaigns.json."""
        window = data["window"]
        day_indexes = {DAY_NAMES.index(day.lower()[:3]) for day in window["days"]}
        regulatory = data.get("regulatory_window", {})
        return cls(
            campaign_id=data["campaign_id"],
            window_start=_clock(window["start"]),
            window_end=_clock(window["end"]),
            days=frozenset(day_indexes),
            max_attempts=int(data.get("max_attempts", 3)),
            regulatory_start=_clock(regulatory["start"]) if regulatory else time(9, 0),
            regulatory_end=_clock(regulatory["end"]) if regulatory else time(21, 0),
        )


@dataclass(frozen=True)
class LeadState:
    """Scheduling-relevant snapshot; a dispatcher can map DB rows into this.

    stage is one of: fresh, retry, callback, active, completed, exhausted,
    dnd_blocked. ended_at is the previous call's provider-reported end time.
    """
    lead_id: str
    stage: str = "fresh"
    attempts: int = 0
    last_status: str | None = None
    ended_at: datetime | None = None
    callback_at: datetime | None = None

    @classmethod
    def from_mapping(cls, data: Mapping) -> "LeadState":
        return cls(
            lead_id=str(data["lead_id"]),
            stage=str(data.get("stage", "fresh")),
            attempts=int(data.get("attempts", 0)),
            last_status=data.get("last_status"),
            ended_at=_datetime(data.get("ended_at")),
            callback_at=_datetime(data.get("callback_at")),
        )


def _is_in_window(campaign: Campaign, instant: datetime) -> bool:
    local = _as_ist(instant)
    return (
        local.weekday() in campaign.days
        and campaign.window_start <= local.time().replace(tzinfo=None) < campaign.window_end
        and campaign.regulatory_start <= local.time().replace(tzinfo=None) < campaign.regulatory_end
    )


def _next_opening(campaign: Campaign, instant: datetime) -> datetime:
    """First instant >= instant that lies in both allowed windows."""
    local = _as_ist(instant)
    for offset in range(0, 9):
        day = (local + timedelta(days=offset)).date()
        if day.weekday() not in campaign.days:
            continue
        opening = max(campaign.window_start, campaign.regulatory_start)
        closing = min(campaign.window_end, campaign.regulatory_end)
        if opening >= closing:
            continue
        candidate = datetime.combine(day, opening, tzinfo=IST)
        if offset == 0 and local > candidate:
            candidate = local
        if candidate.date() == day and candidate.time().replace(tzinfo=None) < closing:
            return candidate
    raise ValueError("Campaign has no valid calling day/window")


def _stage_and_due(state: LeadState, now: datetime) -> tuple[str, datetime] | None:
    if state.stage in FINAL_STATES or state.stage in {"active", "in_flight"}:
        return None
    if state.stage == "callback":
        if state.callback_at is None:
            raise ValueError("callback stage requires callback_at")
        return "callback", _as_ist(state.callback_at)
    if state.stage == "retry":
        if state.ended_at is None or state.last_status not in RETRY_DELAYS:
            raise ValueError("retry stage requires ended_at and a retryable last_status")
        return "retry", _as_ist(state.ended_at) + RETRY_DELAYS[state.last_status]
    if state.stage == "fresh":
        return "fresh", _as_ist(now)
    raise ValueError(f"Unknown lead stage: {state.stage}")


def next_eligible_at(campaign: Campaign, state: LeadState, now: datetime) -> datetime | None:
    """Return the next allowed instant for this lead, or None if it cannot be called."""
    if state.attempts >= campaign.max_attempts:
        return None
    category_due = _stage_and_due(state, now)
    if category_due is None:
        return None
    _, due = category_due
    return _next_opening(campaign, max(_as_ist(now), due))


def can_call_now(campaign: Campaign, state: LeadState, now: datetime) -> bool:
    """True when the lead is eligible and the simulated instant is in its window."""
    eligible_at = next_eligible_at(campaign, state, now)
    return eligible_at is not None and _as_ist(now) >= eligible_at and _is_in_window(campaign, now)


def priority_order(
    candidates: Iterable[tuple[Campaign, LeadState]], now: datetime
) -> list[tuple[Campaign, LeadState]]:
    """Filter to callable leads and sort callbacks, retries, then fresh leads.

    Within each category, older due work comes first, followed by stable IDs.
    """
    ranked = []
    rank = {"callback": 0, "retry": 1, "fresh": 2}
    for campaign, state in candidates:
        if not can_call_now(campaign, state, now):
            continue
        category_due = _stage_and_due(state, now)
        assert category_due is not None
        category, due = category_due
        ranked.append(((rank[category], due, campaign.campaign_id, state.lead_id), (campaign, state)))
    ranked.sort(key=lambda item: item[0])
    return [item[1] for item in ranked]
