# sqlite-utils-jev

Run [TypeSafe Jev](https://docs.typesafe.ai/) over SQLite text, with parallel requests, saved answers, a shared spending allowance and restart recovery.

**Early preview:** Python 3.10+, macOS/Linux, MIT. Install from source; not published on PyPI. Inference uses TypeSafe's hosted API. Results and accounting stay in a separate local SQLite journal; the source database is read-only.

## Install and classify

```sh
git clone https://github.com/Peterrallojay/sqlite-utils-jev.git
cd sqlite-utils-jev
python3 -m venv .venv
source .venv/bin/activate
python -m pip install .
sqlite-utils insert tickets.db tickets examples/tickets.csv --csv --pk id
```

Set `TYPESAFE_API_KEY` in your environment, then:

```sh
sqlite-utils jev classify tickets.db tickets \
  --key id --text subject --text body \
  --question examples/routing.json \
  --state decisions.sqlite --budget-usd 1.00
```

This sends the selected text to TypeSafe and may incur charges. The key is sent only if also selected with `--text`. Use a new filename for the example database.

Repeat the command to resume. Identical requests reuse saved responses. Eight HTTP workers run by default; use `--workers 1` for sequential execution or choose 1–16 workers. **Each request contains one record**, regardless of worker count. Different records never share model context.

Progress goes to stderr; the final JSON summary goes to stdout. `--quiet` suppresses progress. `--limit 20` selects the first 20 rows ordered by key. For filtering, classify a SQLite view. Keys must be unique, non-null integers or strings; selected columns must contain text or nulls. Whitespace-only rows are saved as `empty` without a request.

## Several questions about each record

Use `--questions examples/questions.json` instead of `--question`. Its JSON maps names to Choice questions:

```json
{
  "team": {
    "type": "choice",
    "instructions": "Which team should handle this ticket?",
    "criteria": {"billing": "Payments and refunds", "technical": "Bugs and outages"}
  },
  "urgency": {
    "type": "choice",
    "instructions": "Does this ticket explicitly require urgent action?",
    "criteria": {"urgent": "Explicit urgency", "normal": "No explicit urgency"}
  }
}
```

All questions are sent together with that record. Every answer must validate before any result for the record is published; those writes commit together. Only Choice is supported, with nonempty string instructions and 2–255 categories whose descriptions are strings or nulls.

The cache identity includes the complete selected text, column names, question map and pinned model. Changing a question causes a new request; adding a question does not reuse partial answers. Duplicate records reuse one request, including when workers overlap. Changing the selected rows or their order does not invalidate unchanged records.

## Read results

```sh
sqlite-utils decisions.sqlite \
  "select source_key, question_name, label, proposed_label, probability, confidence, status from results"
```

There is one result per source record and question name. Single-question calls use `classification`. `label` is accepted only when both probability and confidence meet their thresholds (default 0.75); otherwise it is null and `status` is `abstained`. `proposed_label` retains the model's choice. These thresholds are not measured accuracy.

`source_key` is JSON-encoded to preserve integer/string identity; recover it with `json_extract(source_key, '$')`. `source_db`, `source_table` and `source_key_column` identify its origin. `question_hash` identifies the whole question set and model. `request_hash` connects a result to the exact inputs and raw responses in `attempts`; `question_name` selects its answer.

To apply different thresholds without API calls, repeat classification with `--offline --min-confidence 0.90`. Offline mode needs no key and stops on a cache miss. Summary counts are **rows**: a row is accepted only if every question passes; individual statuses remain available in `results`. `cache_hits` counts reused requests and `cached_rows` counts recovered source rows.

Results describe the last successfully processed snapshot. Changed inputs update results only after success; a failed reevaluation leaves earlier results intact. Deleted source rows are not automatically removed. Different question sets retain separate results. Source edits after selection are observed on the next run.

## Shared allowance and recovery

Use the **same `--state` path across tables, databases and successive runs** to share spending and caches. The first `--budget-usd` sets a total lifetime allowance for that journal. Subsequent values must match; separate journals have separate allowances. One process owns a journal at a time, with concurrent HTTP workers inside it.

```sh
sqlite-utils jev status decisions.sqlite
sqlite-utils jev budget decisions.sqlite --usd 2.00
```

The second command changes the total to $2, not $2 more. It cannot go below recorded costs and unresolved reservations. Every request reserves spending durably before dispatch; workers cannot spend the same remaining allowance. Reported input usage settles the reservation, even when an answer is invalid. Already settled history is never repriced by cache reads.

Reservations estimate input tokens using serialized request bytes plus 4,096. The default rate is $0.042 per million input tokens for pinned model `jev-1.13.0`, checked against [TypeSafe's model documentation](https://docs.typesafe.ai/models) on 2026-09-28. Use `--model` and `--input-price` for another pinned version/rate. Moving model aliases are rejected. **This is local accounting, not a provider-enforced spending cap:** actual usage or changed prices can exceed the estimate. Activity using another journal is not included.

There are no automatic paid retries. On failure, new dispatches stop when the coordinator observes it; already-started calls finish and their usage/responses are saved. Other successful records are published. Unknown outcomes retain their reservations and block that exact request until explicitly authorized:

```sh
sqlite-utils decisions.sqlite \
  "select id, request_hash, outcome, http_status, error from attempts order by id"
sqlite-utils jev retry decisions.sqlite REQUEST_HASH
```

Inspect the failure, then repeat classification. Permission allows one additional attempt and keeps the earlier cost/reservation. A timeout may already have been billed. After a rate-limit error, wait before authorizing a retry. Dispatch pacing also limits starts to 18 requests/second and estimates to 200,000 input tokens in a rolling second; these are local limits, not a guarantee about your account's current [provider limits](https://docs.typesafe.ai/models).

A graceful interrupt or application callback exception drains already-started requests and saves their responses; rerun to publish any remaining results. A hard process kill can leave pending reservations that require inspection and explicit retry. No in-memory queue survives process exit.

## Python

```python
from sqlite_utils_jev import classify_table

summary = classify_table(
    "tickets.db", "tickets", key="id", text_columns=["subject", "body"],
    questions=questions,  # A map like the JSON above; or pass question= for one.
    state="decisions.sqlite", budget_usd="1.00", workers=8,
    progress=lambda update: print(update["rows"], update["total"]),
)
```

For records already in Python, use `with Client("decisions.sqlite", budget_usd="1.00") as client:` and call `client.evaluate_questions(state, questions)`. It returns `request_hash`, `answers` and `cached`. The original `client.evaluate(state, question)` returns a single `answer`. These direct calls are synchronous and do not write table results. Direct-call state is a nonempty JSON object and may include structured metadata such as arrays. SQLite table calls select text/null columns. Use a Client from one calling thread; the table runner owns its HTTP workers internally.

## Limits and development

- Selections are loaded into memory and fully checked for oversized requests before paid work. The 24,000-byte request limit rejects long records without truncation. Intended for small and medium jobs.
- Use one canonical journal path on a local filesystem, not hard-link aliases or network storage. New journal/lock files use mode 0600. Raw inputs and responses may contain sensitive text; protect backups. Credentials are not persisted.
- Original v1/v2 journals upgrade automatically, retaining spending, caches and results. v1 rows with unknown key-column names keep `source_key_column=''`. v4 adds `question_name`; older package versions refuse it. The closed experimental batching branch's v3 journals are intentionally unsupported; they must not be silently treated as isolated records.
- A controlled live check completed 100 unchanged Peptides requests (400 answers), costing about $0.00821 in reported usage. Choices agreed with earlier saved responses 94.5% of the time; this measures repeat-run agreement, not accuracy. See [VALIDATION.md](VALIDATION.md) for the sample, limitations and adversarial review.

```sh
python -m pip install -e .
python -m unittest discover -s tests -v
```

Tests make no external calls. CI runs on Linux/macOS with Python 3.10, 3.12 and 3.14. Independent of TypeSafe, MotherDuck and sqlite-utils. Adapted from operational lessons in a Python/SQLite research application; this package is not yet a production replacement for that application's client.
