# sqlite-utils-jev

Classify SQLite text with [TypeSafe Jev](https://docs.typesafe.ai/).
Save answers and costs in a separate SQLite file, called the journal.
The tool does not change the source database.

Each API request contains one record and one or more Choice questions.
The tool runs up to eight requests at the same time by default.
Repeat a command to use saved answers and continue the work.

This preview requires Python 3.10 or later and macOS or Linux.
It has an MIT license. It is not available on PyPI.

## Install

```sh
git clone https://github.com/Peterrallojay/sqlite-utils-jev.git
cd sqlite-utils-jev
python3 -m venv .venv
source .venv/bin/activate
python -m pip install .
```

## Classify records

1. Create the example database. Use a file name that does not exist.

   ```sh
   sqlite-utils insert tickets.db tickets examples/tickets.csv --csv --pk id
   ```

2. Set the `TYPESAFE_API_KEY` environment variable.
3. Run the command:

   ```sh
   sqlite-utils jev classify tickets.db tickets \
     --key id --text subject --text body \
     --questions examples/questions.json \
     --state decisions.sqlite --budget-usd 1.00
   ```

The command sends selected text to TypeSafe. API charges apply.
The $1 allowance includes all runs that use this journal.
Actual charges can exceed this local estimate.

For one question, use `--question examples/routing.json` instead of `--questions`.
Use `--workers 1` for one request at a time.
Use `--offline` to permit saved answers only.

## Read results

```sh
sqlite-utils decisions.sqlite \
  "select source_key, question_name, label, status from results"
```

Each record has one result for each question.
The tool saves a label when probability and confidence are each at least 0.75.
Otherwise, it saves `abstained` and keeps the proposed label.
These thresholds do not measure accuracy.

## Check costs

```sh
sqlite-utils jev status decisions.sqlite
```

Use the same journal path to share the allowance across databases and runs.
The tool does not retry failed requests automatically.
Read the [reference](docs/reference.md) before you authorize a retry or change the allowance.

## Python and tests

See the [Python example](docs/reference.md#python) and the [test results](VALIDATION.md).

```sh
python -m pip install -e .
python -m unittest discover -s tests -v
```

The test suite makes no API calls.
