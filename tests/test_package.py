"""Exercise the local Package Skill without touching the operator's database."""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))

from package_context_sql_blue.cli import run
from package_context_sql_blue.local_db import LocalDB


class PackageTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix='ctx-package-')
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.state = self.root / 'state'
        self.connections = self.root / 'config/connections.json'
        self.skill = self.root / 'skills/sql-context'
        self.config = self.root / 'colors.yml'
        self.write_config()
        clean_env = {key: value for key, value in os.environ.items()
                     if not key.startswith('COLORS_PAR_')}
        self.environ = patch.dict(os.environ, clean_env, clear=True)
        self.environ.start()
        self.addCleanup(self.environ.stop)

    def write_config(self, extra=''):
        self.config.write_text(
            'profile: context-test\n'
            f'context-sql-state-dir: {self.state}\n'
            f'context-sql-config: {self.connections}\n'
            f'context-sql-skill-dir: {self.skill}\n'
            'context-sql-install-skill: true\n'
            'context-sql-service: false\n' + extra)

    async def invoke(self, *args):
        return await run(*args, '--file', str(self.config))

    async def test_build_offline_is_deterministic_and_contains_no_credentials(self):
        with patch.dict(os.environ, {'PATH': ''}):
            first = await self.invoke('build')
            self.assertEqual(first['blue/exit'], 0, first)
            plan = self.root / '.colors/context-test/context-sql/plan.json'
            original = plan.read_bytes()
            self.assertIsInstance(json.loads(original), dict)
            second = await self.invoke('build')
            self.assertEqual(second['blue/exit'], 0, second)
            self.assertEqual(original, plan.read_bytes())
        self.assertNotIn(b'password', original.lower())
        self.assertFalse(self.state.exists())
        self.assertFalse(self.connections.exists())
        self.assertFalse(self.skill.exists())

    async def test_create_dry_run_needs_no_tools_and_writes_nothing(self):
        before = sorted(str(path.relative_to(self.root)) for path in self.root.rglob('*'))
        with patch.dict(os.environ, {'PATH': ''}):
            result = await self.invoke('create', '--dry-run')
        self.assertEqual(result['blue/exit'], 0, result)
        self.assertEqual(before, sorted(str(path.relative_to(self.root))
                                        for path in self.root.rglob('*')))

    async def test_configuration_discovery_walks_up(self):
        child = self.root / 'nested/directory'
        child.mkdir(parents=True)
        previous = Path.cwd()
        try:
            os.chdir(child)
            result = await run('build')
        finally:
            os.chdir(previous)
        self.assertEqual(result['blue/exit'], 0, result)
        self.assertTrue((self.root / '.colors/context-test/context-sql/plan.json').exists())

    async def test_rejects_invalid_configuration_together(self):
        self.config.write_text('profile: ../escape\nunknown-option: true\n'
                               'context-sql-install-skill: maybe\n')
        result = await self.invoke('create', '--dry-run')
        self.assertEqual(result['blue/exit'], 2, result)
        error = str(result.get('blue/err', result))
        for field in ('profile', 'unknown-option', 'context-sql-install-skill'):
            self.assertIn(field, error)
        self.assertFalse(self.state.exists())

    async def test_profile_environment_override_is_rejected(self):
        with patch.dict(os.environ, {'COLORS_PAR_PROFILE': 'different'}):
            result = await self.invoke('build')
        self.assertEqual(result['blue/exit'], 2, result)
        self.assertIn('COLORS_PAR_PROFILE', str(result.get('blue/err', result)))
        self.assertFalse((self.root / '.colors').exists())

    async def test_missing_profile_and_delete_are_usage_errors(self):
        self.config.write_text('context-sql-service: false\n')
        result = await self.invoke('build')
        self.assertEqual(result['blue/exit'], 2, result)
        result = await self.invoke('delete')
        self.assertEqual(result['blue/exit'], 2, result)

    async def test_environment_path_override_is_reflected_in_plan(self):
        alternate = self.root / 'alternate-state'
        with patch.dict(os.environ, {'COLORS_PAR_CONTEXT_SQL_STATE_DIR': str(alternate)}):
            result = await self.invoke('build')
        self.assertEqual(result['blue/exit'], 0, result)
        plan = (self.root / '.colors/context-test/context-sql/plan.json').read_text()
        self.assertIn(str(alternate), plan)
        self.assertFalse(alternate.exists())

    async def test_unrelated_package_environment_is_ignored_without_returning_values(self):
        unrelated = {'COLORS_PAR_AWS_ACCESS_KEY_ID': 'test-only-unrelated-access-key',
                     'COLORS_PAR_CLOUDFLARE_API_TOKEN': 'test-only-unrelated-token'}
        with patch.dict(os.environ, unrelated):
            result = await self.invoke('build')
        self.assertEqual(result['blue/exit'], 0, result)
        returned = repr(result)
        plan = (self.root / '.colors/context-test/context-sql/plan.json').read_text()
        for name, value in unrelated.items():
            self.assertNotIn(name, returned)
            self.assertNotIn(value, returned)
            self.assertNotIn(value, plan)

    async def test_rejects_overlapping_targets_hidden_by_path_aliases(self):
        alias = self.root / 'alias'
        alias.symlink_to(self.root, target_is_directory=True)
        for skill_path in (self.root / 'unused/../state', alias / 'state'):
            with self.subTest(skill_path=str(skill_path)):
                self.write_config()
                self.config.write_text(self.config.read_text().replace(
                    f'context-sql-skill-dir: {self.skill}',
                    f'context-sql-skill-dir: {skill_path}'))
                result = await self.invoke('create', '--dry-run')
                self.assertEqual(result['blue/exit'], 2, result)
                self.assertIn('overlap', str(result.get('blue/err', result)))
                self.assertFalse(self.state.exists())

        self.write_config()
        self.config.write_text(self.config.read_text().replace(
            f'context-sql-state-dir: {self.state}',
            'context-sql-state-dir: /tmp/..'))
        result = await self.invoke('build')
        self.assertEqual(result['blue/exit'], 2, result)
        self.assertIn('dedicated directories', str(result.get('blue/err', result)))

    @unittest.skipUnless(shutil.which('initdb') and os.getuid() != 0,
                         'needs PostgreSQL and a non-root user')
    async def test_existing_config_blocks_fresh_initialization(self):
        self.connections.parent.mkdir()
        original = b'{"unrelated": "existing configuration"}\n'
        self.connections.write_bytes(original)
        result = await self.invoke('create')
        self.assertNotEqual(result['blue/exit'], 0, result)
        self.assertIn('config', str(result.get('blue/err', result)).lower())
        self.assertEqual(self.connections.read_bytes(), original)
        self.assertFalse(self.state.exists())
        self.assertFalse(self.skill.exists())

    @unittest.skipUnless(shutil.which('initdb') and os.getuid() != 0,
                         'needs PostgreSQL and a non-root user')
    async def test_create_refuses_unmanaged_skill_before_initializing(self):
        self.skill.mkdir(parents=True)
        sentinel = self.skill / 'SKILL.md'
        sentinel.write_text('An independently maintained skill')
        result = await self.invoke('create')
        self.assertNotEqual(result['blue/exit'], 0, result)
        self.assertEqual(sentinel.read_text(), 'An independently maintained skill')
        self.assertFalse(self.state.exists())

    @unittest.skipUnless(shutil.which('initdb') and os.getuid() != 0,
                         'needs PostgreSQL and a non-root user')
    async def test_create_preserves_credentials_notes_and_installs_independent_skill(self):
        db = LocalDB(self.state, self.connections)
        try:
            result = await self.invoke('create')
            self.assertEqual(result['blue/exit'], 0, result)
            original = self.connections.read_bytes()
            db.load()
            run_id = 'ce19944c-dca2-400d-a608-0ac9fb1e86cf'
            db.query(f"INSERT INTO working.run(run_id,task,project_id,task_key) "
                     f"VALUES ('{run_id}','Package test','tests/package','preserve')")
            db.query(f"INSERT INTO working.item(run_id,kind,body) "
                     f"VALUES ('{run_id}','decision','Keep this task across initialization')")
            result = await self.invoke('create')
            self.assertEqual(result['blue/exit'], 0, result)
            self.assertEqual(original, self.connections.read_bytes())
            self.assertEqual(db.query(f"SELECT body FROM working.item WHERE run_id='{run_id}'"),
                             'Keep this task across initialization')
            self.assertEqual(db.query('SELECT count(*) FROM catalog.skill'), '14')
            self.assertEqual(db.query('SELECT count(*) FROM catalog.source_file'), '80')
            self.assertEqual(db.query('SHOW listen_addresses'), '')
            self.assertEqual(self.connections.stat().st_mode & 0o777, 0o600)
            self.assertTrue((self.skill / 'SKILL.md').is_file())
            runner = self.skill / 'scripts/context.py'
            self.assertTrue(runner.is_file())
            self.assertFalse(runner.is_symlink())
            self.assertEqual(runner.read_bytes(),
                             (ROOT / 'skills/sql-context/scripts/context.py').read_bytes())
            for relative in ('scripts/remote.py', 'references/queries.sql',
                             'references/schema.md', 'references/improvement.md'):
                self.assertEqual((self.skill / relative).read_bytes(),
                                 (ROOT / 'skills/sql-context' / relative).read_bytes())
            self.assertEqual((self.skill / 'connection-path').read_text().strip(),
                             str(self.connections))
            spec = importlib.util.spec_from_file_location('installed_package_context', runner)
            installed = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(installed)
            environment = {key: value for key, value in os.environ.items()
                           if key != 'CONTEXT_SQL_CONFIG'}
            with patch.dict(os.environ, environment, clear=True):
                settings = installed.connection_settings(True)
            self.assertEqual(settings['host'], str(self.state / 'socket'))
            self.assertEqual(settings['user'], 'context_memory')
            edited = runner.read_bytes() + b'\n# locally maintained edit\n'
            runner.write_bytes(edited)
            rejected = await self.invoke('create')
            self.assertNotEqual(rejected['blue/exit'], 0, rejected)
            self.assertEqual(runner.read_bytes(), edited)
            self.assertEqual(original, self.connections.read_bytes())
        finally:
            if db.owner.exists() and (db.pg / 'PG_VERSION').exists():
                db.stop()

    @unittest.skipUnless(shutil.which('initdb') and os.getuid() != 0,
                         'needs PostgreSQL and a non-root user')
    async def test_create_refuses_unmanaged_state(self):
        self.state.mkdir()
        sentinel = self.state / 'keep.txt'
        sentinel.write_text('existing user data')
        result = await self.invoke('create')
        self.assertNotEqual(result['blue/exit'], 0, result)
        self.assertEqual(sentinel.read_text(), 'existing user data')
        self.assertFalse((self.state / 'pg').exists())


if __name__ == '__main__':
    unittest.main()
