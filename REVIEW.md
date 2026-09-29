# Design and review notes

This is the standalone sqlite-utils-jev package, extracted and reviewed before its initial GitHub publication. It has no runtime dependency on the parent application. No PyPI release has been published.

## Scope

The first release does four things:

1. Classify selected SQLite text columns using one JEV Choice question.
2. Retain answers and reuse unchanged requests when a run resumes.
3. Maintain persistent spending estimates and unresolved reservations.
4. Leave ordinary SQLite results that can be inspected without an API call.

There is no UI, server, task queue, extraction framework, SQL parser or generic provider abstraction. Choice-only is intentional. The Python API and sqlite-utils adapter share one implementation.

## Files to review

- `README.md`: installation, commands, result contract and recovery behavior.
- `src/sqlite_utils_jev/client.py`: durable requests, budgets, locking and recovery.
- `src/sqlite_utils_jev/pipeline.py`: source selection and result persistence.
- `src/sqlite_utils_jev/validation.py`: Choice input/response validation.
- `src/sqlite_utils_jev/cli.py`: thin sqlite-utils command adapter.
- `tests/`: mocked API behavior and real subprocess failure tests.
- `CODE_REVIEW.md`: first-principles review findings, fixes and remaining verification limits.
- `examples/`: synthetic input data and a routing question; no private research data.
- `pyproject.toml`, `LICENSE`, `.github/workflows/tests.yml`: packaging, MIT licensing and test-only CI.

## Decisions kept visible

The repository uses the name `sqlite-utils-jev`, version `0.1.0.dev0` and the MIT license. The PyPI name has not been reserved. The initial platforms are macOS and Linux because the runner uses `fcntl` locking.

The transport uses Python's standard library directly. That small layer retains raw bodies before validation and makes every dispatched attempt visible in the ledger. Introducing the official SDK would require controlling its internal retries to preserve that contract.

This is a simplified extraction of the project's execution pattern, not a replacement for the existing production client. It does not connect to the project's database, caches, key files or shared spending ledger. No migration of the working application is part of this draft.

The single model request is deliberately synchronous. A later concurrency or packing feature would need to demonstrate a useful benefit and preserve recovery semantics.

## Before a stable package release

Perform a small explicitly budgeted live API smoke test. Test fixtures and successful installation cannot establish current service compatibility or model quality. Keep performance claims out of the release until measured.

## Verification on 28 September 2026

- All 42 tests passed on macOS with Python 3.10.20 and Python 3.12.13 after the full package review.
- The built wheel installed into separate clean environments; both ran the full package suite successfully.
- The installed sqlite-utils plugin exposed all four commands and their help.
- Dependency checks passed in both installed-wheel environments.
- Both wheel and source distribution built successfully; the source distribution includes the examples and tests.
- Python compilation checks passed. The package has no separate static typecheck configured.
- Linux and Python 3.14 are configured in CI but have not been executed in this local review.
- No real JEV calls were made; no application database, private data or existing spending ledger was used or modified.

Implementation reference: Peptides `origin/main` at `c69952e03377a70f585eeab78c3774bc87e81a4d`. The package has no imports or runtime dependency on that repository.

The review fetched current `origin/main` (`7c29f9c034c8b4409ee63065f17a350ac99d1be0`) and inspected the active, uncommitted standalone draft in its existing worktree. It did not replace that draft with parent application changes.
