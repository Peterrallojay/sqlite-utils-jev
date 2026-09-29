# sqlite-utils-jev

Classify text in a SQLite table with [TypeSafe Jev](https://docs.typesafe.ai/), save the decisions, and resume without repeating successful requests.

**Early preview.** Python 3.10+, macOS or Linux. MIT licensed. Install from source; this package is not yet published on PyPI.

This package provides a `sqlite-utils` command and a Python API. It reads selected text columns and writes a **separate SQLite file** containing results, raw responses and spending history. Your source database stays unchanged. Jev inference uses TypeSafe's hosted API; saved results can be queried offline.

## Install

Clone the repository and install into a virtual environment:

```sh
git clone https://github.com/Peterrallojay/sqlite-utils-jev.git
cd sqlite-utils-jev
python3 -m venv .venv
source .venv/bin/activate
python -m pip install .
sqlite-utils jev --help
```

Already using `sqlite-utils`? Install into its environment with `sqlite-utils install .`.

## Classify a table

Create a small database from the bundled synthetic tickets, using a new filename:

```sh
sqlite-utils insert tickets.db tickets examples/tickets.csv --csv --pk id
```

Set `TYPESAFE_API_KEY` in your environment, then run:

```sh
sqlite-utils jev classify tickets.db tickets \
  --key id --text subject --text body \
  --question examples/routing.json \
  --state decisions.sqlite --budget-usd 1.00
```

This command sends selected text to TypeSafe and may incur API charges. Only explicitly selected text columns are sent; the row key is not sent unless also selected with `--text`.

The question is a single Choice question:

```json
{
  "type": "choice",
  "instructions": "Which team should handle this ticket? Choose unknown if the text is insufficient.",
  "criteria": {
    "billing": "Payments, invoices and refunds",
    "technical": "Errors, outages and installation problems",
    "unknown": "Insufficient information"
  }
}
```

Choose 2–255 categories with string or null descriptions. This first version supports category classification only. Instructions must be a nonempty string. Noul and Score are outside this release's scope.

Repeat the same command to resume. Unchanged successful requests come from the journal. Changed text, question or pinned model produces a new request. Identical text under the same column names can share a response across rows. Empty or whitespace-only rows are recorded as `empty` without an API call.

Use `--limit 20` to start with the first 20 rows ordered by key. To filter a dataset, prepare a SQLite view and classify that view. The key must be a unique, non-null integer or string throughout the table/view. Selected columns must contain strings or nulls.

## Read results

```sh
sqlite-utils decisions.sqlite \
  "select source_key_column, source_key, label, proposed_label, probability, confidence, status from results"
```

`source_key` is JSON-encoded to preserve integer versus string identities; use SQLite's `json_extract(source_key, '$')` to recover the original value.

| Column | Meaning |
| --- | --- |
| `label` | Accepted category, or `NULL` when below the thresholds |
| `proposed_label` | JEV's category even if it was not accepted |
| `probability` | Probability assigned to that category |
| `confidence` | The model's separate distribution-derived confidence |
| `status` | `accepted`, `abstained` or `empty` |
| `source_db`, `source_table`, `source_key_column`, `source_key` | Origin of the input row, including the column used as its key |
| `question_hash`, `model` | Identity of the classification task |
| `request_hash` | Look up request, response and usage in `attempts`; retries can produce several attempts per hash |

Both probability and confidence must reach 0.75 by default. Set `--min-probability` and `--min-confidence` to change these independent thresholds. These are operating thresholds, **not measured accuracy**. An explicit `unknown` category is an ordinary model answer; `abstained` means the acceptance thresholds were not met.

Reclassify using only saved answers, without a key or network calls:

```sh
sqlite-utils jev classify tickets.db tickets \
  --key id --text subject --text body \
  --question examples/routing.json --state decisions.sqlite \
  --min-confidence 0.90 --offline
```

Threshold changes reuse the original response. Offline mode stops at the first missing successful answer; earlier result updates remain saved.

`results` holds the last successfully processed result for each source row, key-column name, question and model. Changed inputs and thresholds update that result; different keys, questions or models have separate rows. A failed reevaluation leaves the earlier result in place: its request hash still identifies the earlier input. `attempts` retains earlier requests and raw responses. Rows deleted from the source are not removed from results, and edits after source selection are not observed until the next run. This is a record of processed inputs, not a continuously synchronized view.

Journals created by the original draft upgrade automatically on their next write. That draft did not record key-column names, so its old results are preserved with `source_key_column=''` (unknown). Rerun classification to create correctly identified results from cached answers; legacy rows remain alongside them. Filter by the intended key, for example `WHERE source_key_column='id'`, when reading those results. The upgrade preserves spending history and cached responses; `status` remains read-only.

## Spending and recovery

```sh
sqlite-utils jev status decisions.sqlite
```

The first `--budget-usd` sets a **total allowance for that journal**, including future runs. Passing it again must match the saved allowance. All processes using that journal share its accounting. Separate state files have separate allowances; keep the journal to preserve its cache and spending history.

To explicitly change the total:

```sh
sqlite-utils jev budget decisions.sqlite --usd 2.00
```

This means $2 total, not $2 more. It never resets previous spending and cannot go below recorded costs and unresolved reservations.

Each request reserves an estimate before dispatch. A successful response's input-token usage settles that estimate at the configured rate. Missing usage, timeouts and HTTP errors retain the reservation. The raw HTTP response is saved before answer validation, so invalid decisions do not erase their usage. Incomplete responses retain any available status, request identifier and partial body while keeping the outcome `unknown`; partial bodies are never used as cached answers. `status` distinguishes `reported_cost_usd` from `unresolved_reservation_usd` and lists blocked request hashes.

The default model is `jev-1.13.0`; moving aliases are rejected so cached answers do not silently change meaning. The default input price is $0.042 per million tokens, checked against [TypeSafe's model documentation](https://docs.typesafe.ai/models) on 2026-09-28. `--input-price` can change the rate for new requests, in USD per million tokens. Each attempt retains its own rate; changing the setting does not reprice history.

Reservations use serialized UTF-8 request bytes plus a 4,096-token margin. This is an estimate, **not a provider-enforced spending cap or invoice**. Usage exceeding a reservation or changed provider pricing can exceed the configured allowance. The next request then stops. Account activity outside this journal is not counted.

The runner sends one request at a time, at most once per second, and never automatically retries. After an HTTP error, invalid response or uncertain network outcome, it stops. A restart reuses completed responses but does not blindly redispatch the failed request.

Inspect the attempt before authorizing another call:

```sh
sqlite-utils decisions.sqlite \
  "select id, request_hash, outcome, http_status, error, request_id from attempts order by id"
sqlite-utils jev retry decisions.sqlite REQUEST_HASH
```

Then repeat `classify`. `retry` authorizes one additional attempt for that exact request and retains the earlier charge/reservation. The first call may already have been billed. For a rate-limit response, wait for the provider's limit to clear; there is no automatic backoff loop. A crash after receiving and saving a valid response can recover from that response without redispatch.

## Python API

```python
import json
from pathlib import Path
from sqlite_utils_jev import classify_table, status

summary = classify_table(
    "tickets.db", "tickets",
    key="id",
    text_columns=["subject", "body"],
    question=json.loads(Path("examples/routing.json").read_text()),
    state="decisions.sqlite",
    budget_usd="1.00",
)
print(summary)
print(status("decisions.sqlite"))
```

For scripts that already have records:

```python
from sqlite_utils_jev import Client

with Client("script-decisions.sqlite", budget_usd="1.00") as client:
    result = client.evaluate(
        {"body": "Please refund this duplicate charge."},
        {
            "type": "choice",
            "instructions": "Does this message request a refund?",
            "criteria": {"yes": "Explicit refund request", "no": "No refund request"},
        },
    )
    print(result["answer"], result["cached"])
```

`Client.evaluate()` saves requests/responses but does not populate table results or apply acceptance thresholds. `classify_table()` adds those behaviors. `Client` must be used as a context manager, from one thread at a time. Both interfaces read the key from `TYPESAFE_API_KEY` or accept `api_key=` explicitly. The package does not read project-specific key files.

## Deliberate limits

- One local process per journal, enforced by an OS file lock. macOS/Linux only; no background jobs or distributed workers. Use one canonical journal path on a local filesystem, not hard-link aliases or network storage.
- Source selections are loaded into memory before API calls so source-database locks are released. Intended for small and medium jobs, not warehouse-scale scans.
- One Choice question per row; multiple selected text columns share that row's state. No SQL UDFs, multi-row packing, arbitrary-query runner or alternate provider endpoints.
- Serialized requests above 24,000 bytes are rejected without truncation. Preprocess long documents yourself; this package does not perform passage extraction.
- Requests and responses can contain sensitive source text. New journal and lock files use mode 0600; protect their backups. The key itself is not persisted. There is no automatic retention deletion.
- No live API smoke test or paid benchmark has been run for this preview. Tests demonstrate software behavior with synthetic responses, not JEV's classification accuracy.

## Development

```sh
python -m pip install -e .
python -m unittest discover -s tests -v
```

Tests make no external model calls. They cover interruption/resume, actual subprocess crashes and locking, duplicate content, failed responses, budgets, thresholds, source preservation and the installed plugin. The included CI workflow runs tests only; it does not publish packages.

Adapted from operational lessons in a Python/SQLite research application. Independent of TypeSafe, MotherDuck and sqlite-utils. The [TypeSafe API](https://docs.typesafe.ai/api) and [sqlite-utils plugin API](https://sqlite-utils.datasette.io/en/stable/plugins.html) define the external interfaces.
