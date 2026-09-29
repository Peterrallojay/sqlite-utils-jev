# Standalone package code review — 28 September 2026

Reviewed all package source, tests, examples, documentation, packaging and CI. The scope is this standalone JEV utility, not the parent Peptides application. The baseline is the original draft source bundle; the specification is the requested small SQLite utility and its documented public contract. Independent standards and contract reviews were followed by reproductions, fixes and a second review.

## First principles

The essential invariants are:

1. A saved classification identifies the exact source row and request that produced it.
2. Every potentially billed dispatch has a durable reservation before network I/O.
3. A lost or invalid response never causes an automatic paid retry.
4. Successful saved responses remain reusable without credentials or network access.
5. Raw response evidence and usage survive semantic validation failures.
6. Source reads do not mutate the user's database; only selected text goes to the API.

The existing division into validation, durable client, table pipeline and command adapter is appropriate. No server, queue, generic provider layer or additional framework is needed. The fixes add about 40 runtime lines to the original draft, chiefly for preserving journal data during the identity correction.

## Standards

All confirmed defects below are fixed:

- **Result identity was incomplete.** The results key omitted the name of the source key column. Switching between two valid key columns with overlapping values overwrote unrelated results. The key now includes `source_key_column`, and result inserts explicitly name their columns. A regression verifies that both row mappings survive a partial rerun.
- **Schema inspection rejected generated columns.** `table_info` omitted SQLite generated columns. `table_xinfo` now discovers them; tests cover stored and virtual text columns and a generated key.
- **Numeric response validation could crash.** A very large integer confidence overflowed float conversion, leaving a response stuck in `received` and unavailable through normal retry controls. Range checks now precede finite-number conversion. The regression checks the complete invalid-response, accounting and authorized-retry path.
- **Money conversion could overflow or silently round.** Extreme decimal exponents escaped the normal error path, while excess precision could round fractional nanodollars into accepted values. Conversion now checks bounds before scaling, preserves supplied precision, and reports invalid values consistently. Tests cover invalid precision, extreme exponents and exact storage boundaries.
- **Malformed credentials reached dispatch accounting.** Invalid header values could fail after creating a reservation. New requests now validate credentials before reserving or dispatching. Cached/offline requests bypass this check because they do not need credentials.

The bare dictionary return annotations provide limited editor guidance. More detailed public types would be a reasonable later improvement, but were not treated as a correctness defect or a reason to expand this release.

## Spec

All confirmed contract defects below are fixed:

- **Row provenance:** changing key columns could silently mix classifications from different rows. The corrected identity and its regression cover this independently of request caching.
- **SQLite text support:** ordinary generated text columns were rejected despite being valid selectable text. Both generated column forms are now supported.
- **Recovery evidence:** incomplete HTTP reads discarded known HTTP status, request ID and available partial bytes. These are now saved while the attempt remains `unknown`, keeps its reservation, and remains blocked from automatic retry.

The journal correction upgrades version 1 to version 2 transactionally. Earlier results remain with `source_key_column=''`, explicitly meaning unknown; the original draft never saved enough information to reconstruct that name. Requests, raw responses, rates and spending history are preserved. An offline rerun can rebuild identified results from cached responses. Tests verify successful upgrade, unchanged history, rollback on a malformed legacy schema and rejection of an unknown future version. The README explains how to filter legacy rows.

Incremental results remain intentional: deleted source rows are retained, and a failed reevaluation leaves the preceding result intact. The README now spells out that a result's request hash can therefore refer to earlier input. This utility does not claim to maintain a continuously synchronized source view.

## Verification and limits

- The expanded 42-test suite passes against installed wheels on macOS with Python 3.10.20 and Python 3.12.13.
- Regression tests first reproduced the original failures before the fixes were applied.
- Actual subprocess tests cover journal locking and termination after a durable reservation.
- Dependency checks and all four installed command help checks pass in both environments.
- Wheel and source distribution build successfully. Distribution contents include the examples, tests and review documents where appropriate.
- The second independent review found no additional actionable correctness defect after the credential check was moved behind cache/offline handling.
- No live API requests, private data, application databases or production spending ledgers were used. During this review, no repository was created or package published; repository creation followed separately.
- Linux and Python 3.14 remain configured but unexecuted locally. A small, explicitly budgeted live API smoke test is still required before release; mock tests cannot prove current service compatibility or classification quality.

Standards: five confirmed defect groups fixed; the most consequential was incomplete row identity. Spec: three confirmed contract defects fixed; the most consequential was incorrect row provenance. No confirmed correctness finding remains open in this review.
