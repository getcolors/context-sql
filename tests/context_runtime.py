"""Exercise the public runner against check.sh's disposable PostgreSQL cluster."""
import base64
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

import psycopg

ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / 'skills/sql-context/scripts/context.py'


class RuntimeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.owner = psycopg.connect('', autocommit=True)
        cls.temp = tempfile.TemporaryDirectory(prefix='context-runtime-')
        cls.config = Path(cls.temp.name) / 'connections.json'
        for role, group in [('runtime_reader', 'context_reader'),
                            ('runtime_writer', 'context_writer'),
                            ('runtime_other', 'context_writer')]:
            cls.owner.execute(f'CREATE ROLE {role} LOGIN IN ROLE {group}')
        common = {'host': os.environ['PGHOST'], 'port': int(os.environ['PGPORT']),
                  'dbname': os.environ['PGDATABASE'], 'password': 'unused-trust-password'}
        cls.settings = {'reader': {**common, 'user': 'runtime_reader'},
                        'writer': {**common, 'user': 'runtime_writer'}}
        cls.config.write_text(json.dumps(cls.settings))
        cls.config.chmod(0o600)
        cls.env = {**os.environ, 'CONTEXT_SQL_CONFIG': str(cls.config)}
        cls.project = str(Path(cls.temp.name) / 'project')

    @classmethod
    def tearDownClass(cls):
        cls.owner.close()
        cls.temp.cleanup()

    def invoke(self, *args, success=True, env=None):
        result = subprocess.run([sys.executable, str(RUNNER), *map(str, args)],
                                env=env or self.env, text=True, capture_output=True,
                                timeout=20)
        if success:
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            payload = json.loads(result.stdout)
            self.assertLessEqual(len(result.stdout.encode()),
                                 int(args[args.index('--max-bytes') + 1])
                                 if '--max-bytes' in args else 65536)
            self.assertEqual(payload['output_bytes'], len(result.stdout.encode()))
            return payload
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertNotIn('Traceback', result.stderr)
        return result

    def start(self, task, project=None):
        return self.invoke('start', '--project', project or self.project,
                           '--task', task, '--description', 'runtime acceptance')['rows'][0]['run_id']

    def test_task_identity_and_sql_parameters(self):
        task = "runtime'; DELETE FROM catalog.skill; --"
        run = self.start(task)
        self.assertEqual(run, self.start(task))
        self.assertNotEqual(run, self.start(task, self.project + '-other'))
        rows = self.invoke('runs', '--project', self.project)['rows']
        self.assertIn(run, [r['run_id'] for r in rows])
        self.invoke('search', '--query', "'; SET statement_timeout=0; SELECT pg_sleep(20); --")
        self.assertGreater(len(self.invoke('catalog')['rows']), 0)

    def test_note_restore_citation_and_state(self):
        run = self.start('citations')
        source = self.owner.execute('SELECT skill_slug,version_id,path,ordinal FROM catalog.section ORDER BY skill_slug,version_id,path,ordinal LIMIT 1').fetchone()
        note = self.invoke('note', '--run', run, '--kind', 'decision', '--body',
                           'Use the documented source revision.', '--priority', 8,
                           '--source-skill', source[0], '--source-version', source[1],
                           '--source-path', source[2], '--source-section', source[3])['rows'][0]
        restored = self.invoke('restore', '--run', run)['rows']
        row = next(r for r in restored if r['item_id'] == note['item_id'])
        self.assertEqual(row['source_version'], source[1])
        self.assertEqual(row['source_skill'], source[0])
        self.assertEqual(row['body'], 'Use the documented source revision.')
        self.assertIn('/blob/', row['citation'])
        current = self.owner.execute('SELECT current_version FROM catalog.skill WHERE slug=%s', (source[0],)).fetchone()[0]
        try:
            self.owner.execute('UPDATE catalog.skill SET current_version=NULL WHERE slug=%s', (source[0],))
            historical = self.invoke('restore', '--run', run)['rows'][0]
            self.assertEqual(historical['citation'], row['citation'])
            exact = self.invoke('section', '--skill', source[0], '--version', source[1],
                                '--path', source[2], '--ordinal', source[3], '--length', 100)['rows'][0]
            self.assertEqual(exact['version_id'], source[1])
        finally:
            self.owner.execute('UPDATE catalog.skill SET current_version=%s WHERE slug=%s', (current, source[0]))
        self.invoke('state', '--run', run, '--item', note['item_id'], '--state', 'done')
        self.assertNotIn(note['item_id'], [r['item_id'] for r in self.invoke('restore', '--run', run)['rows']])
        self.invoke('note', '--run', run, '--kind', 'decision', '--body', 'Bad reference',
                    '--priority', 0, '--source-skill', source[0], success=False)

    def test_note_chunks_and_output_limits(self):
        run = self.start('large-note')
        body = '\u754c\U0001f41b\\"\n' * 1000
        note = self.invoke('note', '--run', run, '--kind', 'observation',
                           '--body', body, '--priority', 3)['rows'][0]
        limited = self.invoke('restore', '--run', run, '--max-bytes', '1024')
        self.assertTrue(limited['truncated'])
        chunks = []
        offset = 0
        while offset < len(body):
            result = self.invoke('item', '--run', run, '--item', note['item_id'],
                                 '--text-offset', offset, '--length', 100)
            chunk = result['rows'][0]['body']
            self.assertTrue(chunk)
            chunks.append(chunk)
            offset += len(chunk)
        self.assertEqual(''.join(chunks), body)

    def test_file_bytes_reconstruct(self):
        source = self.owner.execute('SELECT skill_slug,version_id,path,content FROM catalog.source_file ORDER BY octet_length(content) DESC LIMIT 1').fetchone()
        data = bytearray()
        while len(data) < len(source[3]):
            result = self.invoke('file', '--skill', source[0], '--version', source[1],
                                 '--path', source[2], '--byte-offset', len(data), '--length', 8192)
            chunk = base64.b64decode(result['rows'][0]['content_base64'])
            self.assertTrue(chunk)
            data.extend(chunk)
        self.assertEqual(bytes(data), bytes(source[3]))

    def test_pagination(self):
        expected = self.invoke('catalog', '--limit', 50)['rows']
        actual = []
        offset = 0
        while True:
            page = self.invoke('catalog', '--limit', 2, '--offset', offset)
            actual.extend(page['rows'])
            if not page['truncated']:
                break
            self.assertGreater(page['next_offset'], offset)
            offset = page['next_offset']
        self.assertEqual(actual, expected)

    def test_restricted_logins_and_expiry(self):
        run = self.start('isolation')
        self.invoke('note', '--run', run, '--kind', 'todo', '--body', 'private', '--priority', 0)
        other = Path(self.temp.name) / 'other.json'
        other.write_text(json.dumps({**self.settings, 'writer': {**self.settings['writer'], 'user': 'runtime_other'}}))
        other.chmod(0o600)
        other_env = {**self.env, 'CONTEXT_SQL_CONFIG': str(other)}
        result = subprocess.run([sys.executable, str(RUNNER), 'restore', '--run', run],
                                env=other_env, text=True, capture_output=True, timeout=20)
        self.assertNotIn('private', result.stdout)
        with psycopg.connect(**self.settings['writer'], autocommit=True) as connection:
            for sql in ["UPDATE catalog.skill SET kind='generic'", 'SET ROLE runtime_reader',
                        'CREATE ROLE runtime_escalation']:
                with self.assertRaises(psycopg.errors.InsufficientPrivilege):
                    connection.execute(sql)
        self.owner.execute("UPDATE working.run SET created_at=now()-interval '9 days', expires_at=now()-interval '1 day' WHERE run_id=%s", (run,))
        self.invoke('note', '--run', run, '--kind', 'todo', '--body', 'expired', '--priority', 0, success=False)
        self.assertEqual(self.start('isolation'), run)
        self.invoke('restore', '--run', run)
        self.owner.execute("UPDATE working.run SET created_at=now()-interval '9 days', expires_at=now()-interval '1 day' WHERE run_id=%s", (run,))
        self.invoke('expire')
        self.assertNotEqual(self.start('isolation'), run)

    def test_config_permissions_and_administrative_login(self):
        unsafe = Path(self.temp.name) / 'unsafe.json'
        unsafe.write_text(json.dumps(self.settings))
        unsafe.chmod(0o644)
        env = {**self.env, 'CONTEXT_SQL_CONFIG': str(unsafe)}
        self.invoke('catalog', success=False, env=env)
        unsafe.chmod(0o600)
        owner = self.owner.execute('SELECT session_user').fetchone()[0]
        unsafe.write_text(json.dumps({**self.settings, 'reader': {**self.settings['reader'], 'user': owner}}))
        result = self.invoke('catalog', success=False, env=env)
        self.assertIn('administrative', result.stdout)

    def test_lock_timeout_releases_transaction(self):
        with self.owner.transaction():
            self.owner.execute('LOCK TABLE catalog.section IN ACCESS EXCLUSIVE MODE')
            started = time.monotonic()
            self.invoke('search', '--query', 'NOAUTH', success=False)
            self.assertLess(time.monotonic() - started, 7)
            self.assertEqual(self.owner.execute(
                "SELECT count(*) FROM pg_stat_activity WHERE usename='runtime_reader' "
                "AND state LIKE 'idle in transaction%%'").fetchone()[0], 0)
        self.invoke('search', '--query', 'NOAUTH')

    def test_write_serialization_failure_rolls_back(self):
        task = 'oversized-acknowledgement'
        self.invoke('start', '--project', self.project, '--task', task,
                    '--description', '界' * 1000, '--max-bytes', '1024', success=False)
        count = self.owner.execute(
            'SELECT count(*) FROM working.run WHERE owner_name=%s AND project_key=%s AND task_key=%s',
            ('runtime_writer', self.project, task)).fetchone()[0]
        self.assertEqual(count, 0)
        run = self.start(task)
        self.assertTrue(run)

    def test_bad_inputs_and_sql_failure_cleanup(self):
        self.invoke('restore', '--run', 'not-a-uuid', success=False)
        self.invoke('catalog', '--limit', 51, success=False)
        self.invoke('catalog', '--max-bytes', 1, success=False)
        run = self.start('cleanup')
        self.invoke('note', '--run', run, '--kind', 'observation', '--body', 'bad reference',
                    '--priority', 0, '--source-skill', 'missing-skill', '--source-version', '0' * 64,
                    '--source-path', 'SKILL.md', '--source-section', 0, success=False)
        self.assertEqual(self.owner.execute("SELECT count(*) FROM working.item WHERE run_id=%s", (run,)).fetchone()[0], 0)
        self.assertEqual(self.owner.execute("SELECT count(*) FROM pg_stat_activity WHERE usename='runtime_writer' AND state LIKE 'idle in transaction%%'").fetchone()[0], 0)
        self.invoke('note', '--run', run, '--kind', 'observation', '--body', 'after failure', '--priority', 0)


if __name__ == '__main__':
    unittest.main()
