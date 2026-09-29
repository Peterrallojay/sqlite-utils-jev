"""Offline crash/resume demonstration. Scripted answers; no network or model benchmark."""
from contextlib import closing
import io
import json
from pathlib import Path
import select
import sqlite3
import subprocess
import sys
import tempfile
import time
from unittest.mock import patch

from sqlite_utils_jev import classify_table, status

QUESTION = {'type': 'choice', 'instructions': 'Which team?',
            'criteria': {'billing': 'Payments', 'technical': 'Bugs'}}


def scripted_response(request, **kwargs):
    payload = json.loads(request.data)
    answers = {}
    for key in payload['questions']:
        state = payload['state'] if key == 'classification' else payload['state']['rows'][key]
        label = 'technical' if 'error' in state['body'] else 'billing'
        answers[key] = {'type': 'choice', 'choice': label, 'confidence': .9,
                        'probabilities': {name: .95 if name == label else .05 for name in QUESTION['criteria']}}
    response = io.BytesIO(json.dumps({'model': payload['model'], 'answers': answers,
                                    'usage': {'input_tokens': 100}}).encode())
    response.code, response.headers = 200, {'x-request-id': 'synthetic-demo'}
    return response


def run(source, journal, mode='packed', progress=None):
    with patch('sqlite_utils_jev.client.urllib.request.build_opener') as http, \
         patch('sqlite_utils_jev.client.REQUEST_INTERVAL', 0):
        http.return_value.open.side_effect = scripted_response
        return classify_table(source, 'tickets', key='id', text_columns=['body'], question=QUESTION,
                              state=journal, budget_usd=1, api_key='synthetic', mode=mode, progress=progress)


def main():
    with tempfile.TemporaryDirectory(prefix='jev-resume-demo-') as temp:
        root = Path(temp)
        source, journal = root/'tickets.sqlite', root/'packed.sqlite'
        with closing(sqlite3.connect(source)) as db, db:
            db.execute('CREATE TABLE tickets(id INTEGER PRIMARY KEY, body TEXT)')
            db.executemany('INSERT INTO tickets VALUES(?,?)',
                           [(i, f"{'error' if i%2 else 'refund'} ticket {i}") for i in range(400)])
        child = subprocess.Popen([sys.executable, __file__, '--worker', str(source), str(journal)],
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            if not select.select([child.stdout], [], [], 30)[0]:
                raise RuntimeError('Worker did not reach its checkpoint')
            message = child.stdout.readline().strip()
            if message != 'checkpoint:200':
                raise RuntimeError(f'Unexpected worker checkpoint: {message}')
        finally:
            child.kill()
            child.communicate(timeout=5)
        resumed = run(source, journal)
        isolated = run(source, root/'isolated.sqlite', mode='isolated')
        assert resumed['rows'] == isolated['rows'] == 400
        assert resumed['cached_rows'] == 200
        assert resumed['requests'] == 5
        assert status(journal)['requests'] == 10
        print(json.dumps({'synthetic_only': True, 'rows': 400, 'killed_after_rows': 200,
                          'rows_recovered_from_cache': resumed['cached_rows'],
                          'new_requests_after_restart': resumed['requests'],
                          'packed_total_requests': status(journal)['requests'],
                          'isolated_total_requests': isolated['requests'],
                          'note': 'Scripted transport, zero network calls. Not a speed, cost or accuracy benchmark.'}, indent=2))


if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] == '--worker':
        def checkpoint(update):
            if update['rows'] == 200:
                print('checkpoint:200', flush=True)
                time.sleep(60)
        run(sys.argv[2], sys.argv[3], progress=checkpoint)
    else:
        main()
