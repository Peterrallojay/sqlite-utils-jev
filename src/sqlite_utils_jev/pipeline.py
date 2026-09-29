"""Read a table, classify selected text, write a separate result journal."""

from pathlib import Path
import sqlite3
import time
from collections.abc import Callable

from .client import Client, MODEL, PRICE, spent
from .batching import PACKING_VERSION, plan_batches
from .validation import JevError, canonical, digest, probability, validate_question


def identifier(name: str) -> str:
    if not isinstance(name, str) or not name or "\x00" in name:
        raise JevError("Table and column names must be nonempty strings")
    return '"' + name.replace('"', '""') + '"'


def read_rows(database: Path, table: str, key: str, text_columns: list[str], limit: int | None) -> list:
    if not text_columns or len(set(text_columns)) != len(text_columns):
        raise JevError("Select at least one text column, without duplicates")
    if limit is not None and (type(limit) is not int or limit <= 0):
        raise JevError("Limit must be a positive integer")
    quoted_table, quoted_key = identifier(table), identifier(key)
    for name in text_columns:
        identifier(name)
    db = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    try:
        db.execute("BEGIN")  # Freeze this selection; close before any network I/O.
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name=? AND type IN ('table','view')", (table,)).fetchone():
            raise JevError("Source table or view does not exist")
        columns = {r["name"] for r in db.execute(f"PRAGMA table_xinfo({quoted_table})")}
        if not {key, *text_columns} <= columns:
            raise JevError("Key or text column does not exist")
        if db.execute(f"SELECT 1 FROM {quoted_table} WHERE typeof({quoted_key}) NOT IN ('integer','text') LIMIT 1").fetchone():
            raise JevError("Source key must contain only non-null integers or strings")
        if db.execute(f"SELECT 1 FROM {quoted_table} GROUP BY {quoted_key} HAVING count(*)>1 LIMIT 1").fetchone():
            raise JevError("Source key must be unique")
        names = list(dict.fromkeys([key, *text_columns]))
        sql = f"SELECT {','.join(identifier(n) for n in names)} FROM {quoted_table} ORDER BY {quoted_key}"
        rows = [dict(row) for row in db.execute(sql + (" LIMIT ?" if limit else ""), (limit,) if limit else ())]
        if any(row[name] is not None and not isinstance(row[name], str) for row in rows for name in text_columns):
            raise JevError("Selected text columns must contain only strings or nulls")
        return rows
    finally:
        db.close()


def classify_table(database: str | Path, table: str, *, key: str,
                   text_columns: list[str], question: dict, state: str | Path,
                   budget_usd: str | float | None = None, api_key: str | None = None,
                   model: str = MODEL, input_price: str | float = PRICE,
                   min_probability: float = 0.75, min_confidence: float = 0.75,
                   limit: int | None = None, offline: bool = False, mode: str = "packed",
                   progress: Callable[[dict], None] | None = None) -> dict:
    """Repeat the same selection to resume; packed cache identity includes context."""
    validate_question(question)
    # The progress callback may mutate caller-owned configuration. Keep the
    # planned question and its stored identity fixed for the entire selection.
    question = {**question, "criteria": dict(question["criteria"])}
    if mode not in ("packed", "isolated"):
        raise JevError("Mode must be packed or isolated")
    if not probability(min_probability) or not probability(min_confidence):
        raise JevError("Acceptance thresholds must be finite numbers between zero and one")
    database, state = Path(database).resolve(strict=True), Path(state).resolve()
    if database == state or (state.exists() and database.samefile(state)):
        raise JevError("State must be a separate file from the source database")
    rows = read_rows(database, table, key, text_columns, limit)
    batches, empty = plan_batches(rows, text_columns, question, model, mode)
    counts = {"rows": 0, "accepted": 0, "abstained": 0, "empty": 0, "cached_rows": 0}
    identity = {"model": model, "question": question}
    if mode == "packed":
        identity["packing"] = PACKING_VERSION
    question_hash = digest(identity)
    with Client(state, budget_usd=budget_usd, api_key=api_key, model=model, input_price=input_price) as client:
        assert client.db is not None

        def report() -> dict:
            return {**counts, "total": len(rows), "remaining": len(rows) - counts["rows"],
                    "requests": client.requests, "cache_hits": client.cache_hits,
                    "accounted_usd": spent(client.db) / 1e9, "mode": mode,
                    "state": str(state), "question_hash": question_hash}

        def save(row: dict, request_hash: str | None, answer_key: str | None, answer: dict | None) -> str:
            label, proposed, score, confidence = None, None, None, None
            result_status = "empty"
            if answer is not None:
                proposed = answer["choice"]
                score, confidence = answer["probabilities"][proposed], answer["confidence"]
                accepted = score >= min_probability and confidence >= min_confidence
                label, result_status = (proposed, "accepted") if accepted else (None, "abstained")
            client.db.execute("""INSERT INTO results
                (source_db,source_table,source_key_column,source_key,question_hash,model,
                 request_hash,label,proposed_label,probability,confidence,status,
                 min_probability,min_confidence,processed_at,answer_key,mode)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(source_db,source_table,source_key_column,source_key,question_hash) DO UPDATE SET
                request_hash=excluded.request_hash,answer_key=excluded.answer_key,label=excluded.label,
                proposed_label=excluded.proposed_label,probability=excluded.probability,
                confidence=excluded.confidence,status=excluded.status,
                min_probability=excluded.min_probability,min_confidence=excluded.min_confidence,
                processed_at=excluded.processed_at""",
                (str(database), table, key, canonical(row[key]), question_hash, model, request_hash,
                 label, proposed, score, confidence, result_status,
                 min_probability, min_confidence, time.time(), answer_key, mode))
            return result_status

        with client.db:
            for row in empty:
                save(row, None, None, None)
        counts["rows"] = counts["empty"] = len(empty)
        if progress:
            progress(report())
        for batch in batches:
            if mode == "packed":
                result = client.evaluate_batch(batch.states, question, offline=offline)
                answers = result["answers"]
            else:
                result = client.evaluate(batch.states[0], question, offline=offline)
                answers = {"classification": result["answer"]}
            # No row from this batch becomes visible unless every answer validated
            # and every result write succeeds. A crash can replay the saved response.
            with client.db:
                outcomes = [save(row, result["request_hash"], answer_key, answers[answer_key])
                            for row, answer_key in zip(batch.rows, batch.answer_keys)]
            for outcome in outcomes:
                counts["rows"] += 1
                counts[outcome] += 1
            if result["cached"]:
                counts["cached_rows"] += len(batch.rows)
            if progress:
                progress(report())
        return report()
