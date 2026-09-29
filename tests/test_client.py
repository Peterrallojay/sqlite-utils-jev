import copy
import io
import http.client
import json
from pathlib import Path
import select
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import urllib.error

from sqlite_utils_jev import Client, JevError, allow_retry, set_budget, status
from sqlite_utils_jev.client import MODEL, NoRedirect

QUESTION = {"type": "choice", "instructions": "Which team?",
            "criteria": {"billing": "Payments", "technical": "Bugs"}}
STATE = {"body": "Refund the duplicate charge."}
RESPONSE = {"model": MODEL, "usage": {"input_tokens": 100}, "answers": {
    "classification": {"type": "choice", "choice": "billing", "confidence": 0.9,
                       "probabilities": {"billing": 0.95, "technical": 0.05}}}}


class Response(io.BytesIO):
    def __init__(self, body, code=200):
        super().__init__(body)
        self.code = code
        self.headers = {"x-request-id": "test-request"}


class ClientTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "results.sqlite"
        self.http = patch("sqlite_utils_jev.client.urllib.request.build_opener").start()
        self.addCleanup(patch.stopall)
        self.http.return_value.open.side_effect = lambda *a, **k: Response(json.dumps(RESPONSE).encode())
        patch("sqlite_utils_jev.client.REQUEST_INTERVAL", 0).start()

    def client(self, **kwargs):
        return Client(self.path, api_key="secret-for-tests", budget_usd=1, **kwargs)

    def attempts(self):
        with sqlite3.connect(self.path) as db:
            db.row_factory = sqlite3.Row
            return [dict(row) for row in db.execute("SELECT * FROM attempts ORDER BY id")]

    def test_request_shape_cached_after_restart_and_no_credential_persistence(self):
        with self.client() as client:
            first = client.evaluate(STATE, QUESTION)
            self.assertFalse(first["cached"])
        with Client(self.path, api_key="") as client:
            again = client.evaluate(dict(reversed(list(STATE.items()))), QUESTION, offline=True)
            self.assertTrue(again["cached"])
        self.assertEqual(self.http.return_value.open.call_count, 1)
        request = self.http.return_value.open.call_args.args[0]
        self.assertEqual(request.full_url, "https://api.typesafe.ai/v1/systemone")
        self.assertEqual(json.loads(request.data), {"model": MODEL, "state": STATE, "questions": {"classification": QUESTION}})
        self.assertNotIn("secret-for-tests", str(self.attempts()))
        self.assertEqual(self.attempts()[0]["request_id"], "test-request")
        self.assertAlmostEqual(status(self.path)["reported_cost_usd"], 0.0000042)

    def test_changed_text_question_or_model_requires_new_request(self):
        with self.client() as client:
            client.evaluate(STATE, QUESTION)
            client.evaluate({"body": "Login is broken"}, QUESTION)
            client.evaluate(STATE, {**QUESTION, "instructions": "Choose a department"})
        changed = copy.deepcopy(RESPONSE)
        changed["model"] = "jev-1.14.0"
        self.http.return_value.open.side_effect = lambda *a, **k: Response(json.dumps(changed).encode())
        with self.client(model="jev-1.14.0") as client:
            client.evaluate(STATE, QUESTION)
        self.assertEqual(len(self.attempts()), 4)

    def test_budget_survives_restart_and_cannot_be_silently_changed(self):
        with self.client() as client:
            client.evaluate(STATE, QUESTION)
        set_budget(self.path, "0.000004200")
        with Client(self.path, api_key="test") as client:
            client.evaluate(STATE, QUESTION)  # Cache works even when allowance is spent.
            with self.assertRaisesRegex(JevError, "budget exhausted"):
                client.evaluate({"body": "new"}, QUESTION)
        with self.assertRaisesRegex(JevError, "already exists"):
            with self.client():
                pass
        with self.assertRaisesRegex(JevError, "below recorded"):
            set_budget(self.path, "0.000000001")
        self.assertEqual(self.http.return_value.open.call_count, 1)

    def test_timeout_blocks_restart_retry_keeps_reservation(self):
        self.http.return_value.open.side_effect = TimeoutError()
        with self.client() as client:
            with self.assertRaisesRegex(JevError, "outcome unknown"):
                client.evaluate(STATE, QUESTION)
        previous = status(self.path)["unresolved_reservation_usd"]
        self.assertGreater(previous, 0)
        with self.client() as client:
            with self.assertRaisesRegex(JevError, "Previous attempt"):
                client.evaluate(STATE, QUESTION)
        self.assertEqual(self.http.return_value.open.call_count, 1)
        request_hash = self.attempts()[0]["request_hash"]
        allow_retry(self.path, request_hash)
        self.http.return_value.open.side_effect = lambda *a, **k: Response(json.dumps(RESPONSE).encode())
        with self.client() as client:
            client.evaluate(STATE, QUESTION)
        self.assertEqual(len(self.attempts()), 2)
        self.assertEqual(status(self.path)["unresolved_reservation_usd"], previous)
        with self.assertRaisesRegex(JevError, "No failed"):
            allow_retry(self.path, request_hash)

    def test_retry_permission_is_consumed_by_next_failed_attempt(self):
        self.http.return_value.open.side_effect = TimeoutError()
        for attempt in range(2):
            with self.client() as client:
                with self.assertRaises(JevError):
                    client.evaluate(STATE, QUESTION)
            if attempt == 0:
                allow_retry(self.path, self.attempts()[0]["request_hash"])
        with self.client() as client:
            with self.assertRaisesRegex(JevError, "Previous attempt"):
                client.evaluate(STATE, QUESTION)
        self.assertEqual(self.http.return_value.open.call_count, 2)

    def test_incomplete_http_body_is_an_uncertain_attempt(self):
        broken = Response(b"")
        broken.read = lambda: (_ for _ in ()).throw(http.client.IncompleteRead(b'{"model":'))
        self.http.return_value.open.side_effect = lambda *a, **k: broken
        with self.client() as client:
            with self.assertRaisesRegex(JevError, "outcome unknown"):
                client.evaluate(STATE, QUESTION)
        self.assertEqual(self.attempts()[0]["outcome"], "unknown")
        self.assertEqual(self.attempts()[0]["http_status"], 200)
        self.assertEqual(self.attempts()[0]["request_id"], "test-request")
        self.assertEqual(self.attempts()[0]["response_body"], '{"model":')
        self.assertGreater(status(self.path)["unresolved_reservation_usd"], 0)
        with self.client() as client:
            with self.assertRaisesRegex(JevError, "Previous attempt is unknown"):
                client.evaluate(STATE, QUESTION)
        self.assertEqual(self.http.return_value.open.call_count, 1)

    def test_oversized_numeric_answer_is_invalid_and_can_be_retried(self):
        invalid = copy.deepcopy(RESPONSE)
        invalid["answers"]["classification"]["confidence"] = 10**400
        self.http.return_value.open.side_effect = lambda *a, **k: Response(json.dumps(invalid).encode())
        with self.client() as client:
            with self.assertRaisesRegex(JevError, "Invalid response"):
                client.evaluate(STATE, QUESTION)
        attempt = self.attempts()[0]
        self.assertEqual(attempt["outcome"], "invalid")
        self.assertEqual(status(self.path)["input_tokens"], 100)
        self.assertEqual(len(status(self.path)["blocked_requests"]), 1)
        allow_retry(self.path, attempt["request_hash"])
        self.http.return_value.open.side_effect = lambda *a, **k: Response(json.dumps(RESPONSE).encode())
        with self.client() as client:
            self.assertFalse(client.evaluate(STATE, QUESTION)["cached"])

    def test_invalid_success_is_retained_and_charged_before_validation(self):
        invalid = {"usage": {"input_tokens": 456}}
        self.http.return_value.open.side_effect = lambda *a, **k: Response(json.dumps(invalid).encode())
        with self.client() as client:
            with self.assertRaisesRegex(JevError, "Invalid response"):
                client.evaluate(STATE, QUESTION)
        self.assertEqual(json.loads(self.attempts()[0]["response_body"]), invalid)
        self.assertEqual(status(self.path)["input_tokens"], 456)
        with self.client() as client:
            with self.assertRaisesRegex(JevError, "Previous attempt is invalid"):
                client.evaluate(STATE, QUESTION)
        self.assertEqual(self.http.return_value.open.call_count, 1)

    def test_raw_response_can_recover_after_crash_before_validation(self):
        with self.client() as client:
            client.evaluate(STATE, QUESTION)
        with sqlite3.connect(self.path) as db:
            db.execute("UPDATE attempts SET outcome='received',input_tokens=NULL,charge_nano=reservation_nano")
        with self.client() as client:
            self.assertTrue(client.evaluate(STATE, QUESTION, offline=True)["cached"])
        self.assertEqual(status(self.path)["input_tokens"], 100)
        self.assertEqual(self.http.return_value.open.call_count, 1)

    def test_http_error_is_saved_without_hidden_retry(self):
        self.http.return_value.open.side_effect = urllib.error.HTTPError(
            "https://api.typesafe.ai/v1/systemone", 429, "rate limited", {}, io.BytesIO(b'{"error":"slow down"}'))
        with self.client() as client:
            with self.assertRaisesRegex(JevError, "HTTP 429"):
                client.evaluate(STATE, QUESTION)
        self.assertEqual(self.attempts()[0]["http_status"], 429)
        self.assertEqual(self.attempts()[0]["response_body"], '{"error":"slow down"}')
        self.assertEqual(self.http.return_value.open.call_count, 1)

    def test_invalid_json_and_missing_usage_retain_reservation(self):
        self.http.return_value.open.side_effect = lambda *a, **k: Response(b"not JSON")
        with self.client() as client:
            with self.assertRaises(JevError):
                client.evaluate(STATE, QUESTION)
        self.assertGreater(status(self.path)["unresolved_reservation_usd"], 0)
        good = copy.deepcopy(RESPONSE)
        del good["usage"]
        self.http.return_value.open.side_effect = lambda *a, **k: Response(json.dumps(good).encode())
        with self.client() as client:
            self.assertEqual(client.evaluate({"body": "new"}, QUESTION)["answer"]["choice"], "billing")
        self.assertEqual(status(self.path)["reported_cost_usd"], 0)

    def test_preflight_no_key_no_budget_offline_oversize(self):
        with Client(self.path, api_key="") as client:
            with self.assertRaisesRegex(JevError, "TYPESAFE_API_KEY"):
                client.evaluate(STATE, QUESTION)
        with Client(self.path, api_key="test") as client:
            with self.assertRaisesRegex(JevError, "total budget"):
                client.evaluate(STATE, QUESTION)
            with self.assertRaisesRegex(JevError, "offline"):
                client.evaluate(STATE, QUESTION, offline=True)
            with self.assertRaisesRegex(JevError, "24,000 bytes"):
                client.evaluate({"body": "x" * 24_000}, QUESTION)
        self.http.return_value.open.assert_not_called()

    def test_refuses_unrelated_database_and_moving_alias(self):
        with sqlite3.connect(self.path) as db:
            db.execute("CREATE TABLE original(value)")
        before = self.path.read_bytes()
        with self.assertRaisesRegex(JevError, "Not a sqlite-utils-jev"):
            with self.client():
                pass
        self.assertEqual(before, self.path.read_bytes())
        with self.assertRaisesRegex(JevError, "pinned model"):
            Client(self.path, model="jev-latest")

    def test_status_does_not_write_or_expose_state(self):
        with self.client() as client:
            client.evaluate(STATE, QUESTION)
        before = self.path.read_bytes()
        report = status(self.path)
        self.assertEqual(before, self.path.read_bytes())
        self.assertNotIn(STATE["body"], str(report))

    def test_lock_prevents_second_process_and_is_released(self):
        script = """
import sys
from sqlite_utils_jev import Client, JevError
try:
    with Client(sys.argv[1]): pass
except JevError as exc:
    assert 'Another process' in str(exc)
    sys.exit(0)
sys.exit(1)
"""
        with self.client():
            result = subprocess.run([sys.executable, "-c", script, str(self.path)], capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
        with self.client():
            pass

    def test_killed_process_leaves_durable_reservation(self):
        script = """
import sys, time
from unittest.mock import patch
from sqlite_utils_jev import Client
def blocked(*args, **kwargs):
    print('dispatched', flush=True)
    time.sleep(60)
with patch('sqlite_utils_jev.client.urllib.request.build_opener') as http:
    http.return_value.open.side_effect=blocked
    with Client(sys.argv[1], budget_usd=1, api_key='fake') as client:
        client.evaluate({'body':'Refund the duplicate charge.'},
            {'type':'choice','instructions':'Which team?','criteria':{'billing':'Payments','technical':'Bugs'}})
"""
        child = subprocess.Popen([sys.executable, "-c", script, str(self.path)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            self.assertTrue(select.select([child.stdout], [], [], 10)[0], "Child never reached dispatch")
            self.assertEqual(child.stdout.readline().strip(), "dispatched")
        finally:
            child.kill()
            child.communicate(timeout=5)
        with self.client() as client:
            with self.assertRaisesRegex(JevError, "Previous attempt is pending"):
                client.evaluate(STATE, QUESTION)
        self.assertGreater(status(self.path)["unresolved_reservation_usd"], 0)
        self.http.return_value.open.assert_not_called()

    def test_pacing_reads_last_dispatch_after_restart(self):
        with self.client() as client:
            client.evaluate(STATE, QUESTION)
        with sqlite3.connect(self.path) as db:
            db.execute("UPDATE attempts SET started_at=100")
        with patch("sqlite_utils_jev.client.REQUEST_INTERVAL", 1), patch("sqlite_utils_jev.client.time.time", return_value=100.25), patch("sqlite_utils_jev.client.time.sleep") as sleep:
            with self.client() as client:
                client.evaluate({"body": "new"}, QUESTION)
        sleep.assert_called_once_with(0.75)

    def test_redirect_is_rejected(self):
        with self.assertRaisesRegex(JevError, "redirects"):
            NoRedirect().redirect_request(None, None, 302, "", {}, "https://elsewhere.example/")

    def test_invalid_money_fails_before_creating_files(self):
        for value in ("nan", "inf", "-1", "0", "0.0000000001", "1e100",
                      "1e9999999", "1.0000000000000000000000000000001"):
            with self.subTest(value=value), self.assertRaises(JevError):
                set_budget(self.path, value)
        self.assertFalse(self.path.exists())

    def test_malformed_credentials_fail_before_reserving_or_dispatching(self):
        for key in ("key\nextra", "key\rextra", "key\x00extra", "nönascii", 42):
            with self.subTest(key=key), Client(self.path, api_key=key, budget_usd=1) as client:
                with self.assertRaisesRegex(JevError, "API key"):
                    client.evaluate(STATE, QUESTION)
        self.assertEqual(self.attempts(), [])
        self.http.return_value.open.assert_not_called()

    def test_offline_cache_does_not_require_valid_credentials(self):
        with self.client() as client:
            client.evaluate(STATE, QUESTION)
        with Client(self.path, api_key="invalid\nkey") as client:
            self.assertTrue(client.evaluate(STATE, QUESTION, offline=True)["cached"])
            with self.assertRaisesRegex(JevError, "offline"):
                client.evaluate({"body": "not cached"}, QUESTION, offline=True)
        self.assertEqual(self.http.return_value.open.call_count, 1)

    def test_money_accepts_exact_nanodollars_at_storage_boundaries(self):
        set_budget(self.path, "0.000000001")
        with sqlite3.connect(self.path) as db:
            self.assertEqual(db.execute("SELECT budget_nano FROM settings").fetchone()[0], 1)
        set_budget(self.path, "9223372036.854775807")
        with sqlite3.connect(self.path) as db:
            self.assertEqual(db.execute("SELECT budget_nano FROM settings").fetchone()[0], 2**63 - 1)
        with self.assertRaises(JevError):
            set_budget(self.path, "9223372036.854775808")


if __name__ == "__main__":
    unittest.main()
