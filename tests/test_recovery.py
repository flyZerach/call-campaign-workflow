import hashlib
import hmac
import json
from pathlib import Path

from fastapi.testclient import TestClient

from solution.dispatcher import FixtureDispatcher
from solution.recovery import CallRecovery
from solution.store import SQLiteStore
from solution.webhook import create_app

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "assignment-b-workflow" / "fixtures"
CONFIG = json.loads((FIXTURES / "campaigns.json").read_text(encoding="utf-8"))
CAMPAIGNS = {campaign["campaign_id"]: campaign for campaign in CONFIG["campaigns"]}
SECRET = CONFIG["webhook_secret"].encode()
START = "2026-11-12T12:10:00+05:30"
END = "2026-11-12T12:15:00+05:30"


class Response:
    def __init__(self, status, body):
        self.status_code = status
        self._body = body

    def json(self):
        return self._body


class FakeProvider:
    def __init__(self, by_request=None, by_call_id=None):
        self.by_request = by_request or {}
        self.by_call_id = by_call_id or {}
        self.request_queries = []
        self.call_queries = []
        self.on_request_lookup = None

    def find_by_request_id(self, request_id):
        self.request_queries.append(request_id)
        if self.on_request_lookup:
            self.on_request_lookup()
            self.on_request_lookup = None
        return self.by_request[request_id]

    def find_by_call_id(self, call_sid):
        self.call_queries.append(call_sid)
        return self.by_call_id[call_sid]


def setup(tmp_path, *, accepted=False, provider=None):
    store = SQLiteStore(tmp_path / "recovery.sqlite3")
    FixtureDispatcher(FIXTURES, store).startup()
    lead = next(item for item in store.list_leads() if item.campaign_id == "c_acme_renewals")
    call = store.prepare_call("recover-request", lead.campaign_id, lead.lead_id)
    if accepted:
        call = store.mark_call_accepted(call.request_id, "CA-recover", START)
    app = create_app(store, FIXTURES)
    webhook_client = TestClient(app)
    recovery = CallRecovery(store, provider or FakeProvider(), CAMPAIGNS)
    return store, lead, call, webhook_client, recovery, recovery.provider


def provider_record(call, *, state="active", result=None):
    data = {"call_sid": "CA-recover", "request_id": call.request_id,
            "campaign_id": call.campaign_id, "lead_id": call.lead_id,
            "created_at": START, "state": state}
    if result is not None:
        data["result"] = result
    return data


def result_payload(call, lead, *, status="busy", disposition=None):
    return {
        "event": "call.completed", "call_sid": "CA-recover",
        "request_id": call.request_id, "tenant_id": "acme_insurance",
        "campaign_id": lead.campaign_id, "lead_id": lead.lead_id,
        "phone": lead.phone, "status": status, "disposition": disposition,
        "callback_at": None, "started_at": START, "ended_at": END,
        "duration_sec": 5,
    }


def signed_webhook(client, payload):
    body = json.dumps(payload, separators=(",", ":")).encode()
    signature = hmac.new(SECRET, body, hashlib.sha256).hexdigest()
    return client.post("/webhooks/telephony", content=body, headers={
        "Content-Type": "application/json", "X-AU-Signature": signature
    })


def test_pending_request_found_is_marked_accepted(tmp_path):
    provider = FakeProvider()
    store, lead, call, _, recovery, _ = setup(tmp_path, provider=provider)
    provider.by_request[call.request_id] = Response(200, provider_record(call))

    result = recovery.recover_call(call.request_id)

    assert result.state == "active"
    saved = store.get_call(call.request_id)
    assert saved.accepted is True
    assert saved.provider_call_id == "CA-recover"
    assert store.get_lead(lead.campaign_id, lead.lead_id).accepted_attempt_count == 1
    assert provider.request_queries == [call.request_id]


def test_pending_request_not_found_keeps_reservation_and_id(tmp_path):
    provider = FakeProvider()
    store, lead, call, _, recovery, _ = setup(tmp_path, provider=provider)
    provider.by_request[call.request_id] = Response(404, {"error": "not_found"})

    result = recovery.recover_call(call.request_id)

    assert result.state == "unresolved"
    assert store.get_call(call.request_id).status == "request_pending"
    saved_lead = store.get_lead(lead.campaign_id, lead.lead_id)
    assert saved_lead.active_request_id == call.request_id
    assert saved_lead.accepted_attempt_count == 0

def test_accepted_call_still_active_is_left_active_and_queried_by_call_id(tmp_path):
    provider = FakeProvider()
    store, lead, call, _, recovery, _ = setup(tmp_path, accepted=True, provider=provider)
    provider.by_call_id["CA-recover"] = Response(200, provider_record(call, state="active"))

    result = recovery.recover_call(call.request_id)

    assert result.state == "active"
    saved = store.get_call(call.request_id)
    assert saved.accepted and not saved.result_applied
    assert store.get_lead(lead.campaign_id, lead.lead_id).accepted_attempt_count == 1
    assert provider.call_queries == ["CA-recover"]


def test_accepted_call_with_ended_result_is_reduced_and_applied(tmp_path):
    provider = FakeProvider()
    store, lead, call, _, recovery, _ = setup(tmp_path, accepted=True, provider=provider)
    outcome = result_payload(call, lead, status="no_answer")
    provider.by_call_id["CA-recover"] = Response(200, provider_record(call, state="ended", result=outcome))

    result = recovery.recover_call(call.request_id)

    assert result.state == "result_applied" and result.result_applied
    assert store.get_call(call.request_id).result_applied
    saved_lead = store.get_lead(lead.campaign_id, lead.lead_id)
    assert saved_lead.stage == "retry"
    assert saved_lead.accepted_attempt_count == 1
    assert saved_lead.last_status == "no_answer"


def test_repeated_recovery_does_not_query_or_apply_result_twice(tmp_path):
    provider = FakeProvider()
    store, lead, call, _, recovery, _ = setup(tmp_path, accepted=True, provider=provider)
    outcome = result_payload(call, lead, status="busy")
    provider.by_call_id["CA-recover"] = Response(200, provider_record(call, state="ended", result=outcome))

    first = recovery.recover_call(call.request_id)
    second = recovery.recover_call(call.request_id)

    assert first.result_applied is True
    assert second.state == "result_applied" and second.result_applied is False
    assert provider.call_queries == ["CA-recover"]
    assert store.get_lead(lead.campaign_id, lead.lead_id).accepted_attempt_count == 1
    assert store.get_lead(lead.campaign_id, lead.lead_id).stage == "retry"


def test_recovery_racing_with_webhook_keeps_attempt_and_result_once(tmp_path):
    provider = FakeProvider()
    store, lead, call, client, recovery, _ = setup(tmp_path, accepted=False, provider=provider)
    outcome = result_payload(call, lead, status="busy")
    provider.by_request[call.request_id] = Response(200, provider_record(call, state="ended", result=outcome))
    provider.on_request_lookup = lambda: signed_webhook(client, outcome)

    recovered = recovery.recover_call(call.request_id)

    saved_call = store.get_call(call.request_id)
    saved_lead = store.get_lead(lead.campaign_id, lead.lead_id)
    assert recovered.state == "result_applied"
    assert recovered.result_applied is False  # webhook won the race
    assert saved_call.result_applied and saved_call.accepted
    assert saved_lead.accepted_attempt_count == 1
    assert saved_lead.stage == "retry"
