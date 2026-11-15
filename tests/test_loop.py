import csv
import json
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from solution.call_runner import ProviderResponseError
from solution.dispatcher import FixtureDispatcher
from solution.loop import build_loop
from solution.store import SQLiteStore

IST = ZoneInfo("Asia/Kolkata")
START = datetime(2026, 11, 12, 12, 0, tzinfo=IST)


class FakeTime:
    def __init__(self):
        self.elapsed = 0.0

    def monotonic(self):
        return self.elapsed

    def sleep(self, seconds):
        assert seconds > 0
        self.elapsed += seconds


class FakeResponse:
    def __init__(self, status, body):
        self.status_code = status
        self._body = body

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeHTTPProvider:
    def __init__(self, runtime, *, speed=600, call_duration=0.5, post_statuses=(),
                 advance_after_post_sim_seconds=0.0):
        self.runtime = runtime
        self.speed = speed
        self.sim_time_offset_seconds = 0.0
        self.call_duration = call_duration
        self.post_statuses = list(post_statuses)
        self.advance_after_post_sim_seconds = advance_after_post_sim_seconds
        self.calls = {}
        self.post_payloads = []
        self.lookups = []
        self.clock_reads = 0

    def now(self):
        return START + timedelta(seconds=self.runtime.elapsed * self.speed + self.sim_time_offset_seconds)

    def post(self, url, *, json):
        self.post_payloads.append((url, json))
        status = self.post_statuses.pop(0) if self.post_statuses else 201
        if status != 201:
            return FakeResponse(status, {"error": "rule_violation"})
        call_sid = f"CA-{len(self.calls) + 1}"
        call = {
            "call_sid": call_sid, "request_id": json["request_id"],
            "campaign_id": json["campaign_id"], "lead_id": json["lead_id"],
            "phone": json["phone"], "callback_url": json["callback_url"],
            "created_at": self.now().isoformat(), "created_real": self.runtime.elapsed,
        }
        self.calls[call_sid] = call
        self.runtime.elapsed += self.advance_after_post_sim_seconds / self.speed
        return FakeResponse(201, {key: call[key] for key in ("call_sid", "request_id", "created_at")})

    def get(self, url, *, params=None):
        if url.endswith("/v1/clock"):
            self.clock_reads += 1
            return FakeResponse(200, {"sim_now": self.now().isoformat(), "speed": self.speed})
        if params and "request_id" in params:
            self.lookups.append(("request", params["request_id"]))
            call = next((c for c in self.calls.values() if c["request_id"] == params["request_id"]), None)
            return FakeResponse(200, self.public(call)) if call else FakeResponse(404, {"error": "not_found"})
        call_sid = url.rsplit("/", 1)[-1]
        self.lookups.append(("call", call_sid))
        call = self.calls.get(call_sid)
        return FakeResponse(200, self.public(call)) if call else FakeResponse(404, {"error": "not_found"})

    def public(self, call):
        elapsed = self.runtime.elapsed - call["created_real"]
        if elapsed < self.call_duration:
            return {**{k: call[k] for k in ("call_sid", "request_id", "campaign_id", "lead_id", "created_at")}, "state": "active"}
        now = self.now().isoformat()
        result = {
            "event": "call.completed", "call_sid": call["call_sid"],
            "request_id": call["request_id"], "tenant_id": "tenant-a",
            "campaign_id": call["campaign_id"], "lead_id": call["lead_id"],
            "phone": call["phone"], "status": "completed",
            "disposition": "interested", "callback_at": None,
            "started_at": call["created_at"], "ended_at": now, "duration_sec": 1,
        }
        return {
            **{k: call[k] for k in ("call_sid", "request_id", "campaign_id", "lead_id", "created_at")},
            "state": "ended", "result": result,
        }


def make_fixtures(tmp_path, *, count=2, start="09:00", end="21:00", concurrency=2):
    fixtures = tmp_path / "fixtures"
    fixtures.mkdir()
    config = {
        "sim_start": START.isoformat(), "timezone": "Asia/Kolkata",
        "regulatory_window": {"start": "09:00", "end": "21:00"},
        "window_grace_seconds": 120, "retry_policy": {
            "no_answer_minutes": 60, "busy_minutes": 60, "failed_minutes": 15,
        },
        "webhook_secret": "test-secret",
        "tenants": [{"tenant_id": "tenant-a", "max_concurrent_calls": concurrency}],
        "campaigns": [{
            "campaign_id": "campaign-a", "tenant_id": "tenant-a",
            "leads_file": "leads.csv", "max_attempts": 3,
            "window": {"start": start, "end": end, "days": ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]},
        }],
    }
    (fixtures / "campaigns.json").write_text(json.dumps(config), encoding="utf-8")
    with (fixtures / "leads.csv").open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=["lead_id", "phone"])
        writer.writeheader()
        for i in range(count):
            writer.writerow({"lead_id": f"lead-{i+1}", "phone": f"+91900000000{i+1}"})
    return fixtures


def build_test_loop(tmp_path, *, count=2, start="09:00", end="21:00", concurrency=2,
                    call_duration=0.5, post_statuses=(), recovery_interval=1.0):
    fixtures = make_fixtures(tmp_path, count=count, start=start, end=end, concurrency=concurrency)
    runtime = FakeTime()
    provider = FakeHTTPProvider(runtime, call_duration=call_duration, post_statuses=post_statuses)
    loop = build_loop(
        database_path=tmp_path / "dispatcher.sqlite3", fixtures_dir=fixtures,
        callback_url="http://dispatcher.test:8200/webhooks/telephony",
        output_path=tmp_path / "final_state.csv",
        http_client=provider, monotonic=runtime.monotonic, sleep=runtime.sleep,
        recovery_interval=recovery_interval, initial_503_backoff=2.0, max_503_backoff=8.0,
    )
    return loop, provider, runtime


def test_loop_completes_campaign_using_provider_clock_and_recovery(tmp_path):
    loop, provider, runtime = build_test_loop(tmp_path)

    assert loop.run(max_steps=8) is True

    leads = loop.dispatcher.store.list_leads()
    assert len(leads) == 2
    assert all(lead.stage == "completed" for lead in leads)
    assert all(lead.accepted_attempt_count == 1 for lead in leads)
    assert len(provider.post_payloads) == 2
    assert all(body["callback_url"].endswith("/webhooks/telephony") for _, body in provider.post_payloads)
    assert any(kind == "call" for kind, _ in provider.lookups)
    assert provider.clock_reads >= len(provider.post_payloads) + 1
    assert runtime.elapsed >= 0.5
    assert (tmp_path / "final_state.csv").exists()


def test_loop_refreshes_provider_clock_before_each_call_in_a_batch(tmp_path):
    loop, provider, runtime = build_test_loop(
        tmp_path, count=2, start="09:00", end="12:00", call_duration=100.0
    )
    # First call is eligible at 11:59. It advances simulated time to 12:00,
    # when the campaign window has closed, so the second lead must wait.
    provider.sim_time_offset_seconds = -60
    provider.advance_after_post_sim_seconds = 60

    step = loop.step()

    assert step.calls_submitted == 1
    assert len(provider.post_payloads) == 1
    assert provider.clock_reads >= 3  # initial snapshot plus one per selection
    stages = {lead.lead_id: lead.stage for lead in loop.dispatcher.store.list_leads()}
    assert sorted(stages.values()) == ["active", "fresh"]


def test_loop_waits_and_does_not_redial_active_calls(tmp_path):
    loop, provider, runtime = build_test_loop(tmp_path, call_duration=100.0)

    first = loop.step()
    assert first.calls_submitted == 2
    assert first.wait_seconds == 1.0
    runtime.sleep(first.wait_seconds)
    second = loop.step()

    assert second.calls_submitted == 0
    assert second.wait_seconds > 0
    assert len(provider.post_payloads) == 2
    assert all(lead.stage == "active" for lead in loop.dispatcher.store.list_leads())


def test_wait_duration_uses_simulated_clock_speed_for_next_window(tmp_path):
    loop, provider, _ = build_test_loop(
        tmp_path, count=1, start="13:00", end="21:00", recovery_interval=60.0
    )
    provider.runtime.elapsed = -3600 / provider.speed  # provider time is 11:00 IST

    step = loop.step()

    assert step.calls_submitted == 0
    assert step.wait_seconds == pytest.approx(6.0)  # one simulated hour at 600x
    assert provider.post_payloads == []


def test_503_uses_backoff_before_making_a_new_intent(tmp_path):
    loop, provider, runtime = build_test_loop(
        tmp_path, count=1, post_statuses=[503, 201], recovery_interval=10.0
    )

    first = loop.step()
    assert first.calls_submitted == 1
    assert first.wait_seconds == 2.0
    assert loop.dispatcher.store.list_calls()[0].status == "not_accepted"
    runtime.sleep(first.wait_seconds)
    second = loop.step()

    assert second.calls_submitted == 1
    assert len(provider.post_payloads) == 2
    assert provider.post_payloads[0][1]["request_id"] != provider.post_payloads[1][1]["request_id"]
    assert loop.dispatcher.store.list_leads()[0].accepted_attempt_count == 1


def test_rule_violation_is_raised_clearly_and_not_retried(tmp_path):
    loop, provider, _ = build_test_loop(tmp_path, count=1, post_statuses=[422])

    with pytest.raises(ProviderResponseError) as error:
        loop.step()

    assert error.value.status_code == 422
    assert len(provider.post_payloads) == 1
    assert loop.dispatcher.store.list_calls()[0].status == "not_accepted"


def test_startup_reconciles_saved_pending_intent_before_dispatch(tmp_path):
    fixtures = make_fixtures(tmp_path, count=1)
    database = tmp_path / "dispatcher.sqlite3"
    store = SQLiteStore(database)
    FixtureDispatcher(fixtures, store).startup()
    lead = store.list_leads()[0]
    saved = store.prepare_call("persisted-request", lead.campaign_id, lead.lead_id)

    runtime = FakeTime()
    provider = FakeHTTPProvider(runtime, call_duration=100.0)
    provider.calls["CA-prior"] = {
        "call_sid": "CA-prior", "request_id": saved.request_id,
        "campaign_id": saved.campaign_id, "lead_id": saved.lead_id,
        "phone": lead.phone, "callback_url": "http://dispatcher.test/webhooks/telephony",
        "created_at": START.isoformat(), "created_real": 0.0,
    }
    loop = build_loop(
        database_path=database, fixtures_dir=fixtures,
        callback_url="http://dispatcher.test:8200/webhooks/telephony",
        http_client=provider, monotonic=runtime.monotonic, sleep=runtime.sleep,
        recovery_interval=1.0,
    )

    step = loop.step()

    assert step.calls_submitted == 0
    saved_after = store.get_call("persisted-request")
    assert saved_after.accepted is True
    assert saved_after.provider_call_id == "CA-prior"
    assert store.get_lead(lead.campaign_id, lead.lead_id).accepted_attempt_count == 1
    assert provider.post_payloads == []
    assert provider.lookups == [("request", "persisted-request")]
