from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from solution.store import LeadRecord, ReservationRejected, SQLiteStore


def setup_store(path, *, limit=3):
    store = SQLiteStore(path)
    store.register_campaign("campaign-a", "tenant-a", limit)
    store.register_campaign("campaign-b", "tenant-a", limit)
    store.register_campaign("campaign-other", "tenant-b", limit)
    return store


def test_tenant_concurrency_is_shared_across_campaigns_and_persisted(tmp_path):
    database = tmp_path / "guardrails.sqlite3"
    store = setup_store(database, limit=1)
    store.save_lead(LeadRecord("campaign-a", "lead-a", "+919111111111"))
    store.save_lead(LeadRecord("campaign-b", "lead-b", "+919222222222"))
    store.prepare_call("active-a", "campaign-a", "lead-a")

    with pytest.raises(ReservationRejected) as error:
        SQLiteStore(database).prepare_call("blocked-b", "campaign-b", "lead-b")
    assert error.value.reason == "tenant_concurrency_limit"


def test_same_phone_cannot_be_active_in_two_campaigns(tmp_path):
    store = setup_store(tmp_path / "guardrails.sqlite3", limit=4)
    store.save_lead(LeadRecord("campaign-a", "lead-a", "+919111111111"))
    store.save_lead(LeadRecord("campaign-b", "lead-b", "+919111111111"))
    store.prepare_call("active-a", "campaign-a", "lead-a")

    with pytest.raises(ReservationRejected) as error:
        store.prepare_call("blocked-b", "campaign-b", "lead-b")
    assert error.value.reason == "phone_already_active"


def test_dnd_phone_is_blocked_across_campaigns_after_restart(tmp_path):
    database = tmp_path / "guardrails.sqlite3"
    store = setup_store(database, limit=4)
    phone = "+919111111111"
    store.save_lead(LeadRecord("campaign-a", "lead-a", phone))
    store.save_lead(LeadRecord("campaign-b", "lead-b", phone))
    store.prepare_call("dnd-request", "campaign-a", "lead-a")
    store.apply_call_result(
        "dnd-request",
        status="completed",
        disposition="dnd",
        ended_at="2026-11-12T12:00:00+05:30",
        next_stage="completed",
    )

    reopened = SQLiteStore(database)
    assert reopened.is_phone_dnd(phone)
    with pytest.raises(ReservationRejected) as error:
        reopened.prepare_call("blocked-after-restart", "campaign-b", "lead-b")
    assert error.value.reason == "dnd_blocked"


def test_dnd_marks_other_unfinished_leads_blocked_but_preserves_finished(tmp_path):
    store = setup_store(tmp_path / "guardrails.sqlite3", limit=4)
    phone = "+919111111111"
    store.save_lead(LeadRecord("campaign-a", "lead-a", phone))
    store.save_lead(LeadRecord("campaign-b", "lead-b", phone, stage="callback"))
    store.save_lead(LeadRecord("campaign-other", "lead-c", phone, stage="exhausted"))
    store.prepare_call("dnd-request", "campaign-a", "lead-a")

    store.apply_call_result(
        "dnd-request",
        status="completed",
        disposition="dnd",
        ended_at="2026-11-12T12:00:00+05:30",
        next_stage="completed",
    )

    assert store.get_lead("campaign-a", "lead-a").stage == "completed"
    assert store.get_lead("campaign-b", "lead-b").stage == "dnd_blocked"
    assert store.get_lead("campaign-other", "lead-c").stage == "exhausted"


def test_clear_removes_guardrail_metadata(tmp_path):
    database = tmp_path / "guardrails.sqlite3"
    store = setup_store(database)
    phone = "+919111111111"
    store.save_lead(LeadRecord("campaign-a", "lead-a", phone))
    store.prepare_call("dnd-request", "campaign-a", "lead-a")
    store.apply_call_result(
        "dnd-request",
        status="completed",
        disposition="dnd",
        ended_at="2026-11-12T12:00:00+05:30",
        next_stage="completed",
    )

    store.clear()

    assert store.list_leads() == []
    assert store.list_calls() == []
    assert not store.is_phone_dnd(phone)
    store.save_lead(LeadRecord("campaign-a", "lead-a", phone))
    with pytest.raises(KeyError, match="Unknown campaign"):
        store.prepare_call("new-request", "campaign-a", "lead-a")


def test_competing_reservations_cannot_take_same_last_tenant_slot(tmp_path):
    database = tmp_path / "race.sqlite3"
    setup_store(database, limit=1)
    setup = SQLiteStore(database)
    setup.save_lead(LeadRecord("campaign-a", "lead-a", "+919111111111"))
    setup.save_lead(LeadRecord("campaign-b", "lead-b", "+919222222222"))
    barrier = Barrier(2)

    def reserve(request_id, campaign_id, lead_id):
        contender = SQLiteStore(database)
        barrier.wait()
        try:
            contender.prepare_call(request_id, campaign_id, lead_id)
            return "reserved"
        except ReservationRejected as error:
            return error.reason

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(
            lambda args: reserve(*args),
            [("race-a", "campaign-a", "lead-a"), ("race-b", "campaign-b", "lead-b")],
        ))

    assert outcomes.count("reserved") == 1
    assert outcomes.count("tenant_concurrency_limit") == 1
    assert len(SQLiteStore(database).list_calls()) == 1
