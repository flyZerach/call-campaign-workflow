"""Submit persisted call intents and reconcile ambiguous provider responses."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from solution.dispatcher import FixtureDispatcher
from solution.provider import TelephonyProviderClient
from solution.store import CallRecord, SQLiteStore


class ProviderResponseError(RuntimeError):
    """Provider returned an unexpected or rule-violation response."""

    def __init__(self, status_code: int, body: Any):
        self.status_code = status_code
        self.body = body
        super().__init__(f"telephony provider returned HTTP {status_code}: {body!r}")


@dataclass(frozen=True)
class SubmissionResult:
    outcome: str  # accepted | not_accepted | unresolved
    call: CallRecord


class CallRunner:
    """Bridge dispatcher intent to provider transport, without handling results."""

    def __init__(self, dispatcher: FixtureDispatcher, store: SQLiteStore,
                 provider: TelephonyProviderClient, callback_url: str):
        self.dispatcher = dispatcher
        self.store = store
        self.provider = provider
        self.callback_url = callback_url

    def dispatch_next(self, simulated_now: datetime, request_id: str | None = None) -> SubmissionResult | None:
        # prepare_next_call writes the intent and reserves shared guardrails first.
        intent = self.dispatcher.prepare_next_call(simulated_now, request_id=request_id)
        if intent is None:
            return None
        return self.submit_intent(intent.request_id)

    def submit_intent(self, request_id: str) -> SubmissionResult:
        """Send an existing intent using its stable idempotency key."""
        intent = self.store.get_call(request_id)
        if intent is None:
            raise KeyError(f"Unknown request_id {request_id}")
        if intent.accepted:
            return SubmissionResult("accepted", intent)
        if intent.status != "request_pending":
            raise ValueError(f"request {request_id} is not pending ({intent.status})")
        lead = self.store.get_lead(intent.campaign_id, intent.lead_id)
        if lead is None:
            raise KeyError(f"Missing lead for request_id {request_id}")
        response = self.provider.place_call({
            "request_id": intent.request_id,
            "campaign_id": intent.campaign_id,
            "lead_id": intent.lead_id,
            "phone": lead.phone,
            "callback_url": self.callback_url,
        })
        return self._handle_post_response(intent.request_id, response)

    def reconcile(self, request_id: str) -> SubmissionResult:
        """Look up an unresolved request without creating another provider call."""
        intent = self.store.get_call(request_id)
        if intent is None:
            raise KeyError(f"Unknown request_id {request_id}")
        if intent.accepted:
            return SubmissionResult("accepted", intent)
        if intent.status != "request_pending":
            raise ValueError(f"request {request_id} is not pending ({intent.status})")
        response = self.provider.find_by_request_id(request_id)
        if response.status_code == 404:
            return SubmissionResult("unresolved", intent)
        if response.status_code != 200:
            raise ProviderResponseError(response.status_code, self._body(response))
        return self._accept(request_id, response.json())

    def _handle_post_response(self, request_id: str, response) -> SubmissionResult:
        if response.status_code in (200, 201):
            return self._accept(request_id, response.json())
        if response.status_code == 503:
            self.store.abandon_unaccepted_call(request_id)
            self.dispatcher.reload_state()
            return SubmissionResult("not_accepted", self.store.get_call(request_id))
        if response.status_code == 504:
            # A timeout can occur before or after acceptance; only lookup can
            # distinguish those cases safely. A 404 remains unresolved.
            return self.reconcile(request_id)
        if response.status_code in (409, 422, 429):
            self.store.abandon_unaccepted_call(request_id)
            self.dispatcher.reload_state()
            raise ProviderResponseError(response.status_code, self._body(response))
        raise ProviderResponseError(response.status_code, self._body(response))

    def _accept(self, request_id: str, data: dict) -> SubmissionResult:
        provider_call_id = data.get("call_sid")
        if not provider_call_id:
            raise ProviderResponseError(502, {"error": "accepted response missing call_sid", "body": data})
        accepted_at = data.get("created_at")
        if accepted_at is None:
            # Provider simulated time, never this machine's wall clock.
            accepted_at = self.provider.get_clock()["sim_now"]
        call = self.store.mark_call_accepted(request_id, provider_call_id, accepted_at)
        self.dispatcher.reload_state()
        return SubmissionResult("accepted", call)

    @staticmethod
    def _body(response) -> Any:
        try:
            return response.json()
        except Exception:
            return getattr(response, "text", "")
