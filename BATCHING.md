# Resumable batching: contract and review record

Branch: `codex/resumable-batching`, based on `origin/main` at `f3737d55725233c91f2395bb55ee8bb76cc676a6`.

## Scope

Provide automatic packed classification through the existing SQLite command and Python table API, retain isolated execution, and preserve durable accounting/recovery. Keep the existing synchronous process and file lock. No provider framework, workers, new service or queue.

## Required behavior

- Plan deterministic batches from the selected source snapshot before paid work. Bound batches by actual serialized UTF-8 bytes and a conservative 40-distinct-record cap. Reject a single oversized row before dispatching anything.
- Include the target record path in every question's instructions. Answer IDs alone are not model instructions. Send selected text only, never an unselected source key.
- Persist exact payload, input identities, and spending reservation atomically before dispatch. A batch input identity includes original row text, column names, question and pinned model; physical source keys are irrelevant to model cache identity.
- Cache complete payloads, including every co-batched row and generated question. Never pretend packed answers are independent of their surrounding context. Keep packed and isolated result identities separate.
- Resume an unchanged selection from successful/received responses. Block automatic retry of uncertain or invalid attempts. Prevent repacking the same unresolved inputs from bypassing that block, including switching modes. Explicit retry authorizes only its exact original request.
- Validate the entire answer set and settle returned usage before publishing. Persist every result of a batch in one transaction. Include the exact answer key in each saved result.
- Display progress on stderr and final JSON on stdout; offer quiet mode and a Python progress callback. Accounted spending includes historical costs/reservations in the journal.
- Upgrade earlier journals while retaining prior attempts, reservations, isolated caches and provenance. Old isolated attempts participate in the regrouping guard.

## Deliberate tradeoffs

Rows are loaded into memory to release source locks before network work. Packing changes context and may change answers. Only complete, identical batches reuse cached responses; changes in source membership or limits may cause other rows to be evaluated again. Failed batches are never automatically split. To recover an unresolved batch, restore its original selection/mode and authorize an exact retry where necessary. Source changes do not delete old results.

The transport remains at most one request per second. Batching reduces request count without claiming provider maximum throughput. The local 24,000-byte bound stays below the documented model context sizes but is not a provider token-count guarantee.

## Validation

`python -m unittest discover -s tests -v` covers isolated compatibility plus batching, strict ID mapping, byte bounds, malformed/partial responses, atomic result rollback, budget exhaustion, regrouping blocks, journal migration and actual process termination.

`python examples/resume_demo.py` is a reproducible offline crash/resume demonstration: 400 rows, killed after 200 committed rows, 200 cache-recovered rows, five new requests after restart, ten packed requests total versus 400 isolated requests. Responses and usage are synthetic; there is no live speed, pricing or classification-quality claim.

Live packed-versus-isolated agreement and service compatibility remain unverified. No paid model calls were performed as part of this implementation.

## Adversarial review

### Standards and correctness

An independent adversarial reviewer reproduced two parser defects: duplicate answer identifiers silently selected the last value, and excessive JSON nesting could leave an attempt stuck as received. Both received failing regression tests before fixes. Parsing now rejects duplicate object keys and routes recursion failures through the invalid-response path, retaining raw data and reservations and permitting explicit retry. The reviewer verified both fixes.

The implementation review additionally reproduced a progress callback mutating the caller-owned question after planning, which could mismatch the stored task hash and dispatched question. The pipeline now snapshots the validated question and criteria before planning; a regression verifies the original question is dispatched despite callback mutation.

### Contract

The separate contract reviewer found no actionable gap after testing direct-API duplicate/reordered inputs and partial responses. Even after retry authorization, overlapping regrouped requests remain blocked until the exact original request succeeds; the older uncertain reservation remains accounted for. A separate fault-injection probe, now a regression test, verified that a failed attempt insertion rolls back batch membership and sends nothing.

The review does not establish live service compatibility or classification agreement. Those limitations remain explicit; this branch makes no live performance or quality claim.

### Verification

The expanded suite has 62 tests. Local checks cover Python 3.10, 3.12 and 3.14, installed packaging, all command help paths, and the offline subprocess demonstration. GitHub CI runs the same suite on Linux and macOS across all three Python versions; its results are linked from the pull request.
