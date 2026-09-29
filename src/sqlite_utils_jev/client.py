"""One local runner, one journal, no automatic paid retries."""

from contextlib import contextmanager
from dataclasses import dataclass
from decimal import Decimal, DecimalException, localcontext
import fcntl
import http.client
import json
import os
from pathlib import Path
import re
import sqlite3
import time
import urllib.error
import urllib.request

from .validation import JevError, canonical, digest, response_json, validate_answers, validate_questions

API = "https://api.typesafe.ai/v1/systemone"
MODEL = "jev-1.13.0"
PRICE = "0.042"  # USD / million input tokens; checked 2026-09-28.
APPLICATION_ID = 0x4A455631
MAX_PAYLOAD_BYTES = 24_000
REQUEST_INTERVAL = 1 / 18
TOKENS_PER_SECOND = 200_000

RESULTS_SCHEMA = """
CREATE TABLE results (
 source_db TEXT NOT NULL, source_table TEXT NOT NULL, source_key_column TEXT NOT NULL,
 source_key TEXT NOT NULL, question_hash TEXT NOT NULL, model TEXT NOT NULL, request_hash TEXT,
 label TEXT, proposed_label TEXT, probability REAL, confidence REAL,
 status TEXT NOT NULL, min_probability REAL NOT NULL, min_confidence REAL NOT NULL,
 processed_at REAL NOT NULL,
 PRIMARY KEY (source_db, source_table, source_key_column, source_key, question_hash)
);
"""

SCHEMA = """
CREATE TABLE settings (id INTEGER PRIMARY KEY CHECK(id=1), budget_nano INTEGER NOT NULL);
CREATE TABLE attempts (
 id INTEGER PRIMARY KEY, request_hash TEXT NOT NULL, request_json TEXT NOT NULL,
 started_at REAL NOT NULL, finished_at REAL, outcome TEXT NOT NULL,
 http_status INTEGER, request_id TEXT, response_body TEXT, error TEXT,
 input_tokens INTEGER, price_nano INTEGER NOT NULL,
 reservation_nano INTEGER NOT NULL, charge_nano INTEGER NOT NULL,
 retry_allowed INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX attempts_request ON attempts(request_hash, id);
""" + RESULTS_SCHEMA


def units(value: str | float, scale: int) -> int:
    try:
        number = Decimal(str(value))
        if not number.is_finite() or not 0 < number <= Decimal(2**63 - 1) / scale:
            raise ValueError
        # Preserve all supplied digits; the default Decimal precision can round
        # a fractional nanodollar into an apparently valid whole number.
        with localcontext() as context:
            context.prec = max(28, len(number.as_tuple().digits) + len(str(scale)))
            number *= scale
        if number <= 0 or number != number.to_integral_value():
            raise ValueError
        return int(number)
    except (DecimalException, ValueError):
        raise JevError("Money values must be positive, finite and representable in nanodollars") from None


def check_journal(db: sqlite3.Connection) -> None:
    if db.execute("PRAGMA application_id").fetchone()[0] != APPLICATION_ID:
        raise JevError("Not a sqlite-utils-jev journal; choose a separate state file")
    if db.execute("PRAGMA user_version").fetchone()[0] not in (1, 2, 4):
        raise JevError("Unsupported journal schema version")


def upgrade_journal(db: sqlite3.Connection) -> None:
    """Preserve draft-v1 results without guessing their missing key column."""
    if db.execute("PRAGMA user_version").fetchone()[0] == 1:
        # An empty key-column name is impossible for new jobs. Legacy results
        # remain inspectable, while reruns rebuild identified results from cache.
        db.executescript("""BEGIN IMMEDIATE;
            ALTER TABLE results RENAME TO results_v1;
            """ + RESULTS_SCHEMA + """
            INSERT INTO results
                (source_db,source_table,source_key_column,source_key,question_hash,model,
                 request_hash,label,proposed_label,probability,confidence,status,
                 min_probability,min_confidence,processed_at)
                SELECT source_db,source_table,'',source_key,question_hash,model,request_hash,
                       label,proposed_label,probability,confidence,status,
                       min_probability,min_confidence,processed_at FROM results_v1;
            DROP TABLE results_v1;
            PRAGMA user_version=2;
            COMMIT;
        """)

    if db.execute("PRAGMA user_version").fetchone()[0] == 2:
        # Preserve user views, indexes and triggers while extending the primary key.
        objects = db.execute("SELECT sql FROM sqlite_master WHERE tbl_name='results' "
                             "AND type IN ('index','trigger') AND sql IS NOT NULL").fetchall()
        legacy = db.execute("PRAGMA legacy_alter_table").fetchone()[0]
        db.execute("PRAGMA legacy_alter_table=ON")
        try:
            # Version 3 belongs to the unreleased packed-record experiment.
            db.executescript("""BEGIN IMMEDIATE;
                ALTER TABLE results RENAME TO results_v2;
                CREATE TABLE results (
                    source_db TEXT NOT NULL, source_table TEXT NOT NULL, source_key_column TEXT NOT NULL,
                    source_key TEXT NOT NULL, question_hash TEXT NOT NULL, model TEXT NOT NULL, request_hash TEXT,
                    label TEXT, proposed_label TEXT, probability REAL, confidence REAL,
                    status TEXT NOT NULL, min_probability REAL NOT NULL, min_confidence REAL NOT NULL,
                    processed_at REAL NOT NULL, question_name TEXT NOT NULL,
                    PRIMARY KEY(source_db,source_table,source_key_column,source_key,question_hash,question_name)
                );
                INSERT INTO results SELECT *, 'classification' FROM results_v2;
                DROP TABLE results_v2;
                CREATE INDEX IF NOT EXISTS attempts_started ON attempts(started_at);
            """)
            for row in objects:
                db.execute(row[0])
            db.execute("PRAGMA user_version=4")
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.execute(f"PRAGMA legacy_alter_table={legacy}")


@contextmanager
def journal(path: Path):
    """OS lock survives neither process exit nor crash; journal reservations do."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_fd = os.open(str(path) + ".lock", os.O_CREAT | os.O_RDWR, 0o600)
    with os.fdopen(lock_fd, "a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise JevError("Another process is using this journal; no request sent") from None
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
            os.close(fd)
        except FileExistsError:
            pass
        db = sqlite3.connect(path, timeout=5)
        db.row_factory = sqlite3.Row
        try:
            app_id = db.execute("PRAGMA application_id").fetchone()[0]
            if app_id == 0 and not db.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchone():
                db.executescript("BEGIN IMMEDIATE;\n" + SCHEMA +
                                 f"PRAGMA application_id={APPLICATION_ID}; PRAGMA user_version=2; COMMIT;")
            check_journal(db)
            upgrade_journal(db)
            yield db
        finally:
            db.close()


def spent(db: sqlite3.Connection) -> int:
    return db.execute("SELECT coalesce(sum(charge_nano),0) FROM attempts").fetchone()[0]


def set_budget(path: str | Path, usd: str | float) -> None:
    """Set the total journal allowance; never erase previous spending."""
    amount = units(usd, 1_000_000_000)
    with journal(Path(path).resolve()) as db, db:
        if amount < spent(db):
            raise JevError("Budget cannot be below recorded costs and unresolved reservations")
        db.execute("INSERT INTO settings VALUES(1,?) ON CONFLICT(id) DO UPDATE SET budget_nano=excluded.budget_nano", (amount,))


def allow_retry(path: str | Path, request_hash: str) -> None:
    """Authorize one retry of the latest failed attempt, retaining its charge."""
    path = Path(path).resolve(strict=True)
    with journal(path) as db, db:
        row = db.execute("SELECT * FROM attempts WHERE request_hash=? ORDER BY id DESC LIMIT 1", (request_hash,)).fetchone()
        if row is None or row["outcome"] in ("succeeded", "received"):
            raise JevError("No failed or interrupted attempt to retry")
        db.execute("UPDATE attempts SET retry_allowed=1 WHERE id=?", (row["id"],))


def status(path: str | Path) -> dict:
    """Read totals without credentials, raw input text or any writes."""
    path = Path(path).resolve(strict=True)
    db = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    try:
        check_journal(db)
        budget = db.execute("SELECT budget_nano FROM settings").fetchone()
        totals = db.execute("""SELECT count(*) AS requests, coalesce(sum(input_tokens),0) AS tokens,
            coalesce(sum(CASE WHEN input_tokens IS NOT NULL THEN charge_nano ELSE 0 END),0) AS known,
            coalesce(sum(CASE WHEN input_tokens IS NULL THEN charge_nano ELSE 0 END),0) AS unresolved
            FROM attempts""").fetchone()
        blocked = db.execute("""SELECT request_hash, outcome, http_status FROM attempts a
            WHERE id=(SELECT max(id) FROM attempts WHERE request_hash=a.request_hash)
            AND outcome NOT IN ('succeeded','received') AND retry_allowed=0""").fetchall()
        return {"budget_usd": budget[0] / 1e9 if budget else None,
                "requests": totals["requests"], "input_tokens": totals["tokens"],
                "reported_cost_usd": totals["known"] / 1e9,
                "unresolved_reservation_usd": totals["unresolved"] / 1e9,
                "blocked_requests": [dict(row) for row in blocked]}
    finally:
        db.close()


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise JevError("Credential-bearing redirects are disabled")


class BudgetExhausted(JevError):
    """No room for another reservation; in-flight usage may release room."""


@dataclass(frozen=True)
class PendingRequest:
    id: int
    request_hash: str
    body: bytes


@dataclass(frozen=True)
class HttpResult:
    status: int | None
    request_id: str | None
    body: str | None
    error: str | None = None


def make_payload(state: dict, questions: dict, model: str) -> dict:
    validate_questions(questions)
    if not isinstance(state, dict) or not state:
        raise JevError("State must be a nonempty JSON object")
    payload = {"model": model, "state": state, "questions": questions}
    try:
        encoded = canonical(payload).encode("utf-8")
    except (ValueError, TypeError, RecursionError, UnicodeError):
        raise JevError("State and questions must contain valid finite JSON data") from None
    def check_keys(value):
        if isinstance(value, dict):
            if any(not isinstance(key, str) for key in value):
                raise JevError("JSON object keys must be strings")
            for child in value.values():
                check_keys(child)
        elif isinstance(value, (list, tuple)):
            for child in value:
                check_keys(child)
    try:
        check_keys(state)
        snapshot = json.loads(encoded)
    except (ValueError, TypeError, RecursionError):
        raise JevError("State and questions must contain valid finite JSON data") from None
    if len(encoded) > MAX_PAYLOAD_BYTES:
        raise JevError("Request exceeds 24,000 bytes; shorten the selected text (nothing was truncated)")
    return snapshot  # Freeze mutable caller-owned JSON before dispatch.


class Client:
    """Use as a context manager. One thread/process per journal at a time."""

    def __init__(self, state: str | Path, *, budget_usd: str | float | None = None,
                 api_key: str | None = None, model: str = MODEL,
                 input_price: str | float = PRICE):
        if not isinstance(model, str) or not re.fullmatch(r"jev-\d+\.\d+\.\d+", model):
            raise JevError("Use a pinned model such as jev-1.13.0, not a moving alias")
        self.path = Path(state).resolve()
        self.model = model
        self.key = api_key if api_key is not None else os.environ.get("TYPESAFE_API_KEY", "")
        self.price = units(input_price, 1000)
        self.budget = units(budget_usd, 1_000_000_000) if budget_usd is not None else None
        self.requests = 0
        self.cache_hits = 0
        self.db: sqlite3.Connection | None = None

    def __enter__(self):
        if self.db is not None:
            raise JevError("Client is already open")
        self._journal = journal(self.path)
        self.db = self._journal.__enter__()
        try:
            old = self.db.execute("SELECT budget_nano FROM settings").fetchone()
            if old and self.budget is not None and old[0] != self.budget:
                raise JevError("Budget already exists; use the budget command to change its total")
            if not old and self.budget is not None:
                with self.db:
                    self.db.execute("INSERT INTO settings VALUES(1,?)", (self.budget,))
        except BaseException:
            self.__exit__(None, None, None)
            raise
        return self

    def __exit__(self, *args):
        self._journal.__exit__(*args)
        self.db = None

    def _finish(self, row: sqlite3.Row, questions: dict) -> dict:
        assert self.db is not None
        try:
            response = response_json(row["response_body"])
        except (ValueError, TypeError, RecursionError):
            response = None
        usage = response.get("usage") if isinstance(response, dict) else None
        tokens = usage.get("input_tokens") if isinstance(usage, dict) else None
        if type(tokens) is not int or not 0 <= tokens <= (2**63 - 1) // row["price_nano"]:
            tokens = None
        # Raw body was committed first. Settle usage even if semantic validation fails.
        if row["input_tokens"] is None and tokens is not None:
            with self.db:
                self.db.execute("UPDATE attempts SET input_tokens=?, charge_nano=? WHERE id=?",
                                (tokens, tokens * row["price_nano"], row["id"]))
        try:
            answer = validate_answers(response, questions, self.model)
        except JevError as exc:
            with self.db:
                self.db.execute("UPDATE attempts SET outcome='invalid',error=? WHERE id=?", (str(exc), row["id"]))
            raise JevError(f"Invalid response retained for {row['request_hash']}: {exc}") from None
        with self.db:
            self.db.execute("UPDATE attempts SET outcome='succeeded' WHERE id=?", (row["id"],))
        return answer

    def evaluate(self, state: dict, question: dict, *, offline: bool = False) -> dict:
        """Backward-compatible single-question interface."""
        result = self.evaluate_questions(state, {"classification": question}, offline=offline)
        return {"request_hash": result["request_hash"], "answer": result["answers"]["classification"],
                "cached": result["cached"]}

    def evaluate_questions(self, state: dict, questions: dict, *, offline: bool = False) -> dict:
        """Evaluate named Choice questions against one isolated record."""
        payload = make_payload(state, questions, self.model)
        prepared = self._prepare(payload, offline=offline)
        if isinstance(prepared, PendingRequest):
            return self._complete(prepared, self._send(prepared), payload["questions"])
        return prepared

    def _prepare(self, payload: dict, *, offline: bool) -> PendingRequest | dict:
        if self.db is None:
            raise JevError("Use Client inside a with block")
        encoded = canonical(payload).encode("utf-8")
        request_hash = digest(payload)
        row = self.db.execute("SELECT * FROM attempts WHERE request_hash=? ORDER BY id DESC LIMIT 1", (request_hash,)).fetchone()
        if row and row["outcome"] in ("succeeded", "received") and row["http_status"] == 200:
            answer = self._finish(row, payload["questions"])
            self.cache_hits += 1
            return {"request_hash": request_hash, "answers": answer, "cached": True}
        if row and not row["retry_allowed"]:
            raise JevError(f"Previous attempt is {row['outcome']}; inspect status, then explicitly authorize retry for {request_hash}")
        if offline:
            raise JevError(f"No saved successful answer for {request_hash}; offline mode sent no request")
        if not self.key:
            raise JevError("Set TYPESAFE_API_KEY before making a new request")
        if not isinstance(self.key, str) or any(not 33 <= ord(char) <= 126 for char in self.key):
            raise JevError("API key must contain only printable ASCII characters without whitespace")
        reservation = (len(encoded) + 4096) * self.price
        budget = self.db.execute("SELECT budget_nano FROM settings").fetchone()
        if budget is None:
            raise JevError("Set a total budget with --budget-usd or the budget command")
        if spent(self.db) + reservation > budget[0]:
            raise BudgetExhausted("Persistent budget exhausted; successful results remain saved")
        self._pace(len(encoded) + 4096)
        with self.db:
            attempt_id = self.db.execute("""INSERT INTO attempts
                (request_hash,request_json,started_at,outcome,price_nano,reservation_nano,charge_nano)
                VALUES(?,?,?,'pending',?,?,?)""",
                (request_hash, encoded.decode(), time.time(), self.price, reservation, reservation)).lastrowid
        self.requests += 1
        return PendingRequest(attempt_id, request_hash, encoded)

    def _pace(self, tokens: int) -> None:
        assert self.db is not None
        while True:
            now = time.time()
            recent = self.db.execute("""SELECT started_at, coalesce(input_tokens, reservation_nano/price_nano)
                FROM attempts WHERE started_at>? ORDER BY started_at""", (now - 1,)).fetchall()
            delay = max(0, recent[-1][0] + REQUEST_INTERVAL - now) if recent else 0
            if recent and sum(row[1] for row in recent) + tokens > TOKENS_PER_SECOND:
                delay = max(delay, recent[0][0] + 1 - now)
            if delay <= 0:
                return
            time.sleep(delay)
            # Recheck token windows, but one request-spacing sleep is sufficient.
            if sum(row[1] for row in recent) + tokens <= TOKENS_PER_SECOND:
                return

    def _send(self, pending: PendingRequest) -> HttpResult:
        """Worker-only I/O: never reads or writes the SQLite connection."""
        request = urllib.request.Request(API, data=pending.body, headers={
            "Content-Type": "application/json", "Authorization": "Bearer " + self.key})
        code, request_id, raw = None, None, None
        try:
            try:
                response = urllib.request.build_opener(NoRedirect()).open(request, timeout=45)
            except urllib.error.HTTPError as exc:
                response = exc
            with response:
                code = response.code
                request_id = response.headers.get("x-request-id") or response.headers.get("request-id")
                raw = response.read().decode("utf-8", errors="replace")
        except (OSError, http.client.HTTPException, JevError) as exc:
            if isinstance(exc, http.client.IncompleteRead):
                raw = exc.partial.decode("utf-8", errors="replace")
            return HttpResult(code, request_id, raw, type(exc).__name__)
        return HttpResult(code, request_id, raw)

    def _complete(self, pending: PendingRequest, response: HttpResult, questions: dict) -> dict:
        assert self.db is not None
        outcome = "unknown" if response.error else "received" if response.status == 200 else "http_error"
        with self.db:
            self.db.execute("""UPDATE attempts SET http_status=?,request_id=?,response_body=?,error=?,
                finished_at=?,outcome=? WHERE id=?""",
                (response.status, response.request_id, response.body, response.error,
                 time.time(), outcome, pending.id))
        if response.error:
            raise JevError(f"Request outcome unknown; reservation retained for {pending.request_hash}")
        if response.status != 200:
            raise JevError(f"HTTP {response.status}; response and reservation retained for {pending.request_hash}. No automatic retry.")
        row = self.db.execute("SELECT * FROM attempts WHERE id=?", (pending.id,)).fetchone()
        return {"request_hash": pending.request_hash, "answers": self._finish(row, questions), "cached": False}
