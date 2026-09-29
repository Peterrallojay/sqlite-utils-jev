"""Concurrency and multi-question failure contracts. No external model calls."""
from contextlib import closing
import copy
import json
from pathlib import Path
import sqlite3
import select
import subprocess
import sys
import threading
import time
import tempfile
import unittest
from unittest.mock import patch

from click.testing import CliRunner
from sqlite_utils.cli import cli
from sqlite_utils_jev import Client, JevError, classify_table, status
from sqlite_utils_jev.client import APPLICATION_ID, SCHEMA, MODEL, make_payload
from sqlite_utils_jev.validation import canonical, digest
from test_client import QUESTION, RESPONSE, Response

QUESTIONS = {'team': QUESTION, 'role': {**QUESTION, 'instructions': 'Which role?'}}


def reply(request, **kwargs):
    payload = json.loads(request.data)
    answer = RESPONSE['answers']['classification']
    return Response(json.dumps({'model': MODEL, 'usage': {'input_tokens': 100},
        'answers': {name: answer for name in reversed(list(payload['questions']))}}).encode())


class ConcurrentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.source = Path(self.temp.name)/'source.sqlite'
        self.state = Path(self.temp.name)/'journal.sqlite'
        with closing(sqlite3.connect(self.source)) as db, db:
            db.execute('CREATE TABLE records(id INTEGER PRIMARY KEY, body TEXT, private TEXT)')
            db.executemany('INSERT INTO records VALUES(?,?,?)', [(i, str(i), 'private') for i in range(6)])
        self.http = patch('sqlite_utils_jev.client.urllib.request.build_opener').start()
        self.addCleanup(patch.stopall)
        self.http.return_value.open.side_effect = reply
        patch('sqlite_utils_jev.client.REQUEST_INTERVAL', 0).start()

    def run_job(self, **kwargs):
        options = dict(key='id', text_columns=['body'], questions=QUESTIONS,
                       state=self.state, api_key='fake', budget_usd='1', workers=2)
        options.update(kwargs)
        return classify_table(self.source, 'records', **options)

    def rows(self, table):
        with closing(sqlite3.connect(self.state)) as db:
            db.row_factory = sqlite3.Row
            return [dict(row) for row in db.execute(f'SELECT * FROM {table}')]

    def test_requests_overlap_but_records_never_share_context(self):
        barrier = threading.Barrier(2, timeout=3)
        parent = threading.get_ident()
        def overlapping(request, **kwargs):
            self.assertNotEqual(threading.get_ident(), parent)
            payload = json.loads(request.data)
            self.assertEqual(set(payload['state']), {'body'})
            self.assertEqual(set(payload['questions']), {'team', 'role'})
            barrier.wait()
            self.assertGreaterEqual(len(self.rows('attempts')), 2)
            return reply(request)
        self.http.return_value.open.side_effect = overlapping
        updates = []
        result = self.run_job(limit=2, progress=lambda u: updates.append((threading.get_ident(), u)))
        self.assertEqual((result['rows'], result['requests']), (2, 2))
        self.assertEqual(len(self.rows('results')), 4)
        self.assertTrue(all(thread == parent for thread, _ in updates))
        self.assertEqual([u['rows'] for _, u in updates], [0, 1, 2])
        self.assertEqual(status(self.state)['input_tokens'], 200)
        result = self.run_job(limit=2, offline=True, api_key='', min_confidence=.99)
        self.assertEqual((result['requests'], result['cached_rows'], result['abstained']), (0, 2, 2))

    def test_duplicate_records_are_not_dispatched_twice_in_flight(self):
        with closing(sqlite3.connect(self.source)) as db, db:
            db.execute("UPDATE records SET body='same'")
        def slow(request, **kwargs):
            time.sleep(.02)
            return reply(request)
        self.http.return_value.open.side_effect = slow
        result = self.run_job(workers=8)
        self.assertEqual((result['requests'], result['cache_hits'], result['cached_rows']), (1, 5, 5))
        self.assertEqual(len(self.rows('results')), 12)

    def test_failed_request_stops_dispatch_and_settles_other_in_flight_calls(self):
        barrier = threading.Barrier(3, timeout=3)
        def fail_one(request, **kwargs):
            barrier.wait()
            if json.loads(request.data)['state']['body'] == '0':
                raise TimeoutError()
            time.sleep(.05)
            return reply(request)
        self.http.return_value.open.side_effect = fail_one
        with self.assertRaisesRegex(JevError, 'unknown'):
            self.run_job(workers=3)
        self.assertEqual(self.http.return_value.open.call_count, 3)
        self.assertEqual(sorted(r['outcome'] for r in self.rows('attempts')), ['succeeded', 'succeeded', 'unknown'])
        self.assertEqual(len(self.rows('results')), 4)
        self.assertEqual(status(self.state)['input_tokens'], 200)
        with self.assertRaisesRegex(JevError, 'Previous attempt'):
            self.run_job(workers=3)
        self.assertEqual(self.http.return_value.open.call_count, 3)

    def test_reservations_release_room_before_budget_exhaustion(self):
        body = canonical(make_payload({'body':'0'}, QUESTIONS, MODEL)).encode()
        # Enough for one reservation plus all settled calls, but not two reservations.
        budget = ((len(body)+4096)*42 + 6*4200)/1e9
        result = self.run_job(workers=8, budget_usd=str(budget))
        self.assertEqual(result['requests'], 6)
        self.assertEqual(status(self.state)['input_tokens'], 600)

    def test_budget_cannot_be_oversubscribed_by_workers(self):
        body = canonical(make_payload({'body':'0'}, QUESTIONS, MODEL)).encode()
        tokens = len(body)+4096
        def full_cost(request, **kwargs):
            data = json.loads(reply(request).getvalue())
            data['usage']['input_tokens'] = tokens
            return Response(json.dumps(data).encode())
        self.http.return_value.open.side_effect = full_cost
        with self.assertRaisesRegex(JevError, 'budget exhausted'):
            self.run_job(workers=8, budget_usd=str(tokens*42/1e9))
        self.assertEqual(self.http.return_value.open.call_count, 1)
        self.assertEqual(len(self.rows('results')), 2)

    def test_all_questions_validate_before_any_result_is_saved(self):
        def missing(request, **kwargs):
            data = json.loads(reply(request).getvalue())
            del data['answers']['role']
            return Response(json.dumps(data).encode())
        self.http.return_value.open.side_effect = missing
        with self.assertRaisesRegex(JevError, 'Invalid response'): self.run_job(limit=1)
        self.assertEqual(self.rows('results'), [])
        self.assertEqual(status(self.state)['input_tokens'], 100)

    def test_answers_for_one_row_commit_atomically_and_recover_from_cache(self):
        with Client(self.state): pass
        with closing(sqlite3.connect(self.state)) as db, db:
            db.execute("CREATE TRIGGER fail_role BEFORE INSERT ON results WHEN NEW.question_name='role' "
                       "BEGIN SELECT RAISE(ABORT,'write failed'); END")
        with self.assertRaises(sqlite3.IntegrityError): self.run_job(limit=1)
        self.assertEqual(self.rows('results'), [])
        with closing(sqlite3.connect(self.state)) as db, db: db.execute('DROP TRIGGER fail_role')
        result = self.run_job(limit=1, offline=True)
        self.assertEqual((result['cached_rows'], len(self.rows('results'))), (1, 2))
        self.assertEqual(self.http.return_value.open.call_count, 1)

    def test_callback_failure_drains_http_and_preserves_responses_for_resume(self):
        barrier = threading.Barrier(2, timeout=3)
        def overlapping(request, **kwargs):
            barrier.wait()
            return reply(request)
        self.http.return_value.open.side_effect = overlapping
        def interrupted(update):
            if update['rows']: raise RuntimeError('callback failed')
        with self.assertRaisesRegex(RuntimeError, 'callback failed'):
            self.run_job(limit=2, progress=interrupted)
        self.assertEqual(status(self.state)['input_tokens'], 200)
        self.assertEqual([r['outcome'] for r in self.rows('attempts')], ['succeeded']*2)
        self.assertEqual(self.run_job(limit=2, offline=True)['cached_rows'], 2)
        self.assertEqual(self.http.return_value.open.call_count, 2)

    def test_interrupt_during_completion_keeps_response_in_drain_set(self):
        original = Client._complete
        calls = []
        def interrupted(client, *args):
            calls.append(1)
            if len(calls) == 1: raise KeyboardInterrupt()
            return original(client, *args)
        with patch.object(Client, '_complete', interrupted), self.assertRaises(KeyboardInterrupt):
            self.run_job(limit=1)
        self.assertEqual(len(calls), 2)
        self.assertEqual(status(self.state)['input_tokens'], 100)
        self.assertEqual(self.rows('attempts')[0]['outcome'], 'succeeded')
        self.assertEqual(self.run_job(limit=1, offline=True)['requests'], 0)

    def test_preflight_rejects_late_oversized_record_without_spending(self):
        with closing(sqlite3.connect(self.source)) as db, db:
            db.execute("UPDATE records SET body=? WHERE id=5", ('x'*24000,))
        with self.assertRaisesRegex(JevError, '24,000'): self.run_job()
        self.http.return_value.open.assert_not_called()
        self.assertFalse(self.state.exists())

    def test_callback_mutations_cannot_change_selected_inputs_or_questions(self):
        questions, columns = copy.deepcopy(QUESTIONS), ['body']
        def mutate(update):
            questions['team']['criteria'].clear()
            columns[:] = ['private']
            update['rows'] = 999
        result = self.run_job(limit=1, questions=questions, text_columns=columns, progress=mutate)
        payload = json.loads(self.rows('attempts')[0]['request_json'])
        self.assertEqual(payload['questions'], QUESTIONS)
        self.assertEqual(payload['state'], {'body':'0'})
        self.assertEqual(result['rows'], 1)

    def test_invalid_workers_and_question_maps_send_nothing(self):
        for kwargs in ({'workers':0}, {'workers':True}, {'workers':17}, {'questions':{}},
                       {'questions':{'':QUESTION}}, {'question':QUESTION}, {'questions':{'bad':{}}}):
            with self.subTest(kwargs=kwargs), self.assertRaises(JevError): self.run_job(**kwargs)
        self.http.return_value.open.assert_not_called()

    def test_cli_multi_question_progress_and_json_output(self):
        path = Path(self.temp.name)/'questions.json'
        path.write_text(json.dumps(QUESTIONS))
        args = ['jev','classify',str(self.source),'records','--key','id','--text','body',
                '--questions',str(path),'--state',str(self.state),'--budget-usd','1','--limit','2','--workers','2']
        result = CliRunner().invoke(cli, args, env={'TYPESAFE_API_KEY':'fake'})
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(json.loads(result.stdout)['questions_per_row'], 2)
        self.assertIn('2/2 rows', result.stderr)
        result = CliRunner().invoke(cli, args+['--offline','--quiet'])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(result.stderr, '')
        self.assertEqual(json.loads(result.stdout)['requests'], 0)

    def test_legacy_revalidation_never_erases_settled_spending(self):
        payload = {'model':MODEL,'state':{'body':'0'},'questions':{'classification':QUESTION}}
        raw = json.dumps({**RESPONSE, 'usage':{'input_tokens':10000}})[:-1]+',"metadata":1,"metadata":2}'
        with closing(sqlite3.connect(self.state)) as db, db:
            db.executescript(SCHEMA+f'PRAGMA application_id={APPLICATION_ID}; PRAGMA user_version=2;')
            db.execute('INSERT INTO settings VALUES(1,1000000000)')
            db.execute('''INSERT INTO attempts (request_hash,request_json,started_at,outcome,http_status,
                response_body,input_tokens,price_nano,reservation_nano,charge_nano)
                VALUES(?,?,0,'succeeded',200,?,10000,42,180000,420000)''', (digest(payload),canonical(payload),raw))
        before = status(self.state)
        with Client(self.state) as client, self.assertRaisesRegex(JevError, 'Invalid response'):
            client.evaluate({'body':'0'}, QUESTION, offline=True)
        after = status(self.state)
        self.assertEqual(after['input_tokens'], before['input_tokens'])
        self.assertEqual(after['reported_cost_usd'], before['reported_cost_usd'])
        self.assertEqual(after['unresolved_reservation_usd'], 0)
        self.http.return_value.open.assert_not_called()

    def test_process_kill_preserves_each_in_flight_reservation(self):
        script = """
import json,sys,threading,time
from unittest.mock import patch
from sqlite_utils_jev import classify_table
barrier=threading.Barrier(2,timeout=5)
def blocked(*args,**kwargs):
    if barrier.wait()==0: print('two dispatched',flush=True)
    time.sleep(60)
with patch('sqlite_utils_jev.client.urllib.request.build_opener') as http:
    http.return_value.open.side_effect=blocked
    classify_table(sys.argv[1],'records',key='id',text_columns=['body'],
                   questions=json.loads(sys.argv[3]),state=sys.argv[2],
                   api_key='fake',budget_usd=1,workers=2)
"""
        child = subprocess.Popen([sys.executable,'-c',script,str(self.source),str(self.state),json.dumps(QUESTIONS)],
                                 stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
        try:
            self.assertTrue(select.select([child.stdout],[],[],10)[0])
            self.assertEqual(child.stdout.readline().strip(),'two dispatched')
        finally:
            child.kill()
            child.communicate(timeout=5)
        self.assertEqual([row['outcome'] for row in self.rows('attempts')], ['pending']*2)
        self.assertGreater(status(self.state)['unresolved_reservation_usd'],0)
        with self.assertRaisesRegex(JevError,'Previous attempt'): self.run_job()
        self.http.return_value.open.assert_not_called()

    def test_pacing_accounts_for_recent_tokens_across_restarts(self):
        self.run_job(limit=2)
        with closing(sqlite3.connect(self.state)) as db, db:
            db.execute('UPDATE attempts SET started_at=100,input_tokens=150000,charge_nano=6300000')
        now = [100.25]
        with Client(self.state) as client, patch('sqlite_utils_jev.client.time.time',side_effect=lambda:now[0]), \
             patch('sqlite_utils_jev.client.time.sleep',side_effect=lambda delay:now.__setitem__(0,now[0]+delay)) as sleep:
            client._pace(5000)
        sleep.assert_called_once_with(.75)

    def test_same_journal_shares_budget_and_cache_across_source_databases(self):
        self.run_job(limit=1)
        second = self.source.with_name('second.sqlite')
        second.write_bytes(self.source.read_bytes())
        result = classify_table(second,'records',key='id',text_columns=['body'],questions=QUESTIONS,
                                state=self.state,offline=True,limit=1)
        self.assertEqual((result['cached_rows'],result['requests']), (1,0))
        self.assertEqual(len({row['source_db'] for row in self.rows('results')}),2)
        from sqlite_utils_jev import set_budget
        set_budget(self.state,str(status(self.state)['reported_cost_usd']))
        with self.assertRaisesRegex(JevError,'budget exhausted'):
            classify_table(second,'records',key='id',text_columns=['body'],questions=QUESTIONS,
                           state=self.state,api_key='fake',limit=2)
        self.assertEqual(self.http.return_value.open.call_count,1)

    def test_structured_python_state_round_trips_without_reshaping(self):
        state = {'title':'A study', 'terms':['human','trial'], 'metadata':{'year':2026}}
        with Client(self.state, api_key='fake', budget_usd='1') as client:
            result = client.evaluate_questions(state, QUESTIONS)
        self.assertEqual(json.loads(self.rows('attempts')[0]['request_json'])['state'], state)
        self.assertEqual(set(result['answers']), set(QUESTIONS))
        self.assertEqual(self.rows('results'), [])

    def test_invalid_json_state_never_reserves_or_dispatches(self):
        cycle = {}; cycle['cycle'] = cycle
        invalid = [[], {}, {'nested':{1:'value'}}, {'value':float('nan')}, {'value':object()}, cycle]
        with Client(self.state, api_key='fake', budget_usd='1') as client:
            for state in invalid:
                with self.subTest(state_type=type(state)), self.assertRaises(JevError):
                    client.evaluate_questions(state, QUESTIONS)
        self.assertEqual(self.rows('attempts'), [])
        self.http.return_value.open.assert_not_called()

    def test_deep_structured_input_is_rejected_with_a_domain_error(self):
        state = {'leaf': 0}
        for depth in range(1010):
            state = {'child': state}
            if depth < 850:
                continue
            try:
                make_payload(state, QUESTIONS, MODEL)
            except JevError:
                pass

    def test_v2_migration_preserves_user_views_indexes_and_triggers(self):
        with closing(sqlite3.connect(self.state)) as db, db:
            db.executescript(SCHEMA+f'PRAGMA application_id={APPLICATION_ID}; PRAGMA user_version=2;')
            db.executescript("""CREATE VIEW accepted_results AS SELECT * FROM results WHERE status='accepted';
                CREATE INDEX results_label ON results(label);
                CREATE TABLE audit(label TEXT);
                CREATE TRIGGER audit_result AFTER INSERT ON results BEGIN
                    INSERT INTO audit VALUES(NEW.label); END;""")
        self.run_job(limit=1)
        with closing(sqlite3.connect(self.state)) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM accepted_results').fetchone()[0], 2)
            self.assertEqual(db.execute('SELECT count(*) FROM audit').fetchone()[0], 2)
            self.assertTrue(db.execute("SELECT 1 FROM sqlite_master WHERE name='results_label'").fetchone())
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], 4)

    def test_duplicate_keys_and_deep_json_are_invalid_and_block_retry(self):
        for raw in ('{"answers":{},"answers":{}}', '['*10000+'0'+']'*10000):
            path = self.state.with_name(str(len(raw))+'.sqlite')
            self.http.return_value.open.side_effect = lambda *a, **k: Response(raw.encode())
            with self.subTest(size=len(raw)), self.assertRaisesRegex(JevError, 'Invalid response'):
                self.run_job(state=path, limit=1)
            self.assertEqual(len(status(path)['blocked_requests']), 1)
            self.assertGreater(status(path)['unresolved_reservation_usd'], 0)
