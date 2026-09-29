# Verification

## Tests and review

The 64-test baseline passed on Linux and macOS with Python 3.10, 3.12 and 3.14.
A new regression test checks the 0.60 confidence default in Python and the CLI.
Package build and installed-wheel checks pass.

Tests cover concurrent requests, duplicate inputs, cost reservations, retries, interruptions, result identity and journal upgrades.
Independent reviews found four defects that now have fixes and regression tests:

- An interrupt could discard a received response.
- A journal upgrade could damage views, indexes and triggers.
- A response check could erase recorded costs.
- Deep JSON input could cause an unhandled error.

Final reviews found no remaining actionable defects.

## API check: 29 September 2026

The check used 100 consecutive saved requests from a real research-classification workload with four Choice questions each.
Inputs did not change. The model was `jev-1.13.0`.

| Measurement | Result |
| --- | --- |
| Successful requests | 100 of 100 |
| Valid answers | 400 of 400 |
| Maximum concurrent requests | 8 |
| Elapsed time | 6.274 seconds |
| Reported input tokens | 195,464 |
| Estimated cost | $0.008209488 |
| Unresolved reservations | 0 |
| Choices equal to previous answers | 378 of 400 (94.5%) |
| Responses recovered offline | 100; no new requests |

This measures repeated-answer agreement, not accuracy or comparative speed.
The sample was not representative. The live check used the concurrent runner.
Separate tests cover SQLite text selection and saved responses through the public Python client.
The source database and existing journal did not change.

See the [full measurements](validation/isolated-live-20260929.json).

## Ticket demo: 29 September 2026

The four-ticket example used 1,639 input tokens across four successful requests.
Estimated cost was $0.000068838. No reservations remained unresolved.
The offline threshold change reused all four responses without API calls.
See the [demo measurements](validation/tickets-live-20260929.json).
