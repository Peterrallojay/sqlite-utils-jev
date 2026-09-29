"""Batch contracts tested with synthetic responses, never a live model."""
from contextlib import closing
import copy
import json
from pathlib import Path
import select
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from click.testing import CliRunner
from sqlite_utils.cli import cli
from sqlite_utils_jev import Client, JevError, allow_retry, classify_table, status
from sqlite_utils_jev.batching import MAX_BATCH_ROWS
from sqlite_utils_jev.client import MAX_PAYLOAD_BYTES
from test_client import MODEL, QUESTION, RESPONSE, Response


def response_for(request, **kwargs):
    payload = json.loads(request.data)
    answers = {}
    for key in reversed(list(payload['questions'])):  # Deliberately shuffled response order.
        answer = copy.deepcopy(RESPONSE['answers']['classification'])
        state = payload['state'] if key == 'classification' else payload['state']['rows'][key]
        if 'technical' in state['body']:
            answer.update(choice='technical', probabilities={'technical': .95, 'billing': .05})
        answers[key] = answer
    return Response(json.dumps({'model': MODEL, 'usage': {'input_tokens': 100}, 'answers': answers}).encode())


class BatchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.source = Path(self.temp.name)/'source.sqlite'
        self.state = Path(self.temp.name)/'journal.sqlite'
        self.question = Path(self.temp.name)/'question.json'
        self.question.write_text(json.dumps(QUESTION))
        with closing(sqlite3.connect(self.source)) as db, db:
            db.execute('CREATE TABLE tickets(id INTEGER PRIMARY KEY, body TEXT, private TEXT)')
            db.executemany('INSERT INTO tickets VALUES(?,?,?)',
                           [(i, f"{'technical' if i%2 else 'billing'} {i}", 'never-send') for i in range(1, 84)])
        self.http = patch('sqlite_utils_jev.client.urllib.request.build_opener').start()
        self.addCleanup(patch.stopall)
        self.http.return_value.open.side_effect = response_for
        patch('sqlite_utils_jev.client.REQUEST_INTERVAL', 0).start()

    def run_job(self, **kwargs):
        options = dict(key='id', text_columns=['body'], question=QUESTION,
                       state=self.state, api_key='fake', budget_usd='1')
        options.update(kwargs)
        return classify_table(self.source, 'tickets', **options)

    def rows(self, table):
        with closing(sqlite3.connect(self.state)) as db:
            db.row_factory = sqlite3.Row
            return [dict(row) for row in db.execute(f'SELECT * FROM {table}')]

    def test_default_packing_identity_progress_and_offline_resume(self):
        before = self.source.read_bytes()
        updates = []
        result = self.run_job(progress=updates.append)
        self.assertEqual((result['rows'], result['requests']), (83, 3))
        self.assertEqual([u['rows'] for u in updates], [0, 40, 80, 83])
        self.assertEqual([u['remaining'] for u in updates], [83, 43, 3, 0])
        self.assertEqual(result['accounted_usd'], status(self.state)['reported_cost_usd'])
        self.assertEqual(before, self.source.read_bytes())
        for saved in self.rows('results'):
            expected = 'technical' if int(saved['source_key'])%2 else 'billing'
            self.assertEqual(saved['label'], expected)
            raw = next(a for a in self.rows('attempts') if a['request_hash'] == saved['request_hash'])
            self.assertEqual(json.loads(raw['response_body'])['answers'][saved['answer_key']]['choice'], expected)
        for call in self.http.return_value.open.call_args_list:
            payload = json.loads(call.args[0].data)
            self.assertLessEqual(len(payload['questions']), MAX_BATCH_ROWS)
            self.assertLessEqual(len(call.args[0].data), MAX_PAYLOAD_BYTES)
            self.assertNotIn('never-send', str(payload))
            for key, question in payload['questions'].items():
                self.assertEqual(question['instructions']['record'], 'state.rows.' + key)
                self.assertEqual(set(payload['state']['rows'][key]), {'body'})
        result = self.run_job(offline=True, api_key='', min_confidence=.99)
        self.assertEqual((result['requests'], result['cache_hits'], result['cached_rows']), (0, 3, 83))
        self.assertEqual(result['abstained'], 83)
        self.assertEqual(self.http.return_value.open.call_count, 3)

    def test_batch_is_persisted_with_members_before_dispatch(self):
        def inspect(request, **kwargs):
            attempts, members = self.rows('attempts'), self.rows('batch_inputs')
            self.assertEqual(attempts[-1]['outcome'], 'pending')
            self.assertGreater(attempts[-1]['charge_nano'], 0)
            self.assertEqual(json.loads(attempts[-1]['request_json']), json.loads(request.data))
            self.assertEqual(len(members), 3)
            return response_for(request)
        self.http.return_value.open.side_effect = inspect
        self.run_job(limit=3)

    def test_reservation_failure_rolls_back_membership_and_sends_nothing(self):
        with Client(self.state, budget_usd=1):
            pass
        with closing(sqlite3.connect(self.state)) as db, db:
            db.execute("CREATE TRIGGER fail_reservation BEFORE INSERT ON attempts "
                       "BEGIN SELECT RAISE(ABORT,'disk write failed'); END")
        with self.assertRaises(sqlite3.IntegrityError): self.run_job(limit=3)
        self.assertEqual(self.rows('attempts'), [])
        self.assertEqual(self.rows('batch_inputs'), [])
        self.http.return_value.open.assert_not_called()

    def test_progress_callback_cannot_change_the_planned_question(self):
        question = copy.deepcopy(QUESTION)
        def mutate(update):
            question['instructions'] = 'A different question'
            question['criteria']['billing'] = 'A different meaning'
        self.run_job(limit=3, question=question, progress=mutate)
        payload = json.loads(self.rows('attempts')[0]['request_json'])
        self.assertEqual(payload['questions']['r0']['instructions']['task'], QUESTION['instructions'])
        self.assertEqual(payload['questions']['r0']['criteria'], QUESTION['criteria'])

    def test_size_splitting_unicode_and_oversize_preflight(self):
        with closing(sqlite3.connect(self.source)) as db, db:
            db.execute('UPDATE tickets SET body=? || id', ('é'*3500,))
        result = self.run_job(limit=5)
        self.assertGreater(result['requests'], 1)
        self.assertTrue(all(len(call.args[0].data) <= MAX_PAYLOAD_BYTES for call in self.http.return_value.open.call_args_list))
        with closing(sqlite3.connect(self.source)) as db, db:
            db.execute('UPDATE tickets SET body=? WHERE id=83', ('x'*24000,))
        calls = self.http.return_value.open.call_count
        with self.assertRaisesRegex(JevError, 'exceeds'):
            self.run_job()
        self.assertEqual(self.http.return_value.open.call_count, calls)

    def test_duplicates_and_empty_rows(self):
        with closing(sqlite3.connect(self.source)) as db, db:
            db.execute("UPDATE tickets SET body='billing same' WHERE id<=2")
            db.execute("UPDATE tickets SET body='  ' WHERE id=3")
        result = self.run_job(limit=3)
        self.assertEqual((result['rows'], result['empty'], result['requests']), (3, 1, 1))
        self.assertEqual(len(json.loads(self.http.return_value.open.call_args.args[0].data)['questions']), 1)
        rows = self.rows('results')
        nonempty = [r for r in rows if r['request_hash']]
        self.assertEqual(len({r['answer_key'] for r in nonempty}), 1)
        self.assertEqual(next(r for r in rows if r['status']=='empty')['answer_key'], None)

    def test_changed_context_recomputes_whole_batch_and_isolated_is_separate(self):
        first = self.run_job()
        with closing(sqlite3.connect(self.source)) as db, db:
            db.execute("UPDATE tickets SET body='billing changed' WHERE id=1")
        second = self.run_job()
        self.assertEqual((second['requests'], second['cached_rows']), (1, 43))
        with self.assertRaisesRegex(JevError, 'offline'):
            self.run_job(mode='isolated', limit=1, offline=True)
        isolated = self.run_job(mode='isolated', limit=1)
        self.assertNotEqual(isolated['question_hash'], first['question_hash'])
        self.assertEqual(len(self.rows('results')), 84)

    def test_partial_answers_publish_nothing_but_keep_usage(self):
        def partial(request, **kwargs):
            result = json.loads(response_for(request).getvalue())
            del result['answers']['r1']
            return Response(json.dumps(result).encode())
        self.http.return_value.open.side_effect = partial
        with self.assertRaisesRegex(JevError, 'Invalid response'):
            self.run_job(limit=3)
        self.assertEqual(self.rows('results'), [])
        self.assertEqual(status(self.state)['input_tokens'], 100)
        self.assertEqual(self.rows('attempts')[0]['outcome'], 'invalid')
        with self.assertRaisesRegex(JevError, 'Previous attempt'):
            self.run_job(limit=3)
        self.assertEqual(self.http.return_value.open.call_count, 1)

    def test_bad_answer_variants_invalidate_entire_batch(self):
        for kind in ('extra', 'bad-category', 'huge-confidence', 'wrong-model'):
            path = self.state.with_name(kind+'.sqlite')
            def invalid(request, **kwargs):
                data = json.loads(response_for(request).getvalue())
                if kind == 'extra': data['answers']['unexpected'] = data['answers']['r0']
                if kind == 'bad-category': data['answers']['r1']['choice'] = 'not-a-category'
                if kind == 'huge-confidence': data['answers']['r1']['confidence'] = 10**400
                if kind == 'wrong-model': data['model'] = 'jev-0.0.0'
                return Response(json.dumps(data).encode())
            self.http.return_value.open.side_effect = invalid
            with self.subTest(kind=kind), self.assertRaises(JevError):
                self.run_job(limit=3, state=path)
            with closing(sqlite3.connect(path)) as db:
                self.assertEqual(db.execute('SELECT count(*) FROM results').fetchone()[0], 0)

    def test_duplicate_answer_ids_are_invalid_even_when_last_answer_is_valid(self):
        first = json.dumps(RESPONSE['answers']['classification'])
        second = json.dumps({**RESPONSE['answers']['classification'], 'choice': 'technical',
                             'probabilities': {'billing': .05, 'technical': .95}})
        body = '{"model":"'+MODEL+'","usage":{"input_tokens":100},"answers":{"r0":'+first+',"r0":'+second+'}}'
        self.http.return_value.open.side_effect = lambda *a, **kw: Response(body.encode())
        with self.assertRaisesRegex(JevError, 'Invalid response'): self.run_job(limit=1)
        self.assertEqual(self.rows('results'), [])
        self.assertEqual(self.rows('attempts')[0]['outcome'], 'invalid')
        self.assertEqual(self.rows('attempts')[0]['response_body'], body)
        self.assertGreater(status(self.state)['unresolved_reservation_usd'], 0)

    def test_deep_json_is_invalid_and_retryable_instead_of_stuck_received(self):
        body = '['*10000+'0'+']'*10000
        self.http.return_value.open.side_effect = lambda *a, **kw: Response(body.encode())
        with self.assertRaisesRegex(JevError, 'Invalid response'): self.run_job(limit=1)
        self.assertEqual(self.rows('results'), [])
        attempt = self.rows('attempts')[0]
        self.assertEqual(attempt['response_body'], body)
        self.assertEqual(attempt['outcome'], 'invalid')
        self.assertEqual(len(status(self.state)['blocked_requests']), 1)
        before = status(self.state)['unresolved_reservation_usd']
        allow_retry(self.state, attempt['request_hash'])
        self.http.return_value.open.side_effect = response_for
        self.run_job(limit=1)
        self.assertEqual(status(self.state)['unresolved_reservation_usd'], before)

    def test_timeout_blocks_repacking_even_with_retry_permission(self):
        self.http.return_value.open.side_effect = TimeoutError()
        with self.assertRaises(JevError): self.run_job(limit=3)
        before = status(self.state)['unresolved_reservation_usd']
        failed = self.rows('attempts')[0]['request_hash']
        allow_retry(self.state, failed)
        for options in ({'limit': 2}, {'limit': 1, 'mode': 'isolated'}):
            with self.subTest(options=options), self.assertRaisesRegex(JevError, 'unresolved request'):
                self.run_job(**options)
        self.assertEqual(self.http.return_value.open.call_count, 1)
        self.http.return_value.open.side_effect = response_for
        self.run_job(limit=3)
        self.assertEqual(self.http.return_value.open.call_count, 2)
        self.assertEqual(status(self.state)['unresolved_reservation_usd'], before)
        self.run_job(limit=2)
        self.assertEqual(self.http.return_value.open.call_count, 3)

    def test_received_response_recovers_after_result_write_failure(self):
        def reject_writes(request, **kwargs):
            with closing(sqlite3.connect(self.state)) as db, db:
                db.execute("CREATE TRIGGER fail_second BEFORE INSERT ON results WHEN NEW.source_key='2' "
                           "BEGIN SELECT RAISE(ABORT,'write interrupted'); END")
            return response_for(request)
        self.http.return_value.open.side_effect = reject_writes
        with self.assertRaises(sqlite3.IntegrityError): self.run_job(limit=3)
        self.assertEqual(self.rows('results'), [])
        with closing(sqlite3.connect(self.state)) as db, db:
            db.execute('DROP TRIGGER fail_second')
            db.execute("UPDATE attempts SET outcome='received',input_tokens=NULL,charge_nano=reservation_nano")
        result = self.run_job(limit=3, offline=True)
        self.assertEqual((result['rows'], result['requests']), (3, 0))
        self.assertEqual(status(self.state)['input_tokens'], 100)
        self.assertEqual(self.http.return_value.open.call_count, 1)

    def test_budget_stops_before_next_batch_and_history_survives(self):
        def first(request, **kwargs):
            with closing(sqlite3.connect(self.state)) as db, db:
                db.execute('UPDATE settings SET budget_nano=4200')
            return response_for(request)
        self.http.return_value.open.side_effect = first
        with self.assertRaisesRegex(JevError, 'budget exhausted'): self.run_job()
        self.assertEqual(len(self.rows('results')), 40)
        self.assertEqual(len(self.rows('attempts')), 1)
        self.assertEqual(self.http.return_value.open.call_count, 1)

    def test_progress_failure_after_commit_does_not_resend(self):
        def interrupted(update):
            if update['rows']: raise KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt): self.run_job(limit=3, progress=interrupted)
        result = self.run_job(limit=3, offline=True)
        self.assertEqual(result['cached_rows'], 3)
        self.assertEqual(self.http.return_value.open.call_count, 1)

    def test_cli_default_progress_is_stderr_and_quiet_keeps_json(self):
        base = ['jev','classify',str(self.source),'tickets','--key','id','--text','body',
                '--question',str(self.question),'--state',str(self.state),'--budget-usd','1','--limit','3']
        result = CliRunner().invoke(cli, base, env={'TYPESAFE_API_KEY':'fake'})
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(json.loads(result.stdout)['requests'], 1)
        self.assertIn('3/3 rows', result.stderr)
        result = CliRunner().invoke(cli, base+['--quiet','--offline'], env={'TYPESAFE_API_KEY':''})
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(json.loads(result.stdout)['cached_rows'], 3)
        self.assertEqual(result.stderr, '')

    def test_killed_process_blocks_batch_and_repacking(self):
        script = '''
import sys,time
from unittest.mock import patch
from sqlite_utils_jev import classify_table
def blocked(*args,**kwargs):
    print('dispatched',flush=True)
    time.sleep(60)
with patch('sqlite_utils_jev.client.urllib.request.build_opener') as http:
    http.return_value.open.side_effect=blocked
    classify_table(sys.argv[1],'tickets',key='id',text_columns=['body'],
                   question=__import__('json').loads(sys.argv[3]),state=sys.argv[2],
                   budget_usd=1,api_key='fake',limit=3)
'''
        child = subprocess.Popen([sys.executable,'-c',script,str(self.source),str(self.state),json.dumps(QUESTION)],
                                 stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
        try:
            self.assertTrue(select.select([child.stdout],[],[],10)[0])
            self.assertEqual(child.stdout.readline().strip(),'dispatched')
        finally:
            child.kill()
            child.communicate(timeout=5)
        self.assertEqual(len(self.rows('batch_inputs')),3)
        self.assertGreater(status(self.state)['unresolved_reservation_usd'],0)
        with self.assertRaisesRegex(JevError,'Previous attempt is pending'): self.run_job(limit=3)
        with self.assertRaisesRegex(JevError,'unresolved request'): self.run_job(limit=2)
        self.http.return_value.open.assert_not_called()

    def test_direct_client_batch_bounds(self):
        with Client(self.state,api_key='fake',budget_usd=1) as client:
            for states in ([], [{}], [{'body': 4}], [{'body': 'ok'}]*41):
                with self.subTest(states=states), self.assertRaises(JevError):
                    client.evaluate_batch(states, QUESTION)
        self.http.return_value.open.assert_not_called()

    def test_v2_migration_keeps_isolated_pending_requests_blocking(self):
        self.http.return_value.open.side_effect = TimeoutError()
        with self.assertRaises(JevError): self.run_job(mode='isolated', limit=1)
        with closing(sqlite3.connect(self.state)) as db, db:
            db.executescript('''DROP TABLE batch_inputs;
                ALTER TABLE results DROP COLUMN answer_key;
                ALTER TABLE results DROP COLUMN mode;
                PRAGMA user_version=2;''')
        previous = status(self.state)
        with self.assertRaisesRegex(JevError, 'unresolved request'): self.run_job(limit=3)
        self.assertEqual(status(self.state), previous)
        self.assertEqual(self.http.return_value.open.call_count, 1)
        allow_retry(self.state, self.rows('attempts')[0]['request_hash'])
        self.http.return_value.open.side_effect = response_for
        self.run_job(mode='isolated', limit=1)
        self.assertEqual(self.http.return_value.open.call_count, 2)

    def test_all_empty_selection_and_invalid_mode(self):
        with closing(sqlite3.connect(self.source)) as db, db:
            db.execute('UPDATE tickets SET body=NULL')
        updates = []
        result = self.run_job(progress=updates.append, offline=True)
        self.assertEqual((result['rows'], result['empty'], result['requests']), (83, 83, 0))
        self.assertEqual(updates[-1]['remaining'], 0)
        with self.assertRaisesRegex(JevError, 'Mode'): self.run_job(mode='anything')
        self.http.return_value.open.assert_not_called()
