"""Transactional replay against an isolated, authenticated managed PostgreSQL."""
import copy
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest

from package_context_sql_blue.ingest_db import commit_import, connection, export_lock
from package_context_sql_blue.local_db import LocalDB

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(shutil.which('initdb') and os.getuid() != 0, 'needs PostgreSQL and a non-root user')
class IngestionDatabaseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix='context-ingest-')
        root = Path(cls.temp.name)
        cls.db = LocalDB(root / 'state', root / 'config/connections.json')
        cls.db.init()
        cls.bundle = json.loads((ROOT / 'data/skills.json').read_text())
        cls.bundle['skills'] = cls.bundle['skills'][:1]
        cls.resolution = dict(requested_source=cls.bundle['repository_url'], requested_ref='main',
                              resolved_at=cls.bundle['acquisition']['acquired_at'])

    @classmethod
    def tearDownClass(cls):
        cls.db.stop()
        cls.temp.cleanup()

    def import_bundle(self, bundle, lock=None):
        return commit_import([bundle], [self.resolution], lock or {'format': 1, 'imports': []},
                             self.db.state, self.db.config)

    def test_replay_exports_lock_and_preserves_notes(self):
        run = '71717171-7171-4171-8171-717171717171'
        self.db.query(f"INSERT INTO working.run(run_id,task) VALUES ('{run}','retained')")
        self.db.query(f"INSERT INTO working.item(run_id,kind,body) VALUES ('{run}','decision','keep')")
        lock = {'format': 1, 'imports': [{'commit': self.bundle['git_commit']}]}
        before = self.db.query('SELECT count(*) FROM catalog.source_file')
        os.environ['PGHOSTADDR'] = '192.0.2.1'
        os.environ['PGSERVICE'] = 'nonexistent-context-service'
        digest = self.import_bundle(self.bundle, lock)
        self.assertNotIn('PGHOSTADDR', os.environ)
        self.assertEqual(digest, self.import_bundle(self.bundle, lock))
        self.assertEqual(export_lock(self.db.state, self.db.config, digest), lock)
        self.assertEqual(self.db.query('SELECT count(*) FROM catalog.source_file'), before)
        self.assertEqual(self.db.query(f"SELECT body FROM working.item WHERE run_id='{run}'"), 'keep')
        with connection(self.db.state, self.db.config) as conn:
            with self.assertRaises(Exception):
                conn.execute('DELETE FROM catalog.ingestion_lock WHERE sha256=%s', (digest,))

    def test_reused_version_rejects_transformed_and_missing_source(self):
        for transform in ('content', 'missing', 'section'):
            bundle = copy.deepcopy(self.bundle)
            file = bundle['skills'][0]['files'][0]
            if transform == 'content':
                file['content_base64'] = 'dGFtcGVyZWQ='
                file['sha256'] = hashlib.sha256(b'tampered').hexdigest()
            elif transform == 'missing':
                bundle['skills'][0]['files'].pop()
            else:
                section_file = next(f for f in bundle['skills'][0]['files'] if f['sections'])
                section_file['sections'][0]['body'] += 'tampered'
            before = self.db.query('SELECT count(*) FROM catalog.ingestion_lock')
            with self.assertRaises(ValueError):
                self.import_bundle(bundle, {'format': 1, 'case': transform})
            self.assertEqual(self.db.query('SELECT count(*) FROM catalog.ingestion_lock'), before)

    def test_cross_repository_and_kind_collisions_rollback(self):
        for key, value in (('repository_url', 'https://github.com/example/skills'), ('kind', 'generic')):
            bundle = copy.deepcopy(self.bundle)
            if key == 'kind':
                bundle['skills'][0][key] = 'generic' if bundle['skills'][0]['kind'] == 'context' else 'context'
            else:
                bundle[key] = value
                bundle['acquisition'][key] = value
            with self.assertRaisesRegex(ValueError, 'collision'):
                self.import_bundle(bundle)

    def test_reproduces_new_repository_payload_on_another_database(self):
        bundle = copy.deepcopy(self.bundle)
        bundle['repository_url'] = 'https://github.com/example/context-fixture'
        bundle['acquisition']['repository_url'] = bundle['repository_url']
        bundle['acquisition']['acquisition_id'] = 'd' * 64
        bundle['skills'][0]['slug'] = 'distributed-fixture'
        bundle['skills'][0]['source_directory'] = 'skills/distributed-fixture'
        bundle['skills'][0]['version_id'] = 'e' * 64
        digest = self.import_bundle(bundle)
        replica = LocalDB(Path(self.temp.name) / 'replica', Path(self.temp.name) / 'replica-config/connections.json')
        try:
            replica.init()
            replay = commit_import([bundle], [self.resolution], export_lock(self.db.state, self.db.config, digest),
                                   replica.state, replica.config)
            self.assertEqual(digest, replay)
            for table in ('skill_version', 'source_file', 'section', 'pin_row', 'eval_case'):
                query = "SELECT md5(string_agg(row_to_json(t)::text, '' ORDER BY row_to_json(t)::text)) FROM catalog." + table + " t WHERE skill_slug='distributed-fixture'"
                self.assertEqual(self.db.query(query), replica.query(query))
            import psycopg
            reader = json.loads(replica.config.read_text())['reader']
            with psycopg.connect(**reader) as conn:
                self.assertEqual(conn.execute('SELECT count(*) FROM catalog.ingestion_lock').fetchone()[0], 1)
                with self.assertRaises(psycopg.errors.InsufficientPrivilege):
                    conn.execute('DELETE FROM catalog.source_resolution')
        finally:
            replica.stop()

    def test_failed_later_bundle_rolls_back_first_and_pointer(self):
        first = copy.deepcopy(self.bundle)
        first['skills'][0]['version_id'] = 'a' * 64
        first['acquisition']['acquisition_id'] = 'b' * 64
        second = copy.deepcopy(self.bundle)
        second['repository_url'] = 'https://github.com/example/other'
        second['acquisition']['repository_url'] = second['repository_url']
        second['acquisition']['acquisition_id'] = 'c' * 64
        second['skills'][0]['slug'] = self.bundle['skills'][0]['slug'] + '-new'
        second['skills'][0]['files'][0]['sha256'] = '0' * 64
        slug = first['skills'][0]['slug']
        before = self.db.query(f"SELECT current_version FROM catalog.skill WHERE slug='{slug}'")
        with self.assertRaises(Exception):
            commit_import([first, second], [self.resolution, self.resolution],
                          {'format': 1, 'case': 'rollback'}, self.db.state, self.db.config)
        self.assertEqual(self.db.query(f"SELECT current_version FROM catalog.skill WHERE slug='{slug}'"), before)
        self.assertEqual(self.db.query("SELECT count(*) FROM catalog.skill_version WHERE version_id='" + 'a' * 64 + "'"), '0')


if __name__ == '__main__':
    unittest.main()
