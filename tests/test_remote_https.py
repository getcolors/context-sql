"""Forward test a copied skill against real HTTPS and disposable PostgreSQL."""
import http.server
import json
import os
from pathlib import Path
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import unittest

from package_context_sql_blue.local_db import LocalDB
from package_context_sql_blue.remote_admin import initialize, add_account

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(shutil.which('initdb') and shutil.which('openssl') and os.getuid() != 0,
                     'needs PostgreSQL, OpenSSL, and a non-root user')
class RemoteHTTPSTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix='ctx-https-')
        cls.addClassCleanup(cls.temp.cleanup)
        cls.root = Path(cls.temp.name)
        cls.db = LocalDB(cls.root / 'state', cls.root / 'config/local.json')
        cls.addClassCleanup(cls.db.stop)
        cls.db.init()
        cls.config = cls.root / 'service/config.json'
        initialize(cls.db.state, cls.db.config, cls.config)
        cls.tokens = []
        for label in ('alice', 'bob'):
            token = cls.root / (label + '.json')
            add_account(cls.db.state, cls.db.config, cls.config, label, token)
            cls.tokens.append(token)
        cls.cert, cls.key = cls.root / 'cert.pem', cls.root / 'key.pem'
        subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '1',
                        '-keyout', str(cls.key), '-out', str(cls.cert), '-subj', '/CN=localhost',
                        '-addext', 'subjectAltName=DNS:localhost,IP:127.0.0.1'], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=20)
        cls.skill = cls.root / 'installed/sql-context'
        shutil.copytree(ROOT / 'skills/sql-context', cls.skill)
        cls.runner = cls.skill / 'scripts/remote.py'
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            cls.port = sock.getsockname()[1]
        cls.url = f'https://localhost:{cls.port}'
        cls.env = {k: v for k, v in os.environ.items() if not k.startswith(('CONTEXT_SQL_', 'PG'))}
        cls.env.update(CONTEXT_SQL_URL=cls.url, SSL_CERT_FILE=str(cls.cert),
                       PYTHONPATH=str(ROOT / 'src'))
        cls.log = open(cls.root / 'server.log', 'w+')
        cls.addClassCleanup(cls.log.close)
        cls.server = subprocess.Popen([sys.executable, '-m', 'package_context_sql_blue.remote_service',
            '--config', str(cls.config), '--port', str(cls.port), '--certfile', str(cls.cert), '--keyfile', str(cls.key)],
            env=cls.env, stdout=cls.log, stderr=cls.log)
        cls.addClassCleanup(cls.stop_server)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if cls.server.poll() is not None:
                cls.log.seek(0)
                raise RuntimeError('HTTPS service failed to start: ' + cls.log.read())
            probe = cls.call('schema')
            if probe.returncode == 0:
                break
            time.sleep(0.1)
        else:
            raise RuntimeError('HTTPS service did not become ready')

    @classmethod
    def stop_server(cls):
        cls.server.terminate()
        try:
            cls.server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            cls.server.kill()
            cls.server.wait(timeout=5)

    @classmethod
    def call(cls, *args, account=0, env=None, auth=True):
        return subprocess.run([sys.executable, str(cls.runner),
                *(['--token-file', str(cls.tokens[account])] if auth else []), *map(str, args)],
                cwd=cls.root, env=env or cls.env, capture_output=True, text=True, timeout=25)

    def invoke(self, *args, account=0):
        result = self.call(*args, account=account)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        return json.loads(result.stdout)

    def prompt(self, account=0):
        return self.invoke('prompt', '--text', 'Find PostHog evidence and preserve its source revision.',
                           '--project', 'amiorin/posthog', '--task', 'https-test', account=account)['prompt_id']

    def query(self, prompt, sql, params=None, writer=False, capture=False, account=0):
        args = ['sql', '--prompt-id', prompt, '--query', sql]
        if params is not None:
            args += ['--params', json.dumps(params)]
        if writer:
            args += ['--role', 'writer']
        if capture:
            args += ['--capture-result']
        return self.invoke(*args, account=account)

    def test_copied_skill_memory_evidence_and_failure_trace(self):
        self.assertTrue((self.skill / 'references/queries.sql').is_file())
        prompt = self.prompt()
        evidence = self.query(prompt, 'SELECT slug, kind FROM catalog.skill WHERE kind=%s ORDER BY slug LIMIT 2',
                              ['context'], capture=True)
        self.assertEqual(len(evidence['rows']), 2)
        self.assertEqual(evidence['outcome'], 'succeeded')
        entry = self.query(prompt, 'INSERT INTO memory.entry DEFAULT VALUES RETURNING entry_id', writer=True)['rows'][0][0]
        note = {'project': 'amiorin/posthog', 'task': 'https-test', 'text': 'Retrieved immutable source evidence'}
        self.query(prompt, 'INSERT INTO memory.revision(entry_id,body) VALUES(%s::uuid,%s::jsonb) RETURNING revision_id',
                   [entry, json.dumps(note)], writer=True)
        saved = self.query(prompt, 'SELECT body FROM memory.revision WHERE entry_id=%s::uuid', [entry])
        self.assertEqual(saved['rows'][0][0], note)
        bob_prompt = self.prompt(account=1)
        hidden = self.query(bob_prompt, 'SELECT body FROM memory.revision WHERE entry_id=%(entry)s::uuid',
                            {'entry': entry}, account=1)
        self.assertEqual(hidden['rows'], [])
        trace = self.query(prompt, 'SELECT p.text,q.sql,q.parameters,q.response FROM trace.prompt p JOIN trace.query q USING(prompt_id) WHERE q.query_id=%s::uuid', [evidence['query_id']])
        self.assertEqual(trace['rows'][0][0], 'Find PostHog evidence and preserve its source revision.')
        self.assertEqual(trace['rows'][0][2], ['context'])
        self.assertEqual(trace['rows'][0][3]['rows'], evidence['rows'])
        failed = self.call('sql', '--prompt-id', prompt, '--query', 'INSERT INTO memory.entry DEFAULT VALUES RETURNING entry_id')
        self.assertNotEqual(failed.returncode, 0)
        failure = json.loads(failed.stdout)
        self.assertEqual(failure['outcome'], 'failed')
        persisted = self.query(prompt, 'SELECT outcome FROM trace.query WHERE query_id=%s::uuid', [failure['query_id']])
        self.assertEqual(persisted['rows'], [['failed']])

    def test_authentication_prompt_ownership_and_tls(self):
        missing = self.call('schema', auth=False)
        self.assertNotEqual(missing.returncode, 0)
        wrong = self.call('schema', auth=False, env={**self.env, 'CONTEXT_SQL_TOKEN': 'wrong-token'})
        self.assertNotEqual(wrong.returncode, 0)
        self.assertIn('401', wrong.stderr)
        prompt = self.prompt()
        forged = self.call('sql', '--prompt-id', prompt, '--query', 'SELECT 1', account=1)
        self.assertNotEqual(forged.returncode, 0)
        self.assertIn('404', forged.stderr)
        own = self.prompt(account=1)
        invisible = self.query(own, 'SELECT prompt_id FROM trace.prompt WHERE prompt_id=%s::uuid', [prompt], account=1)
        self.assertEqual(invisible['rows'], [])
        no_trust = {k: v for k, v in self.env.items() if k != 'SSL_CERT_FILE'}
        untrusted = self.call('schema', env=no_trust)
        self.assertNotEqual(untrusted.returncode, 0)
        self.assertIn('certificate trust', untrusted.stderr)
        insecure = self.call('--url', self.url.replace('https:', 'http:'), 'schema')
        self.assertNotEqual(insecure.returncode, 0)
        self.assertIn('HTTPS', insecure.stderr)

    def test_https_redirect_is_not_followed(self):
        paths = []
        class Redirect(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                paths.append(self.path)
                self.send_response(302)
                self.send_header('Location', '/credential-trap')
                self.end_headers()
            def log_message(self, *args):
                pass
        server = http.server.HTTPServer(('127.0.0.1', 0), Redirect)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(self.cert, self.key)
        server.socket = context.wrap_socket(server.socket, server_side=True)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            result = self.call('--url', f'https://localhost:{server.server_port}', 'schema')
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('redirect refused', result.stderr)
            self.assertEqual(paths, ['/v1/schema'])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)


if __name__ == '__main__':
    unittest.main()
