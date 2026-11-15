"""Provider-independent lead-state transition rules for one completed call."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TypeAlias

Timestamp: TypeAlias = datetime | str | None
TERMINAL_DISPOSITIONS = {"interested", "not_interested", "wrong_number", "dnd"}
RETRY_STATUSES = {"no_answer", "busy", "failed"}
FINAL_STAGES = {"completed", "exhausted", "dnd_blocked"}


@dataclass(frozen=True)
class CallResult:
    """The outcome fields needed to reduce a lead; no provider/API objects."""

    status: str
    disposition: str | None = None
    ended_at: Timestamp = None
    callback_at: Timestamp = None


@dataclass(frozen=True)
class LeadTransition:
    """The lead stage and callback timestamp to persist after a result."""

    stage: str
    callback_at: Timestamp = None


def reduce_call_result(
    current_stage: str,
    accepted_attempt_count: int,
    result: CallResult,
    *,
    max_attempts: int = 3,
) -> LeadTransition:
    """Compute the next stage after an accepted call finishes.

    accepted_attempt_count is the count including this call. A result proves
    that its call was accepted; callers handling an early result should add one
    when the stored CallRecord has not yet been marked accepted.

    Retry timing itself remains scheduler work: it uses the saved status and
    call ended_at. DND only finishes this lead here; blocking matching phone
    numbers in other campaigns remains dispatcher-wide behavior.
    """
    if accepted_attempt_count < 1:
        raise ValueError("accepted_attempt_count must include the completed call")
    if max_attempts < 1:
        raise ValueError("max_attempts must be positive")

    # Completed has higher precedence than exhausted or DND-blocked.
    if current_stage == "completed":
        return LeadTransition("completed")
    if result.disposition in TERMINAL_DISPOSITIONS:
        return LeadTransition("completed")

    if current_stage in {"exhausted", "dnd_blocked"}:
        return LeadTransition(current_stage)

    if result.disposition == "callback_requested":
        if result.callback_at is None:
            raise ValueError("callback_requested result requires callback_at")
        stage = "exhausted" if accepted_attempt_count >= max_attempts else "callback"
        # Preserve the requested time in call history even if no attempt remains.
        return LeadTransition(stage, result.callback_at)

    if result.status in RETRY_STATUSES:
        stage = "exhausted" if accepted_attempt_count >= max_attempts else "retry"
        return LeadTransition(stage)

    if result.status == "completed":
        raise ValueError(f"unsupported completed-call disposition: {result.disposition!r}")
    raise ValueError(f"unsupported call result status: {result.status!r}")
