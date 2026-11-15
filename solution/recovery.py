"""Restart recovery for saved intents and accepted calls; no dispatch loop."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from solution.provider import TelephonyProviderClient
from solution.result_application import apply_provider_result
from solution.store import CallRecord, SQLiteStore


class ProviderLookupError(RuntimeError):
    """A provider lookup failed with an unexpected HTTP response."""

    def __init__(self, status_code: int, body: Any):
        self.status_code = status_code
        self.body = body
        super().__init__(f"provider lookup returned HTTP {status_code}: {body!r}")


@dataclass(frozen=True)
class RecoveryResult:
    request_id: str
    state: str  # unresolved | active | accepted | result_applied | not_found
    result_applied: bool = False


class CallRecovery:
    """Reconcile database calls with provider state using their saved identifiers."""

    def __init__(self, store: SQLiteStore, provider: TelephonyProviderClient,
                 campaigns: Mapping[str, Mapping[str, Any]]):
        self.store = store
        self.provider = provider
        self.campaigns = campaigns

    def recover_all(self) -> list[RecoveryResult]:
        """Reconcile every pending or accepted call that has no saved result."""
        candidates = [
            call for call in self.store.list_calls()
            if not call.result_applied and (call.status == "request_pending" or call.accepted)
        ]
        return [self.recover_call(call.request_id) for call in candidates]

    def recover_call(self, request_id: str) -> RecoveryResult:
        call = self.store.get_call(request_id)
        if call is None:
            raise KeyError(f"Unknown request_id {request_id}")
        if call.result_applied:
            return RecoveryResult(request_id, "result_applied", result_applied=False)
        if call.status != "request_pending" and not call.accepted:
            return RecoveryResult(request_id, "not_found")

        response = (
            self.provider.find_by_request_id(call.request_id)
            if call.status == "request_pending" or not call.provider_call_id
            else self.provider.find_by_call_id(call.provider_call_id)
        )
        if response.status_code == 404:
            # A pending request may have reached the provider despite a lost
            # response. Keep its reservation and request ID intact.
            return RecoveryResult(request_id, "unresolved" if call.status == "request_pending" else "not_found")
        if response.status_code != 200:
            raise ProviderLookupError(response.status_code, self._body(response))
        data = response.json()
        provider_call_id = data.get("call_sid")
        if not provider_call_id:
            raise ProviderLookupError(502, {"error": "provider lookup omitted call_sid", "body": data})

        if not call.accepted:
            call = self.store.mark_call_accepted(
                call.request_id, provider_call_id, data.get("created_at")
            )

        if data.get("state") != "ended":
            return RecoveryResult(request_id, "active")
        result = data.get("result")
        if not isinstance(result, dict):
            # The provider says ended but has not exposed the result yet. Keep
            # the accepted call persisted and retry lookup on the next pass.
            return RecoveryResult(request_id, "accepted")

        applied = apply_provider_result(self.store, result, self.campaigns)
        return RecoveryResult(request_id, "result_applied", result_applied=applied)

    @staticmethod
    def _body(response) -> Any:
        try:
            return response.json()
        except Exception:
            return getattr(response, "text", "")
