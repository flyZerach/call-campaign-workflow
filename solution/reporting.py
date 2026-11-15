"""Final-state CSV generation for the Assignment B fixture leads."""
from __future__ import annotations

import csv
import os
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Iterable
from zoneinfo import ZoneInfo

from solution.scheduler import FINAL_STATES
from solution.store import CallRecord, SQLiteStore

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_FIXTURES = ROOT / "assignment-b-workflow" / "fixtures"
DEFAULT_OUTPUT_PATH = ROOT / "final_state.csv"
CSV_COLUMNS = (
    "campaign_id", "lead_id", "final_state", "attempts", "last_status", "last_disposition",
)
IST = ZoneInfo("Asia/Kolkata")


class IncompleteReportError(RuntimeError):
    """Raised when a final report is requested before every fixture lead is final."""


def _fixture_keys(fixtures_dir: str | Path) -> list[tuple[str, str]]:
    import json

    fixture_dir = Path(fixtures_dir)
    config = json.loads((fixture_dir / "campaigns.json").read_text(encoding="utf-8"))
    keys = []
    for campaign in config["campaigns"]:
        path = fixture_dir / campaign["leads_file"]
        with path.open(newline="", encoding="utf-8") as stream:
            keys.extend(
                (campaign["campaign_id"], row["lead_id"])
                for row in csv.DictReader(stream)
            )
    return sorted(keys)


def _instant_key(value: datetime | str | None) -> float:
    if value is None:
        return float("-inf")
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if value.tzinfo is None:
        value = value.replace(tzinfo=IST)
    return value.timestamp()


def _latest_ended(calls: Iterable[CallRecord]) -> CallRecord | None:
    ended = [call for call in calls if call.ended_at is not None]
    if not ended:
        return None
    return max(ended, key=lambda call: (_instant_key(call.ended_at), call.request_id))


def write_final_state_csv(
    store: SQLiteStore,
    *,
    fixtures_dir: str | Path = DEFAULT_FIXTURES,
    output_path: str | Path = DEFAULT_OUTPUT_PATH,
) -> Path:
    """Atomically write the required CSV after validating every fixture lead is final."""
    keys = _fixture_keys(fixtures_dir)
    leads = {(lead.campaign_id, lead.lead_id): lead for lead in store.list_leads()}
    missing = [key for key in keys if key not in leads]
    if missing:
        raise IncompleteReportError(f"fixture leads missing from SQLite: {missing[:5]!r}")

    incomplete = [key for key in keys if leads[key].stage not in FINAL_STATES]
    if incomplete:
        raise IncompleteReportError(
            f"cannot write final_state.csv; {len(incomplete)} fixture leads are not final "
            f"(examples: {incomplete[:5]!r})"
        )

    calls_by_lead: dict[tuple[str, str], list[CallRecord]] = {}
    for campaign_id, lead_id in keys:
        calls_by_lead[(campaign_id, lead_id)] = store.list_calls(campaign_id, lead_id)

    rows = []
    for key in keys:
        lead = leads[key]
        calls = calls_by_lead[key]
        latest = _latest_ended(calls)
        rows.append({
            "campaign_id": lead.campaign_id,
            "lead_id": lead.lead_id,
            "final_state": lead.stage,
            "attempts": sum(1 for call in calls if call.accepted),
            "last_status": (latest.status or "") if latest else "",
            "last_disposition": (latest.disposition or "") if latest else "",
        })

    destination = Path(output_path).expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    temp_path: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", newline="", encoding="utf-8", dir=destination.parent,
            prefix=f".{destination.name}.", suffix=".tmp", delete=False,
        ) as stream:
            temp_path = stream.name
            writer = csv.DictWriter(stream, fieldnames=CSV_COLUMNS, lineterminator="\n")
            writer.writeheader()
            writer.writerows(rows)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_path, destination)
        temp_path = None
    finally:
        if temp_path is not None:
            try:
                os.unlink(temp_path)
            except FileNotFoundError:
                pass
    return destination
