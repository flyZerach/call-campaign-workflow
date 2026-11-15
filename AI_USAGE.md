# AI Usage Disclosure

## Tools used

OpenAI ChatGPT/Codex coding assistant in this conversation.

## How AI assistance was used

I chose Assignment B and directed the work in small, reviewable slices. I asked for explanations of the campaign, simulated clock, provider, webhook, and scheduler concepts, then specified the required behavior for scheduling, persistence, dispatch, reduction, provider handling, recovery, reporting, tests, and the end-to-end crash/restart check.

The assistant inspected the repository, drafted and edited implementation modules and tests, ran the test suite and local mock-provider workflows, diagnosed failures, and prepared this documentation. I reviewed the work and made the necessary changes and decisions to complete it. The implementation uses a SQLite-backed state machine rather than Temporal.

## A concrete issue found and corrected

During end-to-end testing, a dispatcher batch reused one provider simulated-clock reading for several lead selections. Near a campaign-window boundary, simulated time advanced before a later request was submitted, and the provider rejected a call outside the window. The assistant identified the stale clock snapshot from the provider response, changed the loop to refresh provider time before each lead selection, and added a fake-clock regression test that crosses the window boundary between selections. The first diagnostic run stopped after 7 accepted calls with one outside_calling_window violation; that was before the fix and is not the final run result.

The corrected loop tests passed 7 tests. The full suite passed 68 tests with one Starlette/httpx deprecation warning. A fresh --failure-scale 0 crash/restart run then SIGKILLed the dispatcher after 97 accepted calls, restarted it with the same database and output path, and completed with 407 accepted calls, 407 delivered webhooks, zero active calls, and zero provider violations. All 200 provider leads matched the generated CSV, and no lead exceeded its attempt limit.

## How the work was verified

The full test suite covers the scheduler, reducer, SQLite persistence/guardrails, fake provider responses, webhook signatures and duplicate delivery, recovery, dispatch loop, and final CSV reporting. The end-to-end run used the supplied fixtures and mock provider with failure injection disabled. I compared every provider lead against the CSV and checked the provider admin report.

This does not establish behavior under randomized provider failures, an unseen seed, multi-host operation, or production callback routing. The fake-provider tests cover response paths, but those conditions were not exercised in the final end-to-end run. No fixture or mock-provider files were changed.

## Candidate review

I reviewed the work and made the necessary changes and decisions to complete it. I chose the assignment and SQLite-based approach, directed the implementation in incremental slices, and reviewed the end-to-end and crash/restart results before documenting them.
