"""Real login isolation and append-only shared memory, against disposable PostgreSQL."""
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
import uuid

import psycopg

from package_context_sql_blue.local_db import LocalDB
from package_context_sql_blue.remote_admin import initialize, add_account, add_tenant, read_private, revoke_account


@unittest.skipUnless(shutil.which('initdb') and os.getuid() != 0, 'needs PostgreSQL and a non-root user')
class RemoteDatabaseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix='ctx-remote-')
        root = Path(cls.temp.name)
        cls.db = LocalDB(root / 'state', root / 'config/local.json')
        cls.db.init()
        cls.config = root / 'service/server.json'
        initialize(cls.db.state, cls.db.config, cls.config)
        cls.tenant = add_tenant(cls.db.state, cls.db.config, 'team')
        for label, tenant in [('alice', cls.tenant), ('bob', cls.tenant), ('eve', None)]:
            add_account(cls.db.state, cls.db.config, cls.config, label, root / (label + '.json'), tenant)
        cls.accounts = read_private(cls.config)['accounts']
        cls.trace = read_private(cls.config)['trace']

    @classmethod
    def tearDownClass(cls):
        cls.db.stop()
        cls.temp.cleanup()

    def connect(self, account=0, role='writer'):
        return psycopg.connect(**self.accounts[account][role], autocommit=True)

    def test_visibility_and_revision_authorship(self):
        with self.connect() as alice, self.connect(1) as bob, self.connect(2) as eve:
            ids = {}
            for visibility in ('private', 'tenant', 'public'):
                entry = alice.execute('INSERT INTO memory.entry(visibility,tenant_id) VALUES(%s,%s) RETURNING entry_id',
                    (visibility, self.tenant if visibility == 'tenant' else None)).fetchone()[0]
                ids[visibility] = entry
                alice.execute("INSERT INTO memory.revision(entry_id,body) VALUES(%s,'{\"note\":\"original\"}')", (entry,))
            self.assertEqual(bob.execute('SELECT count(*) FROM memory.entry WHERE entry_id=ANY(%s)', (list(ids.values()),)).fetchone()[0], 2)
            self.assertEqual(eve.execute('SELECT count(*) FROM memory.entry WHERE entry_id=ANY(%s)', (list(ids.values()),)).fetchone()[0], 1)
            self.assertEqual(eve.execute('SELECT count(*) FROM memory.revision WHERE entry_id=ANY(%s)', (list(ids.values()),)).fetchone()[0], 1)
            with self.assertRaises(psycopg.Error):
                bob.execute("INSERT INTO memory.revision(entry_id,body) VALUES(%s,'{}')", (ids['public'],))
            for statement in ['UPDATE memory.entry SET visibility=\'public\'', 'DELETE FROM memory.revision', 'TRUNCATE memory.entry CASCADE']:
                with self.assertRaises(psycopg.Error):
                    alice.execute(statement)
            foreign_revision = alice.execute("INSERT INTO memory.revision(entry_id,body) VALUES(%s,'{}') RETURNING revision_id", (ids['private'],)).fetchone()[0]
            with self.assertRaises(psycopg.Error):
                alice.execute("INSERT INTO memory.revision(entry_id,parent_revision_id,body) VALUES(%s,%s,'{}')", (ids['public'], foreign_revision))

    def test_identity_cannot_be_spoofed_or_privileges_escalated(self):
        with self.connect() as conn:
            original = str(conn.execute('SELECT remote.current_account()').fetchone()[0])
            conn.execute("SELECT set_config('context.account_id',%s,false)", (self.accounts[1]['account_id'],))
            conn.execute('SET ROLE context_remote_writer')
            self.assertEqual(str(conn.execute('SELECT remote.current_account()').fetchone()[0]), original)
            for query in ["INSERT INTO remote.membership VALUES(gen_random_uuid(),gen_random_uuid())", 'SET ROLE context_admin',
                          'SET SESSION AUTHORIZATION context_admin', 'CREATE TEMP TABLE stolen(a int)',
                          'CREATE SCHEMA stolen', 'SELECT * FROM working.item', 'DELETE FROM catalog.skill',
                          'SELECT * FROM remote.login_identity', 'UPDATE trace.query SET outcome=\'succeeded\'']:
                with self.assertRaises(psycopg.Error, msg=query):
                    conn.execute(query)
            with self.assertRaises(psycopg.Error):
                conn.execute('INSERT INTO memory.entry(author_id) VALUES(%s)', (self.accounts[1]['account_id'],))
        with self.connect(role='reader') as conn:
            with self.assertRaises(psycopg.Error):
                conn.execute('INSERT INTO memory.entry DEFAULT VALUES')

    def test_trace_is_private_and_service_cannot_modify_memory(self):
        aid = self.accounts[0]['account_id']
        with psycopg.connect(**self.trace, autocommit=True) as service:
            prompt = service.execute("INSERT INTO trace.prompt(account_id,project,task,text) VALUES(%s,'p','t','exact prompt') RETURNING prompt_id", (aid,)).fetchone()[0]
            query = service.execute("INSERT INTO trace.query(account_id,prompt_id,sql,role) VALUES(%s,%s,'SELECT 1/0','reader') RETURNING query_id", (aid,prompt)).fetchone()[0]
            with self.connect(role='reader') as reader:
                with self.assertRaises(psycopg.Error):
                    reader.execute('SELECT 1/0')
            service.execute("UPDATE trace.query SET outcome='failed',finished_at=clock_timestamp(),response='{\"error\":\"division by zero\"}' WHERE query_id=%s", (query,))
            with self.assertRaises(psycopg.Error):
                service.execute('INSERT INTO memory.entry DEFAULT VALUES')
            with self.assertRaises(psycopg.Error):
                service.execute("INSERT INTO trace.query(account_id,prompt_id,sql,role) VALUES(%s,%s,'SELECT 1','reader')", (self.accounts[1]['account_id'],prompt))
        with self.connect(role='reader') as alice, self.connect(1,'reader') as bob:
            self.assertEqual(alice.execute('SELECT outcome FROM trace.query WHERE query_id=%s',(query,)).fetchone()[0], 'failed')
            self.assertIsNone(bob.execute('SELECT outcome FROM trace.query WHERE query_id=%s',(query,)).fetchone())
            with self.assertRaises(psycopg.Error):
                alice.execute("INSERT INTO trace.prompt(account_id,project,task,text) VALUES(%s,'p','t','forged')", (aid,))

    def test_revocation_disables_login_and_preserves_existing_memory(self):
        token_file = Path(self.temp.name) / 'revoked.json'
        account_id = add_account(self.db.state,self.db.config,self.config,'revoke',token_file)
        account = next(a for a in read_private(self.config)['accounts'] if a['account_id'] == account_id)
        with psycopg.connect(**account['writer']) as conn:
            entry = conn.execute('INSERT INTO memory.entry DEFAULT VALUES RETURNING entry_id').fetchone()[0]
        revoke_account(self.db.state,self.db.config,self.config,account_id)
        self.assertNotIn(account_id, [a['account_id'] for a in read_private(self.config)['accounts']])
        with self.assertRaises(psycopg.OperationalError):
            psycopg.connect(**account['reader'])
        self.assertEqual(self.db.query(f"SELECT count(*) FROM memory.entry WHERE entry_id='{entry}'"), '1')

    def test_credentials_are_private_and_only_hash_is_in_service_config(self):
        self.assertEqual(self.config.stat().st_mode & 0o777, 0o600)
        alice_token = read_private(Path(self.temp.name) / 'alice.json')['token']
        self.assertNotIn(alice_token, self.config.read_text())
        self.assertNotIn('context_admin', self.config.read_text())
        with self.assertRaises(ValueError):
            add_account(self.db.state,self.db.config,self.config,'collision',self.config)


if __name__ == '__main__':
    unittest.main()
