"""Fixture-to-store and scheduling integration, with no provider client."""
from __future__ import annotations

import csv
import json
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

from solution.scheduler import Campaign, LeadState, next_eligible_at as scheduler_next_eligible_at, priority_order
from solution.store import CallRecord, LeadRecord, ReservationRejected, SQLiteStore

IST = ZoneInfo("Asia/Kolkata")


class FixtureDispatcher:
    """Load fixtures, preserve stored progress, and select/persist call intent.

    This integration seam loads fixtures, selects eligible leads, and persists
    intents. Provider transport and webhook handling live in separate modules. SQLiteStore enforces shared tenant
    concurrency, active-phone exclusivity, and global DND during reservation.
    """

    def __init__(self, fixture_dir: str | Path, store: SQLiteStore):
        self.fixture_dir = Path(fixture_dir)
        self.store = store
        self.campaigns: dict[str, Campaign] = {}
        self.leads: dict[tuple[str, str], LeadRecord] = {}
        self.calls: list[CallRecord] = []
        self._calls_by_lead: dict[tuple[str, str], list[CallRecord]] = defaultdict(list)
        self._fixtures_loaded = False

    def startup(self) -> int:
        """Load fixture config, seed missing leads, then reload persisted state.

        Returns the number of fixture lead rows inserted. Repeated startup does
        not reset leads already present in the database.
        """
        config = json.loads((self.fixture_dir / "campaigns.json").read_text(encoding="utf-8"))
        regulatory = config.get("regulatory_window", {"start": "09:00", "end": "21:00"})
        tenant_limits = {tenant["tenant_id"]: tenant["max_concurrent_calls"] for tenant in config["tenants"]}
        self.campaigns = {
            item["campaign_id"]: Campaign.from_mapping(
                {**item, "regulatory_window": regulatory}
            )
            for item in config["campaigns"]
        }

        inserted = 0
        for item in config["campaigns"]:
            self.store.register_campaign(
                item["campaign_id"], item["tenant_id"], tenant_limits[item["tenant_id"]]
            )
            fixture_path = self.fixture_dir / item["leads_file"]
            with fixture_path.open(newline="", encoding="utf-8") as stream:
                for row in csv.DictReader(stream):
                    lead = LeadRecord(
                        campaign_id=item["campaign_id"],
                        lead_id=row["lead_id"],
                        phone=row["phone"],
                    )
                    inserted += int(self.store.insert_lead_if_missing(lead))

        self._fixtures_loaded = True
        self.reload_state()
        return inserted

    def reload_state(self) -> None:
        """Refresh in-memory records from SQLite after startup or a state change."""
        self.leads = {
            (lead.campaign_id, lead.lead_id): lead
            for lead in self.store.list_leads()
        }
        self.calls = self.store.list_calls()
        grouped: dict[tuple[str, str], list[CallRecord]] = defaultdict(list)
        for call in self.calls:
            grouped[(call.campaign_id, call.lead_id)].append(call)
        self._calls_by_lead = grouped

    @staticmethod
    def _instant_key(value: datetime) -> float:
        if value.tzinfo is None:
            value = value.replace(tzinfo=IST)
        return value.timestamp()

    def ordered_eligible(self, simulated_now: datetime) -> list[tuple[Campaign, LeadRecord]]:
        """Return eligible fixture/database leads in scheduler priority order."""
        if not self._fixtures_loaded:
            raise RuntimeError("call startup() before scheduling")

        scheduler_inputs = []
        for lead in self.leads.values():
            campaign = self.campaigns[lead.campaign_id]
            ended = [
                call.ended_at
                for call in self._calls_by_lead.get((lead.campaign_id, lead.lead_id), [])
                if call.ended_at is not None
            ]
            latest_ended_at = max(ended, key=self._instant_key) if ended else None
            scheduler_inputs.append((
                campaign,
                LeadState(
                    lead_id=lead.lead_id,
                    stage=lead.stage,
                    attempts=lead.accepted_attempt_count,
                    last_status=lead.last_status,
                    ended_at=latest_ended_at,
                    callback_at=lead.callback_at,
                ),
            ))

        ordered = priority_order(scheduler_inputs, simulated_now)
        return [
            (campaign, self.leads[(campaign.campaign_id, state.lead_id)])
            for campaign, state in ordered
        ]

    def next_eligible_at(self, simulated_now: datetime) -> datetime | None:
        """Return the earliest future scheduler eligibility across campaigns."""
        if not self._fixtures_loaded:
            raise RuntimeError("call startup() before scheduling")
        due_times = []
        for lead in self.leads.values():
            campaign = self.campaigns[lead.campaign_id]
            ended = [
                call.ended_at
                for call in self._calls_by_lead.get((lead.campaign_id, lead.lead_id), [])
                if call.ended_at is not None
            ]
            latest_ended_at = max(ended, key=self._instant_key) if ended else None
            state = LeadState(
                lead_id=lead.lead_id, stage=lead.stage,
                attempts=lead.accepted_attempt_count, last_status=lead.last_status,
                ended_at=latest_ended_at, callback_at=lead.callback_at,
            )
            due = scheduler_next_eligible_at(campaign, state, simulated_now)
            if due is not None:
                due_times.append(due)
        return min(due_times, key=self._instant_key) if due_times else None

    def prepare_next_call(
        self, simulated_now: datetime, request_id: str | None = None
    ) -> CallRecord | None:
        """Persist intent for the highest-priority eligible lead; make no API call."""
        ordered = self.ordered_eligible(simulated_now)
        if not ordered:
            return None
        for campaign, lead in ordered:
            candidate_request_id = request_id or f"req_{uuid4().hex}"
            try:
                call = self.store.prepare_call(
                    candidate_request_id, campaign.campaign_id, lead.lead_id
                )
            except ReservationRejected:
                # A shared limit or phone state may have changed after the
                # scheduler snapshot. Keep the scheduler's order and try next.
                continue
            self.reload_state()
            return call
        return None
