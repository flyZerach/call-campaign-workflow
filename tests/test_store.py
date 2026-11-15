from datetime import datetime

import pytest

from solution.store import CallRecord, LeadRecord, SQLiteStore

def store_for(path):
    store = SQLiteStore(path)
    store.register_campaign("camp-a", "tenant-a", 4)
    return store


NOW = datetime.fromisoformat("2026-11-12T12:15:00+05:30")
START = datetime.fromisoformat("2026-11-12T12:10:00+05:30")


def test_lead_and_call_survive_store_reopen(tmp_path):
    database = tmp_path / "dispatcher.sqlite3"
    store = store_for(database)
    store.save_lead(LeadRecord(
        campaign_id="camp-a", lead_id="lead-1", phone="+919876543210",
        stage="callback", accepted_attempt_count=0, last_status="busy",
        last_disposition=None, callback_at=NOW,
    ))
    store.prepare_call("req-1", "camp-a", "lead-1")
    store.mark_call_accepted("req-1", "provider-1", START)
    store.mark_call_accepted("req-1", "provider-1", START)

    reopened = SQLiteStore(database)
    lead = reopened.get_lead("camp-a", "lead-1")
    call = reopened.get_call("req-1")

    assert lead == LeadRecord(
        campaign_id="camp-a", lead_id="lead-1", phone="+919876543210",
        stage="active", accepted_attempt_count=1, last_status="busy",
        active_request_id="req-1", active_call_id="provider-1",
        callback_at=NOW,
    )
    assert call == CallRecord(
        request_id="req-1", campaign_id="camp-a", lead_id="lead-1",
        provider_call_id="provider-1", status="active", started_at=START,
        accepted=True,
    )


def test_duplicate_call_result_is_idempotent(tmp_path):
    store = store_for(tmp_path / "dispatcher.sqlite3")
    store.save_lead(LeadRecord("camp-a", "lead-1", "+919876543210"))
    store.prepare_call("req-1", "camp-a", "lead-1")
    store.mark_call_accepted("req-1", "provider-1", START)

    result = dict(
        status="completed", disposition="callback_requested", ended_at=NOW,
        callback_at=datetime.fromisoformat("2026-11-12T15:00:00+05:30"),
        provider_call_id="provider-1", started_at=START, next_stage="callback",
    )
    assert store.apply_call_result("req-1", **result) is True
    assert store.apply_call_result("req-1", **result) is False

    lead = store.get_lead("camp-a", "lead-1")
    call = store.get_call("req-1")
    assert lead.accepted_attempt_count == 1
    assert lead.stage == "callback"
    assert lead.last_status == "completed"
    assert lead.last_disposition == "callback_requested"
    assert lead.active_request_id is None
    assert len(store.list_calls("camp-a", "lead-1")) == 1
    assert call.result_applied is True
    assert call.ended_at == NOW


def test_early_result_then_acceptance_response_counts_once(tmp_path):
    store = store_for(tmp_path / "dispatcher.sqlite3")
    store.save_lead(LeadRecord("camp-a", "lead-1", "+919876543210"))
    store.prepare_call("req-1", "camp-a", "lead-1")
    store.apply_call_result(
        "req-1", status="busy", disposition=None, ended_at=NOW,
        provider_call_id=None, started_at=START, next_stage="retry",
    )
    # The original provider response is read after its webhook was processed.
    call = store.mark_call_accepted("req-1", "provider-1", START)

    assert call.provider_call_id == "provider-1"
    assert call.result_applied is True
    assert store.get_lead("camp-a", "lead-1").accepted_attempt_count == 1


def test_conflicting_duplicate_result_is_rejected(tmp_path):
    store = store_for(tmp_path / "dispatcher.sqlite3")
    store.save_lead(LeadRecord("camp-a", "lead-1", "+919876543210"))
    store.prepare_call("req-1", "camp-a", "lead-1")
    store.apply_call_result("req-1", status="busy", disposition=None, ended_at=NOW, next_stage="retry")

    with pytest.raises(ValueError, match="conflicting result"):
        store.apply_call_result("req-1", status="no_answer", disposition=None, ended_at=NOW, next_stage="retry")


def test_clear_removes_calls_and_leads(tmp_path):
    store = store_for(tmp_path / "dispatcher.sqlite3")
    store.save_lead(LeadRecord("camp-a", "lead-1", "+919876543210"))
    store.prepare_call("req-1", "camp-a", "lead-1")

    store.clear()

    assert store.list_leads() == []
    assert store.list_calls() == []
