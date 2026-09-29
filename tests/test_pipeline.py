from contextlib import closing
import copy
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from click.testing import CliRunner
from sqlite_utils.cli import cli

from sqlite_utils_jev import Client, JevError, classify_table, status
from sqlite_utils_jev.validation import validate_answer, validate_question
from test_client import MODEL, QUESTION, RESPONSE, Response


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.source, self.state = root / "source.db", root / "results.sqlite"
        self.question = root / "question.json"
        self.question.write_text(json.dumps(QUESTION))
        with closing(sqlite3.connect(self.source)) as db, db:
            db.executescript("CREATE TABLE tickets(id INTEGER PRIMARY KEY, body TEXT, private TEXT);"
                             "INSERT INTO tickets VALUES(1,'Refund please','never-send-me'),(2,'Broken login','private too'),(3,NULL,'secret');")
        self.http = patch("sqlite_utils_jev.client.urllib.request.build_opener").start()
        self.addCleanup(patch.stopall)
        self.http.return_value.open.side_effect = lambda *a, **k: Response(json.dumps(RESPONSE).encode())
        patch("sqlite_utils_jev.client.REQUEST_INTERVAL", 0).start()

    def run_job(self, **kwargs):
        options = dict(key="id", text_columns=["body"], question=QUESTION, state=self.state,
                       budget_usd=1, api_key="fake")
        options.update(kwargs)
        return classify_table(self.source, "tickets", **options)

    def saved(self):
        with closing(sqlite3.connect(self.state)) as db, db:
            db.row_factory = sqlite3.Row
            return [dict(row) for row in db.execute("SELECT * FROM results ORDER BY source_key")]

    def test_source_unchanged_empty_skipped_and_private_columns_omitted(self):
        before = self.source.read_bytes()
        summary = self.run_job()
        self.assertEqual(before, self.source.read_bytes())
        self.assertEqual((summary["rows"], summary["accepted"], summary["empty"], summary["requests"]), (3, 2, 1, 2))
        for call in self.http.return_value.open.call_args_list:
            state = json.loads(call.args[0].data)["state"]
            self.assertEqual(set(state), {"body"})
        empty = self.saved()[2]
        self.assertIsNone(empty["request_hash"])
        self.assertEqual(empty["status"], "empty")

    def test_threshold_change_reuses_answers_and_retains_proposal(self):
        self.run_job()
        summary = self.run_job(min_probability=0.99, offline=True, api_key="")
        self.assertEqual((summary["abstained"], summary["requests"], summary["cache_hits"]), (2, 0, 2))
        self.assertIsNone(self.saved()[0]["label"])
        self.assertEqual(self.saved()[0]["proposed_label"], "billing")
        self.assertEqual(self.http.return_value.open.call_count, 2)

    def test_confidence_and_probability_are_separate_gates(self):
        self.run_job(min_probability=0.95, min_confidence=0.9)
        self.assertEqual(self.saved()[0]["status"], "accepted")
        self.run_job(min_probability=0.95, min_confidence=0.91, offline=True)
        self.assertEqual(self.saved()[0]["status"], "abstained")

    def test_changed_row_calls_only_for_changed_input(self):
        self.run_job()
        with closing(sqlite3.connect(self.source)) as db, db:
            db.execute("UPDATE tickets SET body='Please refund again' WHERE id=1")
        summary = self.run_job()
        self.assertEqual((summary["requests"], summary["cache_hits"]), (1, 1))
        self.assertEqual(len(self.saved()), 3)
        with closing(sqlite3.connect(self.state)) as db, db:
            self.assertEqual(db.execute("SELECT count(*) FROM attempts").fetchone()[0], 3)

    def test_different_key_columns_keep_separate_row_identities(self):
        with closing(sqlite3.connect(self.source)) as db, db:
            db.execute("ALTER TABLE tickets ADD COLUMN alternate INTEGER")
            db.execute("UPDATE tickets SET alternate=CASE id WHEN 1 THEN 2 WHEN 2 THEN 1 ELSE 3 END")
        self.run_job()
        original = {row["source_key"]: row["request_hash"] for row in self.saved()}
        summary = self.run_job(key="alternate", limit=1, offline=True)
        self.assertEqual(summary["requests"], 0)
        rows = self.saved()
        self.assertEqual(len(rows), 4)
        self.assertEqual({row["source_key"]: row["request_hash"] for row in rows
                          if row["source_key_column"] == "id"}, original)
        alternate = next(row for row in rows if row["source_key_column"] == "alternate")
        self.assertEqual(alternate["source_key"], "1")
        self.assertEqual(alternate["request_hash"], original["2"])

    def test_stored_and_virtual_generated_columns(self):
        with closing(sqlite3.connect(self.source)) as db, db:
            db.executescript("""CREATE TABLE generated (
                id INTEGER PRIMARY KEY, body TEXT,
                stored TEXT GENERATED ALWAYS AS (lower(body)) STORED,
                virtual TEXT GENERATED ALWAYS AS (upper(body)) VIRTUAL,
                generated_key INTEGER GENERATED ALWAYS AS (id+100) VIRTUAL);
                INSERT INTO generated(id,body) VALUES(1,'Refund Please');""")
        classify_table(self.source, "generated", key="generated_key",
                       text_columns=["stored", "virtual"], question=QUESTION,
                       state=self.state, budget_usd=1, api_key="fake")
        payload = json.loads(self.http.return_value.open.call_args.args[0].data)
        self.assertEqual(payload["state"], {"stored": "refund please", "virtual": "REFUND PLEASE"})
        self.assertEqual(self.saved()[0]["source_key"], "101")

    def make_v1_journal(self):
        self.run_job()
        with closing(sqlite3.connect(self.state)) as db, db:
            db.executescript("""BEGIN;
                ALTER TABLE results RENAME TO current_results;
                CREATE TABLE results (
                    source_db TEXT NOT NULL, source_table TEXT NOT NULL, source_key TEXT NOT NULL,
                    question_hash TEXT NOT NULL, model TEXT NOT NULL, request_hash TEXT,
                    label TEXT, proposed_label TEXT, probability REAL, confidence REAL,
                    status TEXT NOT NULL, min_probability REAL NOT NULL, min_confidence REAL NOT NULL,
                    processed_at REAL NOT NULL,
                    PRIMARY KEY(source_db,source_table,source_key,question_hash));
                INSERT INTO results SELECT source_db,source_table,source_key,question_hash,model,
                    request_hash,label,proposed_label,probability,confidence,status,
                    min_probability,min_confidence,processed_at FROM current_results;
                DROP TABLE current_results;
                PRAGMA user_version=1;
                COMMIT;""")

    def test_v1_upgrade_preserves_history_and_rebuilds_identity_from_cache(self):
        self.make_v1_journal()
        before = self.state.read_bytes()
        totals = status(self.state)
        self.assertEqual(self.state.read_bytes(), before)  # Status never migrates.
        with closing(sqlite3.connect(self.state)) as db, db:
            attempts = db.execute("SELECT * FROM attempts ORDER BY id").fetchall()
        summary = self.run_job(offline=True)
        self.assertEqual(summary["requests"], 0)
        self.assertEqual(status(self.state), totals)
        with closing(sqlite3.connect(self.state)) as db, db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 4)
            self.assertEqual(db.execute("SELECT * FROM attempts ORDER BY id").fetchall(), attempts)
        rows = self.saved()
        self.assertEqual(len(rows), 6)
        self.assertEqual(sum(row["source_key_column"] == "" for row in rows), 3)
        self.assertEqual(sum(row["source_key_column"] == "id" for row in rows), 3)

    def test_failed_upgrade_rolls_back_without_losing_legacy_data(self):
        self.make_v1_journal()
        with closing(sqlite3.connect(self.state)) as db, db:
            db.execute("ALTER TABLE results RENAME COLUMN source_key TO broken_key")
        before = self.state.read_bytes()
        with self.assertRaises(sqlite3.OperationalError):
            with Client(self.state):
                pass
        self.assertEqual(self.state.read_bytes(), before)

    def test_future_journal_version_is_rejected_without_changes(self):
        self.run_job()
        with closing(sqlite3.connect(self.state)) as db, db:
            db.execute("PRAGMA user_version=999")
        before = self.state.read_bytes()
        with self.assertRaisesRegex(JevError, "Unsupported journal"):
            with Client(self.state):
                pass
        self.assertEqual(self.state.read_bytes(), before)

    def test_partial_run_resumes_completed_rows_and_stops_at_uncertain_attempt(self):
        self.http.return_value.open.side_effect = [Response(json.dumps(RESPONSE).encode()), TimeoutError()]
        with self.assertRaises(JevError):
            self.run_job()
        self.assertEqual(len(self.saved()), 2)
        with self.assertRaisesRegex(JevError, "Previous attempt"):
            self.run_job()
        self.assertEqual(self.http.return_value.open.call_count, 2)

    def test_duplicate_content_across_rows_is_paid_once(self):
        with closing(sqlite3.connect(self.source)) as db, db:
            db.execute("UPDATE tickets SET body='same' WHERE id IN (1,2)")
        summary = self.run_job()
        self.assertEqual((summary["requests"], summary["cache_hits"]), (1, 1))

    def test_source_and_state_cannot_be_same_file_or_hard_link(self):
        before = self.source.read_bytes()
        with self.assertRaisesRegex(JevError, "separate file"):
            self.run_job(state=self.source)
        alias = self.source.with_name("alias.db")
        os.link(self.source, alias)
        with self.assertRaisesRegex(JevError, "separate file"):
            self.run_job(state=alias)
        self.assertEqual(before, self.source.read_bytes())

    def test_invalid_column_or_text_type_fails_before_spending(self):
        with self.assertRaisesRegex(JevError, "does not exist"):
            self.run_job(text_columns=["missing"])
        with closing(sqlite3.connect(self.source)) as db, db:
            db.execute("UPDATE tickets SET body=? WHERE id=2", (b"binary",))
        with self.assertRaisesRegex(JevError, "strings or nulls"):
            self.run_job()
        self.http.return_value.open.assert_not_called()

    def test_missing_and_duplicate_keys_rejected_even_with_limit(self):
        with closing(sqlite3.connect(self.source)) as db, db:
            db.executescript("CREATE VIEW duplicates AS SELECT id,body FROM tickets UNION ALL SELECT id,body FROM tickets;")
        with self.assertRaisesRegex(JevError, "unique"):
            classify_table(self.source, "duplicates", key="id", text_columns=["body"], question=QUESTION, state=self.state, limit=1)
        with self.assertRaisesRegex(JevError, "non-null"):
            self.run_job(key="body")
        self.http.return_value.open.assert_not_called()

    def test_quoted_names_and_text_keys(self):
        with closing(sqlite3.connect(self.source)) as db, db:
            db.execute('CREATE TABLE "odd "" table" ("key value" TEXT, "text content" TEXT)')
            db.execute('INSERT INTO "odd "" table" VALUES(?,?)', ('a"1', 'refund'))
        classify_table(self.source, 'odd " table', key="key value", text_columns=["text content"],
                       question=QUESTION, state=self.state, budget_usd=1, api_key="fake")
        self.assertEqual(json.loads(self.saved()[0]["source_key"]), 'a"1')

    def test_invalid_threshold_and_limit(self):
        for options in ({"min_probability": float("nan")}, {"min_confidence": True}, {"limit": 0}):
            with self.subTest(options=options), self.assertRaises(JevError):
                self.run_job(**options)
        self.http.return_value.open.assert_not_called()

    def test_installed_plugin_cli_and_offline_rerun(self):
        runner = CliRunner()
        base = ["jev", "classify", str(self.source), "tickets", "--key", "id", "--text", "body",
                "--question", str(self.question), "--state", str(self.state), "--budget-usd", "1"]
        result = runner.invoke(cli, base, env={"TYPESAFE_API_KEY": "fake"})
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(json.loads(result.stdout)["requests"], 2)
        result = runner.invoke(cli, base + ["--offline"], env={"TYPESAFE_API_KEY": ""})
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(json.loads(result.stdout)["requests"], 0)
        result = runner.invoke(cli, ["jev", "status", str(self.state)])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(json.loads(result.stdout)["requests"], 2)

    def test_cli_budget_and_error_output(self):
        runner = CliRunner()
        result = runner.invoke(cli, ["jev", "budget", str(self.state), "--usd", "0.01"])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(json.loads(result.stdout)["budget_usd"], 0.01)
        result = runner.invoke(cli, ["jev", "retry", str(self.state), "missing"])
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("No failed", result.output)
        self.assertNotIn("Traceback", result.output)


class ValidationTests(unittest.TestCase):
    def test_malformed_answers_cannot_be_published(self):
        bad = []
        for changes in ({"confidence": True}, {"confidence": float("nan")}, {"choice": []},
                        {"choice": "other"}, {"probabilities": []},
                        {"probabilities": {"billing": 0.1, "technical": 0.9}},
                        {"probabilities": {"billing": 0.9, "technical": 0.9}},
                        {"probabilities": {"billing": 1}}, {"type": "noul"}):
            response = copy.deepcopy(RESPONSE)
            response["answers"]["classification"].update(changes)
            bad.append(response)
        bad.extend([None, [], {}, {**RESPONSE, "answers": []}, {**RESPONSE, "model": "jev-latest"}])
        for response in bad:
            with self.subTest(response=response), self.assertRaises(JevError):
                validate_answer(response, QUESTION, MODEL)

    def test_question_validation_before_network(self):
        for question in (None, [], {}, {**QUESTION, "type": "score"},
                         {**QUESTION, "instructions": ""}, {**QUESTION, "criteria": {"one": None}},
                         {**QUESTION, "criteria": {"one": 1, "two": None}}):
            with self.subTest(question=question), self.assertRaises(JevError):
                validate_question(question)


if __name__ == "__main__":
    unittest.main()
