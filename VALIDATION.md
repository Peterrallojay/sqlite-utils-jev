# Isolated concurrency: contract and validation

Branch `codex/isolated-concurrency`, based on main `f3737d55725233c91f2395bb55ee8bb76cc676a6`. Replaces the closed packed-record experiment in PR #1.

## Contract

- The direct Python client accepts structured JSON records, preserving metadata arrays. The SQLite table interface selects text/null columns.
- Each HTTP request contains one source record and one or more named Choice questions. Worker count changes scheduling, never model context.
- Default eight HTTP workers, configurable 1–16. One calling thread owns every journal read/write and every result/progress callback. Only transport runs in workers.
- Fully preflight selected input types and request sizes before any paid call. Use a frozen source selection; never send unselected columns.
- Persist the complete payload and reservation before dispatch. Check the same shared allowance serially. At most the worker count may be outstanding; there is no unbounded submission queue.
- Exact request hashes include the entire question map. Wait for an in-flight duplicate before taking its cache path. Do not reuse partial question answers.
- Validate all answers before publishing all question results for a record in one transaction. Summary acceptance requires all questions to pass.
- Stop dispatch when a failure is observed. Drain existing calls and account for their responses; no automatic retry. A callback/storage exception aborts publication, but retained successful responses can be published on an offline rerun.
- Preserve settled costs when revalidating old responses. Preserve original single-question cache/task identities. Retain legacy journal data and dependent views/indexes/triggers during the v2 upgrade. Refuse experimental v3 journals explicitly.
- Keep the single-process journal lock, total persistent allowance and explicit one-attempt retry authorization. Separate journal files do not share a budget.

## Adversarial review

Independent standards and contract reviews inspect the implementation against the contract above. Initial findings:

1. An interrupt during response completion could remove that future from the cleanup set too early. Fixed by retaining it until completion finishes; a fault-injection regression interrupts `_complete` and proves the saved response can resume offline.
2. Rebuilding the results table could silently break user-created views and drop indexes/triggers. Fixed by preserving view references and recreating dependent objects inside the migration transaction; a regression verifies all three.
3. The previous branch's strict-JSON revalidation could erase settled historical usage. This branch preserves already-settled tokens and charges; a v2 journal regression verifies the cost survives duplicate-key rejection during an offline read.

4. Deep structured state could serialize successfully but overflow the recursive key check. A failing depth-sweep regression reproduced the uncaught exception. The key walk and snapshot decoding now normalize that failure to `JevError`; the independent reviewer verified the fix.

The final independent standards and contract passes reported no remaining actionable findings.

## Verification

The suite covers overlapping requests, bounded dispatch, duplicate inputs, budget reservation contention, settling in-flight work after failure, exact named-answer mapping, per-record atomic writes, progress exceptions, interruptions, old caches, strict response validation and migrations. Tests use synthetic HTTP responses and temporary databases; no paid calls occur in the test suite.

The suite has 64 tests, passing locally on Python 3.10, 3.12 and 3.14. The wheel and source distribution build successfully; the installed wheel runs the same suite. CI covers those Python versions on Linux and macOS.

### Saved production-response replay

A read-only snapshot of 100 distinct Peptides requests (source rowids 101993–102092) passed through the public `Client.evaluate_questions()` interface with their original saved HTTP responses. All 100 dispatched payload hashes matched exactly, all 400 named answers validated, and raw responses were retained unchanged. A reopened journal recovered all 100 through the offline cache with zero HTTP calls. Fixture accounting matched the source's 195,464 input tokens. This was replay, with no additional spend.

### Controlled live check — 2026-09-29 UTC

With user authorization and a $0.20 ceiling, the concurrent runner sent those same 100 original structured payloads to pinned `jev-1.13.0`. The local journal allowance was $0.15, leaving headroom; no retries or budget increases occurred. The production database and ledger were read-only. No credentials or source text are committed here.

- 100/100 requests succeeded; 400/400 answers passed validation.
- Eight workers reached eight simultaneous requests; elapsed time was 6.274 seconds.
- Reported usage: 195,464 input tokens, estimated cost **$0.008209488** at the configured rate. No unresolved reservations.
- Reopening the journal recovered all 100 responses offline, with zero new requests.
- Choice agreement with the earlier saved answers: **378/400 (94.5%)**. By question: outcome focus 89%, research model 98%, source kind 98%, study design 93%.

This convenience sample consists of the latest 100 saved requests in the frozen selection. Agreement measures repeat-run consistency, not correctness against human labels. There is no sequential comparison or broad speed/accuracy claim. The live path used the package's concurrent runner on original structured payloads; selected-text SQLite table extraction is covered by the integration tests, not by this live comparison.

The sanitized [machine-readable report](validation/isolated-live-20260929.json) records the sample identity, timings, usage, per-question agreement and offline recovery.
