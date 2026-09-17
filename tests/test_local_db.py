"""Exercise the managed service in a disposable SCRAM-authenticated cluster."""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('local_db', ROOT / 'scripts/local_db.py')
local_db = importlib.util.module_from_spec(spec)
spec.loader.exec_module(local_db)


@unittest.skipUnless(shutil.which('initdb') and os.getuid() != 0, 'needs PostgreSQL and a non-root user')
class LocalDatabaseTests(unittest.TestCase):
    def test_lifecycle_authentication_migrations_and_backup(self):
        with tempfile.TemporaryDirectory(prefix='context-local-') as directory:
            root = Path(directory)
            db = local_db.LocalDB(root / 'state', root / 'config/connections.json')
            try:
                db.init()
                db.wait_ready()
                self.assertTrue(db.running())
                original = db.config.read_bytes()
                db.init()
                self.assertEqual(original, db.config.read_bytes())
                self.assertEqual(db.query('SELECT count(*) FROM catalog.skill'), '14')
                self.assertEqual(db.query('SHOW listen_addresses'), '')
                self.assertEqual(db.config.stat().st_mode & 0o777, 0o600)
                self.assertEqual(db.owner.stat().st_mode & 0o777, 0o600)
                config = json.loads(original)
                env = {key: value for key, value in os.environ.items() if not key.startswith('PG')}
                connection = config['reader']
                env.update(PGHOST=connection['host'], PGPORT=str(connection['port']),
                           PGUSER=connection['user'], PGDATABASE=connection['dbname'],
                           PGPASSWORD='incorrect-password')
                denied = subprocess.run(['psql', '-X', '-w', '-Atc', 'SELECT 1'],
                                        env=env, text=True, capture_output=True)
                self.assertNotEqual(denied.returncode, 0)
                self.assertIn('password authentication failed', denied.stderr)
                env['PGPASSWORD'] = connection['password']
                allowed = subprocess.run(['psql', '-X', '-w', '-Atc', 'SELECT count(*) FROM catalog.skill'],
                                         env=env, text=True, capture_output=True)
                self.assertEqual(allowed.returncode, 0, allowed.stderr)
                self.assertEqual(allowed.stdout.strip(), '14')
                denied = subprocess.run(['psql', '-X', '-w', '-v', 'ON_ERROR_STOP=1', '-c',
                                         'DELETE FROM catalog.skill'], env=env, text=True, capture_output=True)
                self.assertNotEqual(denied.returncode, 0)
                backup = db.backup(None)
                self.assertEqual(backup.stat().st_mode & 0o777, 0o600)
                restored = db.command('pg_restore', '--list', backup).stdout
                self.assertIn('catalog source_file', restored)
                db.query('CREATE DATABASE context_restore', 'postgres')
                db.command('pg_restore', '--exit-on-error', '--dbname=context_restore',
                           backup, database='context_restore')
                self.assertEqual(db.query('SELECT count(*) FROM catalog.source_file', 'context_restore'), '80')
                with self.assertRaises(FileExistsError):
                    db.backup(backup)
                name, digest = db.query('SELECT name,sha256 FROM public.context_migration ORDER BY name LIMIT 1').split('|')
                db.query(f"UPDATE public.context_migration SET sha256='changed' WHERE name={local_db.literal(name)}")
                with self.assertRaisesRegex(ValueError, 'Applied migration changed'):
                    db.init()
                db.query(f'UPDATE public.context_migration SET sha256={local_db.literal(digest)} WHERE name={local_db.literal(name)}')
                db.stop()
                self.assertFalse(db.running())
                db.start()
                self.assertEqual(db.query('SELECT count(*) FROM catalog.source_file'), '80')
            finally:
                if db.owner.exists() and (db.pg / 'PG_VERSION').exists():
                    db.stop()

    def test_preserves_transaction_words_in_payload(self):
        script = "-- seed\nBEGIN;\nSELECT 'data\nBEGIN;\nCOMMIT;\n';\nCOMMIT;\n"
        self.assertEqual(local_db.atomic_script(script), "-- seed\nSELECT 'data\nBEGIN;\nCOMMIT;\n';\n")

    def test_preserves_literal_line_separators(self):
        payload = "SELECT 'one\r\ntwo\u2028three\u2029four';\r\n"
        script = '-- seed\nBEGIN;\n' + payload + 'COMMIT;\n'
        self.assertEqual(local_db.atomic_script(script), '-- seed\n' + payload)

    def test_refuses_unmanaged_nonempty_directory(self):
        with tempfile.TemporaryDirectory(prefix='context-local-') as directory:
            root = Path(directory)
            sentinel = root / 'important'
            sentinel.write_text('keep')
            db = local_db.LocalDB(root, root / 'connections.json')
            with self.assertRaisesRegex(ValueError, 'empty state directory'):
                db.init()
            self.assertEqual(sentinel.read_text(), 'keep')


if __name__ == '__main__':
    unittest.main()
