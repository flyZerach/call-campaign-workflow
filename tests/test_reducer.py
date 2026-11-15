from datetime import datetime

import pytest

from solution.reducer import CallResult, reduce_call_result

CALLBACK_AT = datetime.fromisoformat("2026-11-12T15:00:00+05:30")


@pytest.mark.parametrize("disposition", ["interested", "not_interested", "wrong_number", "dnd"])
def test_terminal_disposition_completes_even_at_attempt_limit(disposition):
    transition = reduce_call_result(
        "active", 3, CallResult("completed", disposition), max_attempts=3
    )
    assert transition.stage == "completed"
    assert transition.callback_at is None


def test_callback_requested_schedules_callback_time():
    transition = reduce_call_result(
        "active", 1,
        CallResult("completed", "callback_requested", callback_at=CALLBACK_AT),
    )
    assert transition.stage == "callback"
    assert transition.callback_at == CALLBACK_AT


def test_callback_on_last_attempt_keeps_time_but_lead_is_exhausted():
    transition = reduce_call_result(
        "active", 3,
        CallResult("completed", "callback_requested", callback_at=CALLBACK_AT),
    )
    assert transition.stage == "exhausted"
    assert transition.callback_at == CALLBACK_AT


@pytest.mark.parametrize("status", ["no_answer", "busy", "failed"])
def test_retryable_status_schedules_retry_before_limit(status):
    transition = reduce_call_result("active", 2, CallResult(status, ended_at=CALLBACK_AT))
    assert transition.stage == "retry"
    assert transition.callback_at is None


@pytest.mark.parametrize("status", ["no_answer", "busy", "failed"])
def test_retryable_status_exhausts_at_attempt_limit(status):
    transition = reduce_call_result("active", 3, CallResult(status, ended_at=CALLBACK_AT))
    assert transition.stage == "exhausted"


def test_existing_dnd_block_is_preserved_for_nonterminal_result():
    transition = reduce_call_result("dnd_blocked", 1, CallResult("busy"))
    assert transition.stage == "dnd_blocked"


def test_callback_requires_a_time():
    with pytest.raises(ValueError, match="requires callback_at"):
        reduce_call_result("active", 1, CallResult("completed", "callback_requested"))


def test_completed_lead_cannot_be_reopened_by_another_result():
    transition = reduce_call_result(
        "completed", 1, CallResult("completed", "callback_requested", callback_at=CALLBACK_AT)
    )
    assert transition.stage == "completed"
    assert transition.callback_at is None
