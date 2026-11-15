import csv
import json
from dataclasses import replace
from pathlib import Path

import pytest

from solution.dispatcher import FixtureDispatcher
from solution.reporting import CSV_COLUMNS, IncompleteReportError, write_final_state_csv
from solution.store import SQLiteStore


def make_fixtures(tmp_path):
    fixtures = tmp_path / "fixtures"
    fixtures.mkdir()
    config = {
        "webhook_secret": "test-secret",
        "regulatory_window": {"start": "09:00", "end": "21:00"},
        "tenants": [{"tenant_id": "tenant-a", "max_concurrent_calls": 2}],
        "campaigns": [
            {"campaign_id": "campaign-z", "tenant_id": "tenant-a", "leads_file": "z.csv",
             "max_attempts": 3, "window": {"start": "09:00", "end": "21:00", "days": ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]}},
            {"campaign_id": "campaign-a", "tenant_id": "tenant-a", "leads_file": "a.csv",
             "max_attempts": 3, "window": {"start": "09:00", "end": "21:00", "days": ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]}},
        ],
    }
    (fixtures / "campaigns.json").write_text(json.dumps(config), encoding="utf-8")
    for filename, lead_ids in (("z.csv", ["z2", "z1"]), ("a.csv", ["a2", "a1"])):
        with (fixtures / filename).open("w", newline="", encoding="utf-8") as stream:
            writer = csv.DictWriter(stream, fieldnames=["lead_id", "phone"])
            writer.writeheader()
            for index, lead_id in enumerate(lead_ids):
                writer.writerow({"lead_id": lead_id, "phone": f"+9190000000{index:02d}"})
    return fixtures


def setup_final_store(tmp_path):
    fixtures = make_fixtures(tmp_path)
    store = SQLiteStore(tmp_path / "report.sqlite3")
    FixtureDispatcher(fixtures, store).startup()

    # A lead with two accepted attempts and a later non-accepted request.
    lead = store.get_lead("campaign-a", "a1")
    store.prepare_call("a1-old", lead.campaign_id, lead.lead_id)
    store.mark_call_accepted("a1-old", "CA-old", "2026-11-12T10:00:00+05:30")
    store.apply_call_result(
        "a1-old", status="busy", disposition=None,
        started_at="2026-11-12T10:00:00+05:30", ended_at="2026-11-12T10:05:00+05:30",
        provider_call_id="CA-old", next_stage="retry",
    )
    store.prepare_call("a1-new", lead.campaign_id, lead.lead_id)
    store.mark_call_accepted("a1-new", "CA-new", "2026-11-12T11:00:00+05:30")
    store.apply_call_result(
        "a1-new", status="completed", disposition="interested",
        started_at="2026-11-12T11:00:00+05:30", ended_at="2026-11-12T11:02:00+05:30",
        provider_call_id="CA-new", next_stage="completed",
    )
    store.prepare_call("a1-rejected", lead.campaign_id, lead.lead_id)
    store.abandon_unaccepted_call("a1-rejected")

    # No ended call: result fields must be emitted as empty strings.
    lead_without_call = store.get_lead("campaign-a", "a2")
    store.save_lead(replace(lead_without_call, stage="dnd_blocked"))
    store.save_lead(replace(store.get_lead("campaign-z", "z1"), stage="exhausted"))
    store.save_lead(replace(store.get_lead("campaign-z", "z2"), stage="completed"))
    return fixtures, store


def test_report_sorts_fixture_leads_and_uses_accepted_count_and_latest_ended_call(tmp_path):
    fixtures, store = setup_final_store(tmp_path)
    output = tmp_path / "final_state.csv"

    written = write_final_state_csv(store, fixtures_dir=fixtures, output_path=output)

    assert written == output.resolve()
    with output.open(newline="", encoding="utf-8") as stream:
        reader = csv.DictReader(stream)
        rows = list(reader)
        assert tuple(reader.fieldnames) == CSV_COLUMNS == (
            "campaign_id", "lead_id", "final_state", "attempts", "last_status", "last_disposition"
        )
    assert [(row["campaign_id"], row["lead_id"]) for row in rows] == [
        ("campaign-a", "a1"), ("campaign-a", "a2"),
        ("campaign-z", "z1"), ("campaign-z", "z2"),
    ]
    assert rows[0] == {
        "campaign_id": "campaign-a", "lead_id": "a1", "final_state": "completed",
        "attempts": "2", "last_status": "completed", "last_disposition": "interested",
    }
    assert rows[1] == {
        "campaign_id": "campaign-a", "lead_id": "a2", "final_state": "dnd_blocked",
        "attempts": "0", "last_status": "", "last_disposition": "",
    }
    assert rows[2]["final_state"] == "exhausted"
    assert rows[3]["final_state"] == "completed"


def test_report_refuses_incomplete_fixture_state_without_writing(tmp_path):
    fixtures = make_fixtures(tmp_path)
    store = SQLiteStore(tmp_path / "report.sqlite3")
    FixtureDispatcher(fixtures, store).startup()
    output = tmp_path / "final_state.csv"
    output.write_text("previous report", encoding="utf-8")

    with pytest.raises(IncompleteReportError, match="not final"):
        write_final_state_csv(store, fixtures_dir=fixtures, output_path=output)

    assert output.read_text(encoding="utf-8") == "previous report"
