# Reference

## Inputs and requests

- Select text columns with `--text`. Column values must be text or null.
- Select a unique integer or text key with `--key`. Null keys are not permitted.
- The tool sends the key only when you also select it with `--text`.
- Use a SQLite view to select a subset of records.
- Use `--limit N` to select the first N records in key order.
- Use `--workers N` to set the maximum number of concurrent requests. The permitted range is 1–16; the default is eight.

The tool reads the selection into memory before API calls start.
It rejects requests larger than 24,000 UTF-8 bytes. It does not shorten the text.
Empty records receive status `empty` without an API call.
A record is empty when all selected values contain only whitespace or nulls.

Use either [one question](../examples/routing.json) or [named questions](../examples/questions.json).
Each Choice question needs instructions and 2–255 categories.
Instructions must contain text. Category descriptions must be text or null.

Saved answers apply only to identical text, column names, questions and model versions.
Duplicate records share one response. Changes to record order or thresholds do not prevent reuse.

## Results

All answers must pass validation before the tool saves the record results in one database transaction.

| Field | Content |
| --- | --- |
| `label` | Accepted category, or null |
| `proposed_label` | Model choice, including rejected choices |
| `probability`, `confidence` | Separate acceptance scores |
| `status` | `accepted`, `abstained` or `empty` |
| `source_db`, `source_table`, `source_key_column` | Source location |
| `source_key` | Key encoded as JSON |
| `question_hash` | Identity of the question set and model |
| `question_name` | Answer name; `classification` for a single question |
| `request_hash` | Link to inputs and responses in `attempts` |

Use `json_extract(source_key, '$')` to read the original key.
Defaults are 0.75 probability and 0.60 confidence. Thresholds depend on the workload; test them on labeled records.
Use `--offline --min-confidence 0.50` to change a threshold without API calls.
Use `--min-probability` to change the other threshold.
Offline work needs no API key. It stops when a saved answer is missing.

Results store the source database’s absolute path. Moving the source creates separate result rows; saved answers remain reusable.
A failed request leaves previous results in place.
The tool retains results for deleted records and different question sets.
Source changes after selection apply to the next run.

Progress uses stderr; the final JSON summary uses stdout.
Use `--quiet` to hide progress.
Summary counts refer to records. An accepted record must pass all question thresholds.
`cache_hits` counts saved requests used; `cached_rows` counts records recovered from them.

## Allowance and retries

One process can use a journal at a time. Its workers share one allowance.
Separate journals have separate allowances.

Later `--budget-usd` values must match the initial allowance.
To change the total, use this command:

```sh
sqlite-utils jev budget decisions.sqlite --usd 2.00
```

This sets a $2 total allowance. It does not add $2.
The total cannot be less than recorded costs and unresolved reservations.

A reservation is an estimated cost saved before a request starts.
Reported usage replaces that estimate. Unknown usage keeps the reservation.
Invalid answers and later response checks do not erase costs.
`accounted_usd` includes costs and reservations for the entire journal.
Actual usage and price changes can exceed the allowance.

A failure stops new requests. The tool saves responses and costs for requests that have started.
To inspect failures, use this command:

```sh
sqlite-utils decisions.sqlite \
  "select request_hash, outcome, http_status, error from attempts order by id"
```

After a rate-limit error, wait for the provider limit to clear.
To permit one more attempt, replace `REQUEST_HASH` with the selected request hash:

```sh
sqlite-utils jev retry decisions.sqlite REQUEST_HASH
```

Then repeat the classification command.
The previous cost remains. A request that timed out can already have a charge.

After an interrupt or callback error, repeat the command to recover saved responses.
A forced process termination can leave unresolved requests that need inspection before a retry.

## Defaults and storage

Defaults are `jev-1.13.0` and $0.042 per million input tokens, checked on 29 September 2026.
If you select another model version, pass its fixed ID with `--model` and its current rate with `--input-price`.
If your current model’s price changes, pass the new rate with `--input-price` before new requests.
Each attempt keeps its original rate. Cache reads do not change recorded costs.
The documented model API does not supply prices. Check the [provider documentation](https://docs.typesafe.ai/models) before paid work.

Reservations use request bytes plus 4,096 estimated tokens.
Local limits permit 18 request starts and 200,000 estimated input tokens per second.
These values leave room below the provider’s documented 1,200 requests per minute and 250,000 tokens per second.
Source: [TypeSafe model limits](https://docs.typesafe.ai/models), checked on 29 September 2026. Provider limits can change without notice.

Use one journal path on a local filesystem.
Do not use hard-link aliases or network storage.
Journal and lock files start with permissions `0600`.
Protect journal backups: requests and responses contain source text.
The tool does not save the API key.

Journal versions 1 and 2 upgrade to version 4 automatically.
Version 1 results keep `source_key_column=''` when the original key column is unknown.
Filter by the required key column to exclude those legacy results.
Older tool versions cannot read version 4.
The tool rejects experimental version 3 journals from the closed batch branch.

## Python

```python
import json
from pathlib import Path
from sqlite_utils_jev import classify_table

questions = json.loads(Path("examples/questions.json").read_text())
summary = classify_table(
    "tickets.db", "tickets",
    key="id", text_columns=["subject", "body"], questions=questions,
    state="decisions.sqlite", budget_usd="1.00", workers=8,
)
```

For one question, supply `question=` instead of `questions=`.
Supply `progress=callback` to receive a summary after each completed record.
A callback error stops result publication. Saved responses remain available for an offline run.

For a JSON record, use the direct client:

```python
from sqlite_utils_jev import Client

with Client("decisions.sqlite", budget_usd="1.00") as client:
    result = client.evaluate_questions({"body": "Please refund this charge."}, questions)
```

The record must be a nonempty JSON object. It can contain arrays and nested objects.
The result contains `request_hash`, `answers` and `cached`.
For one question, use `client.evaluate(record, question)`. Its result contains `answer` instead of `answers`.
Direct calls are sequential and do not write table results.
Use each Client from one thread.
