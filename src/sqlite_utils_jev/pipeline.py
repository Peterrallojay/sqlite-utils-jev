"""Read a table, classify selected text, write a separate result journal."""

from pathlib import Path
import sqlite3
import time

from .client import Client, MODEL, PRICE
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
                   limit: int | None = None, offline: bool = False) -> dict:
    """Resume by repeating the same call. Only new request content needs the API."""
    validate_question(question)
    if not probability(min_probability) or not probability(min_confidence):
        raise JevError("Acceptance thresholds must be finite numbers between zero and one")
    database, state = Path(database).resolve(strict=True), Path(state).resolve()
    if database == state or (state.exists() and database.samefile(state)):
        raise JevError("State must be a separate file from the source database")
    rows = read_rows(database, table, key, text_columns, limit)
    counts = {"rows": 0, "accepted": 0, "abstained": 0, "empty": 0}
    question_hash = digest({"model": model, "question": question})
    with Client(state, budget_usd=budget_usd, api_key=api_key, model=model, input_price=input_price) as client:
        assert client.db is not None
        for row in rows:
            text = {name: row[name] for name in text_columns}
            request_hash, label, proposed, score, confidence = None, None, None, None, None
            result_status = "empty"
            if any(value and value.strip() for value in text.values()):
                result = client.evaluate(text, question, offline=offline)
                request_hash, answer = result["request_hash"], result["answer"]
                proposed = answer["choice"]
                score, confidence = answer["probabilities"][proposed], answer["confidence"]
                accepted = score >= min_probability and confidence >= min_confidence
                label, result_status = (proposed, "accepted") if accepted else (None, "abstained")
            with client.db:
                client.db.execute("""INSERT INTO results
                    (source_db,source_table,source_key_column,source_key,question_hash,model,
                     request_hash,label,proposed_label,probability,confidence,status,
                     min_probability,min_confidence,processed_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(source_db,source_table,source_key_column,source_key,question_hash) DO UPDATE SET
                    request_hash=excluded.request_hash,label=excluded.label,
                    proposed_label=excluded.proposed_label,probability=excluded.probability,
                    confidence=excluded.confidence,status=excluded.status,
                    min_probability=excluded.min_probability,min_confidence=excluded.min_confidence,
                    processed_at=excluded.processed_at""",
                    (str(database), table, key, canonical(row[key]), question_hash, model, request_hash,
                     label, proposed, score, confidence, result_status,
                     min_probability, min_confidence, time.time()))
            counts["rows"] += 1
            counts[result_status] += 1
        return {**counts, "requests": client.requests, "cache_hits": client.cache_hits,
                "state": str(state), "question_hash": question_hash}
