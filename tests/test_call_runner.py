from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from solution.call_runner import CallRunner, ProviderResponseError
from solution.dispatcher import FixtureDispatcher
from solution.provider import TelephonyProviderClient
from solution.store import SQLiteStore

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "assignment-b-workflow" / "fixtures"
NOW = datetime(2026, 11, 12, 12, 0, tzinfo=ZoneInfo("Asia/Kolkata"))
CALLBACK = "http://127.0.0.1:8200/webhooks/telephony"


class Response:
    def __init__(self, status, body):
        self.status_code = status
        self._body = body
        self.text = str(body)

    def json(self):
        return self._body


class FakeHTTP:
    def __init__(self, posts=(), gets=()):
        self.posts = list(posts)
        self.gets = list(gets)
        self.post_payloads = []
        self.get_calls = []

    def post(self, url, *, json):
        self.post_payloads.append((url, json))
        return self.posts.pop(0)

    def get(self, url, *, params=None):
        self.get_calls.append((url, params))
        return self.gets.pop(0)


def setup(tmp_path, fake):
    store = SQLiteStore(tmp_path / "calls.sqlite3")
    dispatcher = FixtureDispatcher(FIXTURES, store)
    dispatcher.startup()
    client = TelephonyProviderClient(http_client=fake)
    return store, dispatcher, CallRunner(dispatcher, store, client, CALLBACK)


def test_201_persists_provider_id_time_and_counts_attempt_once(tmp_path):
    fake = FakeHTTP(posts=[Response(201, {
        "call_sid": "CA-201", "request_id": "intent-201", "created_at": "2026-11-12T12:00:01+05:30"
    })])
    store, dispatcher, runner = setup(tmp_path, fake)

    result = runner.dispatch_next(NOW, request_id="intent-201")

    assert result.outcome == "accepted"
    assert result.call.provider_call_id == "CA-201"
    assert result.call.started_at.isoformat() == "2026-11-12T12:00:01+05:30"
    assert store.get_lead(result.call.campaign_id, result.call.lead_id).accepted_attempt_count == 1
    url, body = fake.post_payloads[0]
    assert url.endswith("/v1/calls")
    assert body == {
        "request_id": "intent-201", "campaign_id": result.call.campaign_id,
        "lead_id": result.call.lead_id,
        "phone": store.get_lead(result.call.campaign_id, result.call.lead_id).phone,
        "callback_url": CALLBACK,
    }


def test_idempotent_200_is_accepted_and_attempt_counted_once(tmp_path):
    fake = FakeHTTP(posts=[Response(200, {
        "call_sid": "CA-existing", "request_id": "intent-200", "created_at": "2026-11-12T12:00:02+05:30"
    })])
    store, _, runner = setup(tmp_path, fake)

    first = runner.dispatch_next(NOW, request_id="intent-200")
    # Reprocessing an acceptance does not send a second request or count twice.
    second = runner.submit_intent("intent-200")

    assert first.outcome == second.outcome == "accepted"
    assert len(fake.post_payloads) == 1
    assert store.get_lead(first.call.campaign_id, first.call.lead_id).accepted_attempt_count == 1


def test_503_releases_reservation_without_counting_attempt(tmp_path):
    fake = FakeHTTP(posts=[Response(503, {"error": "service_unavailable"})])
    store, _, runner = setup(tmp_path, fake)

    result = runner.dispatch_next(NOW, request_id="intent-503")

    assert result.outcome == "not_accepted"
    assert result.call.status == "not_accepted"
    lead = store.get_lead(result.call.campaign_id, result.call.lead_id)
    assert lead.accepted_attempt_count == 0
    assert lead.active_request_id is None
    assert lead.stage == "fresh"


def test_504_found_by_same_request_id_is_accepted(tmp_path):
    fake = FakeHTTP(
        posts=[Response(504, {"error": "upstream_timeout"})],
        gets=[Response(200, {
            "call_sid": "CA-reconciled", "request_id": "intent-504",
            "created_at": "2026-11-12T12:00:03+05:30",
        })],
    )
    store, _, runner = setup(tmp_path, fake)

    result = runner.dispatch_next(NOW, request_id="intent-504")

    assert result.outcome == "accepted"
    assert result.call.provider_call_id == "CA-reconciled"
    assert store.get_lead(result.call.campaign_id, result.call.lead_id).accepted_attempt_count == 1
    assert fake.get_calls[0][1] == {"request_id": "intent-504"}


def test_504_not_found_stays_reserved_and_reconciliation_does_not_post(tmp_path):
    fake = FakeHTTP(
        posts=[Response(504, {"error": "upstream_timeout"})],
        gets=[Response(404, {"error": "not_found"}), Response(404, {"error": "not_found"})],
    )
    store, _, runner = setup(tmp_path, fake)

    result = runner.dispatch_next(NOW, request_id="intent-unknown")
    again = runner.reconcile("intent-unknown")

    assert result.outcome == again.outcome == "unresolved"
    lead = store.get_lead(result.call.campaign_id, result.call.lead_id)
    assert lead.active_request_id == "intent-unknown"
    assert lead.accepted_attempt_count == 0
    assert len(fake.post_payloads) == 1
    assert all(params == {"request_id": "intent-unknown"} for _, params in fake.get_calls)


def test_retrying_same_pending_intent_reuses_request_id(tmp_path):
    fake = FakeHTTP(posts=[
        Response(504, {"error": "upstream_timeout"}),
        Response(200, {"call_sid": "CA-same", "created_at": "2026-11-12T12:00:04+05:30"}),
    ], gets=[Response(404, {"error": "not_found"})])
    store, _, runner = setup(tmp_path, fake)

    runner.dispatch_next(NOW, request_id="stable-intent")
    accepted = runner.submit_intent("stable-intent")

    assert accepted.outcome == "accepted"
    assert [body["request_id"] for _, body in fake.post_payloads] == ["stable-intent", "stable-intent"]
    assert store.get_lead(accepted.call.campaign_id, accepted.call.lead_id).accepted_attempt_count == 1


@pytest.mark.parametrize("status", [409, 422, 429])
def test_rule_violation_responses_are_visible_errors_and_release_reservation(tmp_path, status):
    fake = FakeHTTP(posts=[Response(status, {"error": "rule_violation"})])
    store, _, runner = setup(tmp_path, fake)

    with pytest.raises(ProviderResponseError) as error:
        runner.dispatch_next(NOW, request_id=f"intent-{status}")

    assert error.value.status_code == status
    call = store.get_call(f"intent-{status}")
    assert call.status == "not_accepted"
    lead = store.get_lead(call.campaign_id, call.lead_id)
    assert lead.active_request_id is None
    assert lead.accepted_attempt_count == 0
