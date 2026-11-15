# Assignment B: Durable Campaign Dispatcher

## Candidate

- Name: Aman Prajapati
- Assignment: B — Workflow
- Time spent: approximately 7 hours

## Setup, tests, and local run

From the repository root, create an environment and install the project dependencies:

~~~bash
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m pytest -q
~~~

Run the three services in separate terminals. The webhook and dispatcher must use the same database. The callback URL must be reachable from the mock provider; 127.0.0.1 works when all three processes run on the same machine.

Terminal 1, from the repository root:

~~~bash
cd assignment-b-workflow
../.venv/bin/python -m mock_telephony --failure-scale 0
~~~

Terminal 2, from the repository root:

~~~bash
RUN_DIR=/tmp/assignment-b-run
mkdir -p "$RUN_DIR"
DATABASE_PATH="$RUN_DIR/dispatcher.sqlite3" .venv/bin/python -m solution.webhook
~~~

Terminal 3, from the repository root:

~~~bash
RUN_DIR=/tmp/assignment-b-run
mkdir -p "$RUN_DIR"
DATABASE_PATH="$RUN_DIR/dispatcher.sqlite3" \
CALLBACK_URL=http://127.0.0.1:8200/webhooks/telephony \
FINAL_STATE_PATH="$RUN_DIR/final_state.csv" \
PROVIDER_BASE_URL=http://127.0.0.1:8099 \
.venv/bin/python -m solution.loop
~~~

For a crash/restart check, send SIGKILL to the dispatcher process while the other two services continue running, then restart Terminal 3 with the same environment values and paths:

~~~bash
kill -9 <dispatcher-pid>
~~~

## Design and lead lifecycle

The implementation uses a SQLite-backed state machine. The fixture dispatcher inserts missing fixture leads but does not overwrite saved lead progress. The scheduler, provider HTTP client, result reducer, webhook receiver, recovery layer, and CSV writer are separate modules.

~~~mermaid
stateDiagram-v2
    [*] --> fresh
    fresh --> request_pending: eligible; persist intent and reserve
    retry --> request_pending: retry due and window open
    callback --> request_pending: callback due and window open
    request_pending --> request_pending: ambiguous 504 or lookup 404; reconcile same ID
    request_pending --> fresh: 503 not accepted; prior stage was fresh
    request_pending --> retry: 503 not accepted; prior stage was retry
    request_pending --> callback: 503 not accepted; prior stage was callback
    request_pending --> completed: early result with terminal disposition
    request_pending --> callback: early callback result; attempts remain
    request_pending --> retry: early retry result; attempts remain
    request_pending --> exhausted: early result reaches attempt limit
    request_pending --> active: provider confirms acceptance
    active --> completed: terminal disposition, including DND
    active --> callback: callback requested and attempts remain
    active --> retry: retryable result and attempts remain
    active --> exhausted: attempt limit reached without terminal disposition
    fresh --> dnd_blocked: phone was marked DND by another lead
    retry --> dnd_blocked: phone was marked DND by another lead
    callback --> dnd_blocked: phone was marked DND by another lead
    completed --> [*]
    exhausted --> [*]
    dnd_blocked --> [*]
~~~

A 503 returns the lead to its saved stage; backoff delays the next submission. The diagram shows that transition for fresh, retry, and callback leads. A signed result that arrives before the 201 response proves acceptance and goes directly to the reducer-selected next stage.

The scheduler uses the provider's simulated clock, IST campaign days/windows, and the 09:00–21:00 regulatory window. It orders due callbacks before retries and fresh leads. No-answer and busy outcomes retry after 60 simulated minutes; failed outcomes retry after 15 minutes, measured from the ended call. Callbacks wait until their requested time and then until the next valid calling window if needed. A terminal disposition completes the lead. A DND disposition also blocks that phone across campaigns and moves other non-final leads for that phone to dnd_blocked. Final-state precedence is completed, then exhausted, then dnd_blocked.

SQLite stores campaign limits, lead state, call intents/results, and globally blocked DND phones. A lead row contains its campaign and lead IDs, phone, stage, accepted attempt count, last result fields, callback time, and active request/call IDs. A call row is keyed by request_id and stores provider ID, acceptance/result state, status, disposition, and call timestamps.

Before a provider request, prepare_call runs in an immediate SQLite transaction. It checks the tenant-wide active-call limit, active calls to the same phone, global DND, and whether the lead is already active, then writes the request_pending call and reserves the lead. This makes the guardrail check and reservation atomic for dispatchers sharing that database.

The request_id is saved before the HTTP request and reused for that intent. A 200 or 201 marks the call accepted and increments attempts once. A confirmed 503 releases the reservation and uses backoff; it does not count as an attempt. A 504 is ambiguous, so recovery looks up the same request_id rather than creating a replacement. A pending request that lookup still cannot find remains unresolved and reserved. Recovery also checks accepted calls for results that were not delivered by webhook.

The webhook verifies X-AU-Signature with HMAC-SHA256 over the raw body, then sends valid results through the same reducer/store path as recovery. The store makes result application idempotent, including when the webhook arrives before the original acceptance response. The loop periodically runs recovery and waits using provider simulated time and speed instead of repeatedly polling or redialling active leads.

When every fixture lead is completed, exhausted, or dnd_blocked, the reporter atomically writes final_state.csv. It includes every fixture lead, sorted by campaign_id and lead_id, with exactly these columns: campaign_id, lead_id, final_state, attempts, last_status, last_disposition. Attempts count accepted calls; status and disposition come from the latest ended call and are blank if there is no ended call.

## Decisions and limitations

- SQLite was chosen for durable local state without a separate database service. BEGIN IMMEDIATE protects reservations shared by processes using the same database file. This is not a multi-host coordination design.
- Provider time is authoritative. The loop refreshes it before every lead selection; a previous batch-level clock snapshot became stale near a window boundary and caused an outside-window response. The fix has a fake-clock regression test.
- Ambiguous requests keep their original ID and reservation until reconciliation proves their state. This favors avoiding duplicate calls over making progress if the provider permanently returns 404 for a request that was never placed; resolving such an orphan would need an explicit operational policy.
- The verified end-to-end run used the supplied fixtures and failure-scale 0. It did not test randomized provider failures, an unseen seed, multi-host dispatch, or production callback routing. Unit tests use fake providers for response and recovery cases. The local callback URL must be replaced with a provider-reachable URL outside the same-machine setup.

## Verification and results

The full test command, .venv/bin/python -m pytest -q, completed with 68 passed and one Starlette/httpx deprecation warning. The targeted dispatcher-loop suite completed with 7 passed.

The clean and crash/restart end-to-end run used --failure-scale 0 and a fresh database at /tmp/assignment-b-kill9-final-yQinFO/dispatcher.sqlite3 with output at /tmp/assignment-b-kill9-final-yQinFO/final_state.csv. The mock was ready at 2026-11-15 07:19:37.450 UTC, the webhook at 07:19:39.990, and the first dispatcher started at 07:19:39.994. Dispatcher PID 3456887 received SIGKILL at 07:20:19.446 after 97 accepted calls; the signal was observed at 07:20:19.459. The mock and webhook stayed running. The dispatcher restarted at 07:20:37.875 with the same database and CSV path, then exited with code 0 at 07:21:59.142. From mock readiness to dispatcher completion, the run took 2 minutes 21.692 seconds. The provider reported 407 accepted calls, 407 delivered webhooks, zero active calls, and zero violations. All 200 provider leads matched the CSV on final state, attempts, last status, and last disposition. CSV attempts summed to 407, no lead exceeded its campaign attempt limit, and there were 139 completed, 60 exhausted, and 1 dnd_blocked lead.

The final CSV, database, service logs, provider report, lead data, exact process commands, and timestamps are preserved in /tmp/assignment-b-kill9-final-yQinFO. The run metadata is in that directory's run-metadata.json.

## Next steps

Run the suite and crash/restart workflow with an unseen provider seed and injected failures. Add an explicit operator path for permanently unresolved intents, then assess multi-host storage/coordination and deploy the webhook behind a routable, secured callback URL.
