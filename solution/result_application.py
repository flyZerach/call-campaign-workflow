"""Shared reducer-to-store path for trusted provider and signed webhook results."""
from __future__ import annotations

from typing import Any, Mapping

from solution.reducer import CallResult, reduce_call_result
from solution.store import SQLiteStore


class ResultApplicationError(ValueError):
    """A provider result conflicts with the persisted call or lead state."""


def apply_provider_result(
    store: SQLiteStore, payload: Mapping[str, Any], campaigns: Mapping[str, Mapping[str, Any]]
) -> bool:
    """Reduce and persist one result. Returns False when that result was already applied."""
    for name in ("request_id", "campaign_id", "lead_id", "call_sid", "status", "ended_at"):
        if not payload.get(name):
            raise ResultApplicationError(f"provider result is missing {name}")

    request_id = str(payload["request_id"])
    call = store.get_call(request_id)
    if call is None:
        raise KeyError(f"Unknown request_id {request_id}")
    if (call.campaign_id, call.lead_id) != (payload["campaign_id"], payload["lead_id"]):
        raise ResultApplicationError("provider result identifiers do not match saved call")
    if call.provider_call_id not in (None, payload["call_sid"]):
        raise ResultApplicationError("provider result call_sid does not match saved call")

    if call.result_applied:
        # Reuse the stage chosen by the first application. Other lead events
        # (for example, a later global DND) must not make a duplicate result
        # look like a conflicting transition. The store verifies result fields.
        next_stage = call.result_stage
        if next_stage is None:
            raise ResultApplicationError("saved call result has no result stage")
    else:
        campaign = campaigns.get(call.campaign_id)
        if campaign is None:
            raise ResultApplicationError("campaign missing from fixture configuration")
        lead = store.get_lead(call.campaign_id, call.lead_id)
        if lead is None:
            raise KeyError(f"Missing lead for request_id {request_id}")
        result = CallResult(
            status=str(payload["status"]),
            disposition=payload.get("disposition"),
            ended_at=payload["ended_at"],
            callback_at=payload.get("callback_at"),
        )
        attempts_including_this_call = lead.accepted_attempt_count + int(not call.accepted)
        transition = reduce_call_result(
            lead.stage, attempts_including_this_call, result,
            max_attempts=int(campaign.get("max_attempts", 3)),
        )
        next_stage = transition.stage

    return store.apply_call_result(
        request_id,
        status=str(payload["status"]),
        disposition=payload.get("disposition"),
        ended_at=payload["ended_at"],
        callback_at=payload.get("callback_at"),
        provider_call_id=str(payload["call_sid"]),
        started_at=payload.get("started_at") or call.started_at,
        next_stage=next_stage,
    )
