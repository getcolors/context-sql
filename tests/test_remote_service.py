"""Parser/auth tests; integration tests run with CONTEXT_REMOTE_TEST=1."""
import hashlib
import json
import os
import subprocess
from unittest.mock import MagicMock, patch
from pathlib import Path
import tempfile
import unittest
import uuid

import psycopg
from fastapi.testclient import TestClient
from package_context_sql_blue.remote_service import create_app, execute, load_config, validate_sql


class ParserTests(unittest.TestCase):
    def test_queries_and_data_ctes(self):
        for query in ['SELECT 1', 'SELECT %s', 'WITH x AS (DELETE FROM memory.entry RETURNING *) SELECT * FROM x', "SELECT 'SET ROLE bob; DROP TABLE t'", 'INSERT INTO memory.entry DEFAULT VALUES RETURNING *', 'UPDATE memory.entry SET visibility=\'private\'', 'DELETE FROM memory.entry']:
            if query == 'SELECT %s':
                # psycopg placeholders need normalization before parsing.
                continue
            validate_sql(query)

    def test_parameters(self):
        validate_sql('SELECT %s', [42])
        validate_sql('INSERT INTO memory.revision(entry_id,body) VALUES(%s::uuid,%s::jsonb) RETURNING revision_id', ['id', '{}'])
        validate_sql('SELECT %(name)s', {'name': 'value'})
        validate_sql("SELECT '%%', %s", [42])
        with self.assertRaises(ValueError):
            validate_sql("SELECT %s; SET ROLE bob", ['x'])

    def test_forbidden_statements_and_functions(self):
        for query in ['', 'SELECT 1; SELECT 2', 'SET ROLE bob', 'BEGIN', 'COMMIT', 'COPY (SELECT 1) TO STDOUT', 'CREATE TABLE t(x int)', 'SELECT 1 INTO t', "SELECT pg_catalog.set_config('statement_timeout','0',true)", "WITH x AS (SELECT set_config('role','bob',false)) SELECT * FROM x", 'SELECT pg_advisory_lock(1)']:
            with self.subTest(query=query), self.assertRaises(ValueError):
                validate_sql(query)

    def test_private_config(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'config.json'
            config = {'format': 1, 'trace': {'host': '/tmp'}, 'accounts': [{'account_id': str(uuid.uuid4()), 'token_sha256': 'a'*64}]}
            path.write_text(json.dumps(config))
            path.chmod(0o600)
            self.assertEqual(load_config(path), config)
            path.chmod(0o644)
            with self.assertRaises(ValueError):
                load_config(path)

    def test_hard_deadline_uses_immutable_backend_identity(self):
        process = MagicMock()
        process.communicate.side_effect = [subprocess.TimeoutExpired('worker', 8, output=b'{"pid":123,"backend_start":"2026-01-01T00:00:00Z"}\n'), (b'', b'')]
        connection = MagicMock()
        with patch('package_context_sql_blue.remote_service.subprocess.Popen', return_value=process), patch('package_context_sql_blue.remote_service.connect', return_value=connection):
            result = execute({'query_id': str(uuid.uuid4()), 'prompt_id': str(uuid.uuid4()), 'settings': {}})
        sql, params = connection.__enter__.return_value.execute.call_args.args
        self.assertIn('pg_terminate_backend', sql)
        self.assertIn('backend_start', sql)
        self.assertNotIn('application_name', sql)
        self.assertEqual(params, (123, '2026-01-01T00:00:00Z'))
        self.assertEqual(result['commit_state'], 'unknown')
        process.kill.assert_called_once()

    def test_trace_id_not_authentication(self):
        app = create_app({'trace': {}, 'accounts': [{'account_id': str(uuid.uuid4()), 'token_sha256': hashlib.sha256(b'secret').hexdigest()}]})
        with TestClient(app) as client:
            for headers in [{}, {'X-Trace-ID': str(uuid.uuid4())}, {'Authorization': 'Bearer wrong'}]:
                self.assertEqual(client.get('/v1/schema', headers=headers).status_code, 401)


@unittest.skipUnless(os.environ.get('CONTEXT_REMOTE_TEST') == '1', 'requires disposable migrated PostgreSQL')
class ServiceIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.owner = psycopg.connect('', autocommit=True)
        common = {'host': os.environ['PGHOST'], 'port': int(os.environ['PGPORT']), 'dbname': os.environ['PGDATABASE']}
        accounts = []
        cls.identities = []
        prefix = 'svc_' + uuid.uuid4().hex[:8]
        for index in range(2):
            identity = str(uuid.uuid4())
            cls.identities.append(identity)
            cls.owner.execute('INSERT INTO remote.account(account_id,label) VALUES (%s,%s)', (identity, 'test'))
            account = {'account_id': identity, 'token_sha256': hashlib.sha256(f'token{index}'.encode()).hexdigest()}
            for role in ('reader', 'writer'):
                name = f'{prefix}_{index}_{role}'
                cls.owner.execute(f'CREATE ROLE {name} LOGIN IN ROLE context_remote_{role}')
                cls.owner.execute('INSERT INTO remote.login_identity(role_name,account_id) VALUES (%s,%s)', (name, identity))
                account[role] = {**common, 'user': name}
            accounts.append(account)
        name = prefix + '_trace'
        cls.owner.execute(f'CREATE ROLE {name} LOGIN IN ROLE context_trace_service')
        cls.client = TestClient(create_app({'format': 1, 'trace': {**common, 'user': name}, 'accounts': accounts}))
        cls.headers = {'Authorization': 'Bearer token0'}

    @classmethod
    def tearDownClass(cls):
        cls.client.close()
        cls.owner.close()

    def prompt(self):
        response = self.client.post('/v1/prompts', headers=self.headers, json={'text': 'Exact original prompt\nline two', 'project': 'tests/remote', 'task': 'integration'})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()['prompt_id']

    def query(self, sql, **extra):
        return self.client.post('/v1/sql', headers=self.headers, json={'prompt_id': self.prompt(), 'sql': sql, **extra})

    def test_bound_parameters(self):
        for sql, params in [('SELECT %s::text AS value', ["quoted'; SET ROLE bob --"]), ('SELECT %(value)s::text', {'value': 'named'})]:
            response = self.query(sql, params=params)
            self.assertEqual(response.status_code, 200, response.text)
            value = params[0] if isinstance(params, list) else params['value']
            self.assertEqual(response.json()['rows'], [[value]])

    def test_result_capture_and_trace(self):
        response = self.query('SELECT 42 AS answer', capture_result=True)
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()
        self.assertEqual(result['rows'], [[42]])
        row = self.owner.execute('SELECT q.sql,q.response,p.text FROM trace.query q JOIN trace.prompt p USING(prompt_id) WHERE q.query_id=%s', (result['query_id'],)).fetchone()
        self.assertEqual(row[0], 'SELECT 42 AS answer')
        self.assertEqual(row[1]['rows'], [[42]])
        self.assertEqual(row[2], 'Exact original prompt\nline two')

    def test_rejected_attempt_persists(self):
        response = self.query('SELECT 1; DROP TABLE memory.entry')
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()
        self.assertEqual(self.owner.execute('SELECT outcome FROM trace.query WHERE query_id=%s', (result['query_id'],)).fetchone()[0], 'failed')

    def test_trace_completion_failure_preserves_committed_result(self):
        from package_context_sql_blue.remote_service import connect
        prompt_id = self.prompt()
        attempts = 0

        def fail_completion(*args, **kwargs):
            nonlocal attempts
            attempts += 1
            if attempts == 2:
                raise psycopg.OperationalError('simulated trace outage')
            return connect(*args, **kwargs)

        with patch('package_context_sql_blue.remote_service.connect', side_effect=fail_completion):
            response = self.client.post('/v1/sql', headers=self.headers, json={'prompt_id': prompt_id, 'sql': 'INSERT INTO memory.entry DEFAULT VALUES RETURNING entry_id', 'role': 'writer'})
        result = response.json()
        self.assertEqual(result['commit_state'], 'committed', response.text)
        self.assertIn('trace_completion', result)
        self.assertIsNotNone(self.owner.execute('SELECT entry_id FROM memory.entry WHERE entry_id=%s', (result['rows'][0][0],)).fetchone())
        self.assertEqual(self.owner.execute('SELECT outcome FROM trace.query WHERE query_id=%s', (result['query_id'],)).fetchone()[0], 'started')

    def test_cross_account_prompt_and_schema_auth(self):
        response = self.client.post('/v1/sql', headers={'Authorization': 'Bearer token1'}, json={'prompt_id': self.prompt(), 'sql': 'SELECT 1'})
        self.assertEqual(response.status_code, 404)
        self.assertEqual(self.client.get('/v1/schema', headers=self.headers).status_code, 200)

    def test_reader_cannot_write_and_failure_is_safe(self):
        response = self.query('INSERT INTO memory.entry DEFAULT VALUES')
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['commit_state'], 'rolled_back')
        self.assertNotIn('permission', response.text)

    def test_special_floats(self):
        response = self.query("SELECT 'NaN'::float8, 'Infinity'::float8", capture_result=True)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['rows'], [['nan', 'inf']])

    def test_indirect_settings_cannot_escape_deadline(self):
        response = self.query("SELECT query_to_xml($q$SELECT set_config('statement_timeout','0',false), set_config('application_name','escaped_service_test',false), pg_sleep(30)$q$,false,true,'')")
        self.assertIn(response.json()['commit_state'], ('unknown', 'rolled_back'), response.text)
        active = self.owner.execute("SELECT count(*) FROM pg_stat_activity WHERE application_name='escaped_service_test' AND state='active'").fetchone()[0]
        self.assertEqual(active, 0)

    def test_bounds_and_timeout(self):
        response = self.query('SELECT generate_series(1,100)')
        self.assertEqual(len(response.json()['rows']), 50)
        self.assertTrue(response.json()['truncated'])
        response = self.query("SELECT repeat('x',100000)")
        self.assertLessEqual(len(response.content), 65536)
        self.assertTrue(response.json()['truncated'])
        response = self.query('SELECT pg_sleep(20)')
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['sqlstate'], '57014')


if __name__ == '__main__':
    unittest.main()
