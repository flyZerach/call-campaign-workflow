"""Durable campaign dispatch loop with final-state reporting on completion."""
from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable

from solution.call_runner import CallRunner, ProviderResponseError
from solution.dispatcher import FixtureDispatcher
from solution.recovery import CallRecovery
from solution.reporting import DEFAULT_OUTPUT_PATH, write_final_state_csv
from solution.scheduler import FINAL_STATES
from solution.store import SQLiteStore

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_FIXTURES = ROOT / "assignment-b-workflow" / "fixtures"
LOGGER = logging.getLogger("assignment_b.dispatcher")


@dataclass(frozen=True)
class LoopStep:
    simulated_now: datetime
    complete: bool
    calls_submitted: int
    wait_seconds: float


class DispatcherLoop:
    """Coordinate existing modules while keeping time anchored to provider clock."""

    def __init__(
        self, dispatcher: FixtureDispatcher, call_runner: CallRunner,
        recovery: CallRecovery, provider, *,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        recovery_interval: float = 0.25,
        initial_503_backoff: float = 1.0,
        max_503_backoff: float = 30.0,
        report_path: str | Path = DEFAULT_OUTPUT_PATH,
    ):
        if recovery_interval <= 0 or initial_503_backoff <= 0 or max_503_backoff <= 0:
            raise ValueError("loop intervals and backoff values must be positive")
        self.dispatcher = dispatcher
        self.call_runner = call_runner
        self.recovery = recovery
        self.provider = provider
        self.monotonic = monotonic
        self.sleep = sleep
        self.recovery_interval = recovery_interval
        self.initial_503_backoff = initial_503_backoff
        self.max_503_backoff = max_503_backoff
        self.report_path = Path(report_path)
        self._report_written = False
        self._initialized = False
        self._last_recovery = 0.0
        self._retry_not_before = 0.0
        self._current_503_backoff = initial_503_backoff

    def initialize(self) -> None:
        """Seed missing fixture leads, reload state, and reconcile before dispatch."""
        self.dispatcher.startup()
        self.recovery.recover_all()
        self.dispatcher.reload_state()
        self._last_recovery = self.monotonic()
        self._initialized = True

    def step(self) -> LoopStep:
        """Run recovery/dispatch once and return a bounded delay before the next pass."""
        if not self._initialized:
            self.initialize()

        clock = self.provider.get_clock()
        simulated_now = datetime.fromisoformat(clock["sim_now"].replace("Z", "+00:00"))
        speed = float(clock.get("speed", 1.0))
        if speed <= 0:
            raise ValueError("provider clock speed must be positive")

        current_monotonic = self.monotonic()
        if current_monotonic - self._last_recovery >= self.recovery_interval:
            self.recovery.recover_all()
            self._last_recovery = self.monotonic()
        self.dispatcher.reload_state()

        if self._all_leads_final():
            self._write_report()
            return LoopStep(simulated_now, True, 0, 0.0)

        calls_submitted = 0
        if current_monotonic >= self._retry_not_before:
            while True:
                # A batch can span meaningful simulated time when the provider
                # clock runs faster than wall time. Refresh before selecting
                # each call so scheduling uses the current calling windows.
                clock = self.provider.get_clock()
                simulated_now = datetime.fromisoformat(
                    clock["sim_now"].replace("Z", "+00:00")
                )
                speed = float(clock.get("speed", 1.0))
                if speed <= 0:
                    raise ValueError("provider clock speed must be positive")
                submission = self.call_runner.dispatch_next(simulated_now)
                if submission is None:
                    break
                calls_submitted += 1
                if submission.outcome == "not_accepted":
                    # Confirmed 503: don't spin on the same eligible lead. A
                    # later, new intent is safe because this one was not placed.
                    self._retry_not_before = self.monotonic() + self._current_503_backoff
                    self._current_503_backoff = min(
                        self.max_503_backoff, self._current_503_backoff * 2
                    )
                    break
                if submission.outcome == "accepted":
                    self._current_503_backoff = self.initial_503_backoff

        self.dispatcher.reload_state()
        complete = self._all_leads_final()
        if complete:
            self._write_report()
        wait_seconds = 0.0 if complete else self._wait_seconds(simulated_now, speed)
        return LoopStep(simulated_now, complete, calls_submitted, wait_seconds)

    def run(self, *, max_steps: int | None = None) -> bool:
        """Run until all leads are final; max_steps is provided for deterministic tests."""
        steps = 0
        while max_steps is None or steps < max_steps:
            result = self.step()
            steps += 1
            if result.complete:
                return True
            self.sleep(result.wait_seconds)
        return False

    def _write_report(self) -> None:
        if not self._report_written:
            path = write_final_state_csv(
                self.dispatcher.store, fixtures_dir=self.dispatcher.fixture_dir,
                output_path=self.report_path,
            )
            LOGGER.info("Wrote final state report to %s", path)
            self._report_written = True

    def _all_leads_final(self) -> bool:
        leads = self.dispatcher.store.list_leads()
        return all(lead.stage in FINAL_STATES for lead in leads)

    def _wait_seconds(self, simulated_now: datetime, speed: float) -> float:
        """Sleep until next eligibility or recovery, whichever comes first."""
        now_mono = self.monotonic()
        outstanding = any(
            not call.result_applied and (call.status == "request_pending" or call.accepted)
            for call in self.dispatcher.store.list_calls()
        )
        # Poll outstanding provider calls at the recovery cadence. With no
        # active intents, sleep for scheduled eligibility and refresh the clock
        # at most every 30 seconds.
        deadlines = [self.recovery_interval if outstanding else 30.0]
        if self._retry_not_before > now_mono:
            deadlines.append(self._retry_not_before - now_mono)

        next_due = self.dispatcher.next_eligible_at(simulated_now)
        if next_due is not None:
            sim_delta = (next_due - simulated_now).total_seconds()
            if sim_delta > 0:
                deadlines.append(sim_delta / speed)
        # A positive floor protects against accidental zero-delay polling when
        # scheduling says work is due but a SQLite guardrail currently blocks it.
        return max(0.05, min(deadlines))


def build_loop(
    *, database_path: str | Path, fixtures_dir: str | Path = DEFAULT_FIXTURES,
    provider_base_url: str = "http://127.0.0.1:8099",
    callback_url: str = "http://127.0.0.1:8200/webhooks/telephony",
    output_path: str | Path = DEFAULT_OUTPUT_PATH,
    http_client=None, **loop_options,
) -> DispatcherLoop:
    """Wire SQLite, fixture dispatcher, provider client, recovery, and loop."""
    from solution.provider import TelephonyProviderClient

    fixture_dir = Path(fixtures_dir)
    store = SQLiteStore(database_path)
    dispatcher = FixtureDispatcher(fixture_dir, store)
    config = json.loads((fixture_dir / "campaigns.json").read_text(encoding="utf-8"))
    provider = TelephonyProviderClient(provider_base_url, http_client=http_client)
    call_runner = CallRunner(dispatcher, store, provider, callback_url)
    campaigns = {item["campaign_id"]: item for item in config["campaigns"]}
    recovery = CallRecovery(store, provider, campaigns)
    return DispatcherLoop(
        dispatcher, call_runner, recovery, provider, report_path=output_path, **loop_options
    )


def main() -> None:
    """Run until all fixture leads are final and write the configured CSV."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    database_path = os.environ.get(
        "DATABASE_PATH", str(ROOT / "assignment-b-workflow" / "dispatcher.sqlite3")
    )
    callback_url = os.environ.get(
        "CALLBACK_URL", "http://127.0.0.1:8200/webhooks/telephony"
    )
    provider_base_url = os.environ.get("PROVIDER_BASE_URL", "http://127.0.0.1:8099")
    output_path = os.environ.get("FINAL_STATE_PATH", str(DEFAULT_OUTPUT_PATH))
    loop = build_loop(
        database_path=database_path, callback_url=callback_url,
        provider_base_url=provider_base_url, output_path=output_path,
    )
    LOGGER.info("Starting dispatcher; provider=%s callback_url=%s final_state_path=%s",
                provider_base_url, callback_url, output_path)
    try:
        loop.run()
    except ProviderResponseError as exc:
        LOGGER.exception("Provider rejected a call with HTTP %s: %r", exc.status_code, exc.body)
        raise SystemExit(1) from exc
    finally:
        loop.provider.close()


if __name__ == "__main__":
    main()
