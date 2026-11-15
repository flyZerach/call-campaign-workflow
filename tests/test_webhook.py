import hashlib
import hmac
import json
from pathlib import Path

from fastapi.testclient import TestClient
import pytest

from solution.dispatcher import FixtureDispatcher
from solution.store import LeadRecord, SQLiteStore
from solution.webhook import create_app

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "assignment-b-workflow" / "fixtures"
SECRET = json.loads((FIXTURES / "campaigns.json").read_text(encoding="utf-8"))["webhook_secret"]
END = "2026-11-12T12:15:00+05:30"
START = "2026-11-12T12:10:00+05:30"
CALLBACK_AT = "2026-11-12T15:00:00+05:30"


def setup(tmp_path, *, accepted=True):
    store = SQLiteStore(tmp_path / "webhook.sqlite3")
    dispatcher = FixtureDispatcher(FIXTURES, store)
    dispatcher.startup()
    lead = next(lead for lead in store.list_leads() if lead.campaign_id == "c_acme_renewals")
    call = store.prepare_call("webhook-request", lead.campaign_id, lead.lead_id)
    if accepted:
        call = store.mark_call_accepted(call.request_id, "CA-webhook", START)
    app = create_app(store, FIXTURES)
    return store, lead, call, TestClient(app)


def signed_post(client, payload, *, signature_override=None):
    body = json.dumps(payload, separators=(",", ":")).encode()
    signature = hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
    return client.post(
        "/webhooks/telephony", content=body,
        headers={"Content-Type": "application/json",
                 "X-AU-Signature": signature_override or signature},
    )


def payload_for(lead, call, *, status="completed", disposition="interested", callback_at=None):
    return {
        "event": "call.completed", "request_id": call.request_id, "call_sid": "CA-webhook",
        "tenant_id": "acme_insurance", "campaign_id": lead.campaign_id,
        "lead_id": lead.lead_id, "phone": lead.phone, "status": status,
        "disposition": disposition, "callback_at": callback_at,
        "started_at": START, "ended_at": END, "duration_sec": 142,
    }


def test_invalid_signature_is_rejected_without_changing_call(tmp_path):
    store, lead, call, client = setup(tmp_path)
    response = signed_post(client, payload_for(lead, call), signature_override="not-a-valid-signature")

    assert response.status_code == 401
    assert store.get_call(call.request_id).result_applied is False
    saved_lead = store.get_lead(lead.campaign_id, lead.lead_id)
    assert saved_lead.accepted_attempt_count == 1
    assert saved_lead.active_request_id == call.request_id


@pytest.mark.parametrize(("status", "disposition", "callback_at", "expected_stage"), [
    ("completed", "interested", None, "completed"),
    ("busy", None, None, "retry"),
    ("failed", None, None, "retry"),
    ("completed", "callback_requested", CALLBACK_AT, "callback"),
])
def test_valid_webhook_reduces_and_persists_terminal_retry_and_callback(
    tmp_path, status, disposition, callback_at, expected_stage
):
    store, lead, call, client = setup(tmp_path)
    response = signed_post(
        client, payload_for(lead, call, status=status, disposition=disposition, callback_at=callback_at)
    )

    assert response.status_code == 200
    assert response.json() == {"accepted": True, "duplicate": False, "request_id": call.request_id}
    saved_lead = store.get_lead(lead.campaign_id, lead.lead_id)
    saved_call = store.get_call(call.request_id)
    assert saved_lead.stage == expected_stage
    assert saved_lead.accepted_attempt_count == 1
    assert saved_lead.active_request_id is None
    assert saved_call.result_applied is True
    assert saved_call.provider_call_id == "CA-webhook"
    if callback_at:
        assert saved_lead.callback_at.isoformat() == callback_at


def test_duplicate_webhook_delivery_is_idempotent(tmp_path):
    store, lead, call, client = setup(tmp_path)
    body = payload_for(lead, call, status="busy", disposition=None)

    first = signed_post(client, body)
    duplicate = signed_post(client, body)

    assert first.status_code == duplicate.status_code == 200
    assert first.json()["duplicate"] is False
    assert duplicate.json()["duplicate"] is True
    assert store.get_lead(lead.campaign_id, lead.lead_id).accepted_attempt_count == 1
    assert len(store.list_calls(lead.campaign_id, lead.lead_id)) == 1


def test_result_before_acceptance_records_call_as_accepted_once(tmp_path):
    store, lead, call, client = setup(tmp_path, accepted=False)
    response = signed_post(client, payload_for(lead, call, status="no_answer", disposition=None))

    assert response.status_code == 200
    saved_call = store.get_call(call.request_id)
    saved_lead = store.get_lead(lead.campaign_id, lead.lead_id)
    assert saved_call.accepted is True
    assert saved_call.provider_call_id == "CA-webhook"
    assert saved_call.result_applied is True
    assert saved_lead.accepted_attempt_count == 1
    assert saved_lead.stage == "retry"
    assert saved_lead.active_request_id is None
    # The delayed original 201 response must not count this attempt again.
    store.mark_call_accepted(call.request_id, "CA-webhook", START)
    assert store.get_lead(lead.campaign_id, lead.lead_id).accepted_attempt_count == 1


def test_dnd_result_blocks_same_phone_in_other_campaign(tmp_path):
    store, lead, call, client = setup(tmp_path)
    other = LeadRecord("c_acme_feedback", "dnd-copy", lead.phone)
    store.save_lead(other)
    response = signed_post(client, payload_for(lead, call, disposition="dnd"))

    assert response.status_code == 200
    assert store.get_lead(lead.campaign_id, lead.lead_id).stage == "completed"
    assert store.get_lead(other.campaign_id, other.lead_id).stage == "dnd_blocked"
    assert store.is_phone_dnd(lead.phone)
