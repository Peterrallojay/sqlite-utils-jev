# sqlite-utils-jev

Classify SQLite text with [TypeSafe Jev](https://docs.typesafe.ai/).
Save answers and costs in a separate SQLite journal. Keep the source database unchanged.
Each request contains one record and one or more Choice questions. Eight requests can run at the same time.

Use this instead of [LLM](https://llm.datasette.io/) when you need threshold-based abstention and a shared budget journal that survives process crashes.

Alpha. Python 3.10+. macOS and Linux only: the journal requires a Unix file lock. MIT license.

## Install

Version 0.1.0 is available on [PyPI](https://pypi.org/project/sqlite-utils-jev/).

```sh
python -m pip install sqlite-utils-jev
```

If you already use sqlite-utils, run `sqlite-utils install sqlite-utils-jev` in its environment.

## Try four tickets

1. Save [tickets.csv](https://raw.githubusercontent.com/Peterrallojay/sqlite-utils-jev/v0.1.0/examples/tickets.csv) and [routing.json](https://raw.githubusercontent.com/Peterrallojay/sqlite-utils-jev/v0.1.0/examples/routing.json) in your current directory.
2. Set the `TYPESAFE_API_KEY` environment variable.
3. Run these commands. Use a new database name.

```sh
sqlite-utils insert tickets.db tickets tickets.csv --csv --detect-types --pk id
sqlite-utils jev classify tickets.db tickets \
  --key id --text subject --text body --question routing.json \
  --state decisions.sqlite --budget-usd 0.01
sqlite-utils decisions.sqlite \
  "select source_key, label, status from results order by source_key" --table
```

Real output from `jev-1.13.0` on 29 September 2026:

```text
  source_key  label      status
------------  ---------  --------
           1  billing    accepted
           2  technical  accepted
           3  other      accepted
           4  unknown    accepted
```

Four requests used 1,639 input tokens: $0.000068838 at the configured price.
`unknown` is a category in this question. It is not an abstention.
Answers can change between runs.

## Change thresholds without API calls

```sh
sqlite-utils jev classify tickets.db tickets \
  --key id --text subject --text body --question routing.json \
  --state decisions.sqlite --offline --min-confidence 0.50
```

Defaults are 0.75 probability and 0.60 confidence. Both thresholds must pass.
Otherwise, the tool saves `abstained` and keeps the proposed label.
Thresholds depend on the workload. Test them on labeled records; scores do not establish accuracy.

Repeat the original command to resume. Use `--questions` for [named questions](https://github.com/Peterrallojay/sqlite-utils-jev/blob/v0.1.0/examples/questions.json).

## Check costs

```sh
sqlite-utils jev status decisions.sqlite
```

The example sends text to TypeSafe. Its one-cent allowance covers all runs that use this journal.
Reservations survive crashes. The tool does not retry failed requests automatically.
Actual charges can exceed the local estimate.

Defaults are `jev-1.13.0` and $0.042 per million input tokens, checked on 29 September 2026.
Before paid work, check [current pricing](https://docs.typesafe.ai/models).
For another model, pass its fixed ID with `--model` and its rate with `--input-price`.
If the price changes, pass the new rate with `--input-price`.

See the [reference](https://github.com/Peterrallojay/sqlite-utils-jev/blob/v0.1.0/docs/reference.md) for rate limits, file moves, retries and Python use.
See [verification](https://github.com/Peterrallojay/sqlite-utils-jev/blob/v0.1.0/VALIDATION.md) for tests and live measurements.
