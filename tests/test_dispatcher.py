from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from solution.dispatcher import FixtureDispatcher
from solution.store import LeadRecord, SQLiteStore

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "assignment-b-workflow" / "fixtures"
IST = ZoneInfo("Asia/Kolkata")
NOW = datetime(2026, 11, 12, 12, 0, tzinfo=IST)


def test_startup_seeds_missing_leads_but_preserves_saved_state(tmp_path):
    database = tmp_path / "dispatcher.sqlite3"
    store = SQLiteStore(database)
    first = FixtureDispatcher(FIXTURES, store)
    inserted = first.startup()
    assert inserted > 0

    preserved_key = ("c_acme_renewals", "ren_0001")
    original = store.get_lead(*preserved_key)
    saved_state = replace(
        original,
        stage="callback",
        accepted_attempt_count=2,
        last_status="completed",
        last_disposition="callback_requested",
        callback_at=NOW + timedelta(minutes=20),
    )
    store.save_lead(saved_state)

    # Persist separate active call state too; startup should reload it.
    active = store.get_lead("c_acme_renewals", "ren_0002")
    store.prepare_call("resume-request", active.campaign_id, active.lead_id)
    store.mark_call_accepted("resume-request", "provider-call-1", NOW)

    reopened = FixtureDispatcher(FIXTURES, SQLiteStore(database))
    assert reopened.startup() == 0
    assert reopened.leads[preserved_key] == saved_state
    assert any(call.request_id == "resume-request" for call in reopened.calls)
    assert reopened.store.get_lead("c_acme_renewals", "ren_0002").active_call_id == "provider-call-1"


def test_dispatcher_orders_with_scheduler_and_persists_only_call_intent(tmp_path):
    store = SQLiteStore(tmp_path / "dispatcher.sqlite3")
    dispatcher = FixtureDispatcher(FIXTURES, store)
    dispatcher.startup()

    campaign_id = "c_acme_feedback"
    leads = [lead for (cid, _), lead in dispatcher.leads.items() if cid == campaign_id]
    callback_lead, retry_lead, fresh_lead = leads[:3]

    store.save_lead(replace(
        callback_lead,
        stage="callback",
        accepted_attempt_count=1,
        callback_at=NOW - timedelta(minutes=5),
    ))

    store.prepare_call("retry-request", campaign_id, retry_lead.lead_id)
    store.mark_call_accepted("retry-request", "retry-provider-call", NOW - timedelta(minutes=70))
    store.apply_call_result(
        "retry-request",
        status="busy",
        disposition=None,
        started_at=NOW - timedelta(minutes=70),
        ended_at=NOW - timedelta(minutes=65),
        provider_call_id="retry-provider-call",
        next_stage="retry",
    )
    dispatcher.reload_state()

    ordered = dispatcher.ordered_eligible(NOW)
    positions = {
        lead.lead_id: index
        for index, (_, lead) in enumerate(ordered)
    }
    assert positions[callback_lead.lead_id] < positions[retry_lead.lead_id]
    assert positions[retry_lead.lead_id] < positions[fresh_lead.lead_id]

    intent = dispatcher.prepare_next_call(NOW, request_id="selected-intent")
    assert intent.request_id == "selected-intent"
    assert intent.status == "request_pending"
    assert intent.lead_id == callback_lead.lead_id
    saved_callback = store.get_lead(campaign_id, callback_lead.lead_id)
    assert saved_callback.active_request_id == "selected-intent"
    assert store.get_call("selected-intent").provider_call_id is None
