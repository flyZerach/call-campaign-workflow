"""Signed provider webhook endpoint, separate from scheduling and HTTP transport."""
from __future__ import annotations

import hashlib
import hmac
import json
import os
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request

from solution.result_application import ResultApplicationError, apply_provider_result
from solution.store import SQLiteStore

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_FIXTURES = ROOT / "assignment-b-workflow" / "fixtures"


def create_app(store: SQLiteStore, fixtures_dir: str | Path = DEFAULT_FIXTURES) -> FastAPI:
    """Build the webhook app with an existing database and campaign fixtures."""
    fixture_dir = Path(fixtures_dir)
    config = json.loads((fixture_dir / "campaigns.json").read_text(encoding="utf-8"))
    secret = config["webhook_secret"].encode("utf-8")
    campaigns = {item["campaign_id"]: item for item in config["campaigns"]}
    app = FastAPI(title="Assignment B telephony webhooks")
    app.state.store = store

    @app.post("/webhooks/telephony")
    async def receive_telephony_webhook(request: Request) -> dict[str, Any]:
        raw_body = await request.body()
        supplied = request.headers.get("X-AU-Signature", "")
        expected = hmac.new(secret, raw_body, hashlib.sha256).hexdigest()
        if not supplied or not hmac.compare_digest(supplied, expected):
            raise HTTPException(status_code=401, detail="invalid webhook signature")

        try:
            payload = json.loads(raw_body)
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise HTTPException(status_code=400, detail="invalid JSON body")
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="webhook body must be a JSON object")
        required = ("request_id", "campaign_id", "lead_id", "call_sid", "status", "ended_at")
        missing = [name for name in required if not payload.get(name)]
        if missing:
            raise HTTPException(status_code=422, detail={"missing_fields": missing})

        try:
            applied = apply_provider_result(store, payload, campaigns)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ResultApplicationError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

        return {"accepted": True, "duplicate": not applied, "request_id": str(payload["request_id"])}

    return app


def main() -> None:
    """Run locally with DATABASE_PATH optionally set in the environment."""
    import uvicorn

    from solution.dispatcher import FixtureDispatcher

    database = os.environ.get("DATABASE_PATH", str(ROOT / "assignment-b-workflow" / "dispatcher.sqlite3"))
    store = SQLiteStore(database)
    # Register campaign tenant limits and seed missing fixture leads. Existing
    # persisted progress is preserved by FixtureDispatcher.startup().
    FixtureDispatcher(DEFAULT_FIXTURES, store).startup()
    uvicorn.run(create_app(store), host="0.0.0.0", port=8200)


if __name__ == "__main__":
    main()
