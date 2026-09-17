"""Exercise remote acquisition using disposable Git fixtures and an installer double."""
import base64
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from package_context_sql_blue import acquisition as a


class SourceTests(unittest.TestCase):
    def test_sources_and_refs(self):
        for source in ('team/repo', 'https://github.com/team/repo.git', 'Team/Repo'):
            self.assertEqual(('https://github.com/team/repo', 'HEAD'), a.parse_source(source))
        self.assertEqual(('https://github.com/team/repo', 'feature/nested'),
                         a.parse_source('https://github.com/team/repo/tree/feature/nested'))
        self.assertEqual(('https://github.com/team/repo', 'main'),
                         a.parse_source('https://github.com/team/repo/tree/main', 'main'))

    def test_rejected_sources_and_conflicts(self):
        for source in ('/tmp/repo', 'http://github.com/team/repo',
                       'https://token@github.com/team/repo', 'https://github.com/team/repo?q=a',
                       'https://github.com/team/repo#main', 'https://example.com/team/repo',
                       'https://github.com/team/repo/tree/../bad'):
            with self.subTest(source=source), self.assertRaises(ValueError):
                a.parse_source(source)
        with self.assertRaisesRegex(ValueError, 'Conflicting'):
            a.parse_source('https://github.com/team/repo/tree/main', 'other')
        for ref in ('--upload-pack=bad', '', 'a b', 'main^', 'main:other'):
            with self.subTest(ref=ref), self.assertRaises(ValueError):
                a.parse_source('team/repo', ref)

    def test_cli_version_is_resolved_then_exact(self):
        with patch.object(a.subprocess, 'run', return_value=subprocess.CompletedProcess(
                [], 0, '"1.8.3"', '')) as run:
            self.assertEqual('1.8.3', a.resolve_cli_version())
            run.assert_called_once()
        for version in ('latest', '^1.0.0', '1', '1.2.3;echo', '01.2.3'):
            with self.subTest(version=version), self.assertRaises(ValueError):
                a.resolve_cli_version(version)


class AcquireTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.repo = Path(self.temporary.name) / 'upstream'
        self.repo.mkdir()
        self.real_git = a.snapshot.git
        self.real_run = subprocess.run
        self.real_git(self.repo, 'init', '-q')
        self.real_git(self.repo, 'config', 'user.email', 'fixture@example.invalid')
        self.real_git(self.repo, 'config', 'user.name', 'Fixture')
        for slug in ('fixture-one', 'fixture-two'):
            directory = self.repo / 'nested' / slug
            directory.mkdir(parents=True)
            (directory / 'SKILL.md').write_text(f'---\nname: {slug}\ndescription: Fixture evidence.\n---\n# Evidence\nHello.\n')
        (self.repo / 'nested/fixture-one/asset.bin').write_bytes(b'\x00\xff\r\n')
        self.commit()
        self.projects = []
        self.commands = []
        self.damage = None

    def commit(self):
        self.real_git(self.repo, 'add', '.')
        self.real_git(self.repo, '-c', 'commit.gpgsign=false', 'commit', '-qm', 'Fixture')
        self.commit_id = self.real_git(self.repo, 'rev-parse', 'HEAD').decode().strip()

    def fixture_git(self, repo, *args):
        if args[0] == 'fetch':
            args = (*args[:-2], str(self.repo), args[-1])
        return self.real_git(repo, *args)

    def fixture_run(self, command, **kwargs):
        if command[0] != 'npx':
            return self.real_run(command, **kwargs)
        self.commands.append(command)
        project = Path(kwargs['cwd'])
        self.projects.append(project)
        names = command[command.index('--skill') + 1:command.index('--agent')]
        for name in names:
            for path in (self.repo / 'nested' / name).rglob('*'):
                if path.is_file():
                    target = project / '.agents/skills' / name / path.relative_to(self.repo / 'nested' / name)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(path.read_bytes())
                    target.chmod(path.stat().st_mode & 0o777)
        if self.damage:
            self.damage(project / '.agents/skills')
        return subprocess.CompletedProcess(command, 0, '', '')

    def acquire(self, **kwargs):
        with patch.object(a.snapshot, 'git', side_effect=self.fixture_git), patch.object(
                a.subprocess, 'run', side_effect=self.fixture_run):
            return a.acquire('fixture/upstream', cli_version='1.6.0', **kwargs)

    def test_nested_payload_and_provenance_survive_staging_cleanup(self):
        bundle, resolution = self.acquire(skills=['fixture-one'])
        self.assertEqual(self.commit_id, bundle['git_commit'])
        self.assertEqual('HEAD', resolution['requested_ref'])
        skill = bundle['skills'][0]
        self.assertEqual('nested/fixture-one', skill['source_directory'])
        artifact = next(f for f in skill['files'] if f['path'] == 'asset.bin')
        self.assertEqual(b'\x00\xff\r\n', base64.b64decode(artifact['content_base64']))
        self.assertIn('nested/fixture-one', a.snapshot.sql(bundle))
        self.assertFalse(self.projects[0].exists())
        self.assertEqual(1, len(self.commands))
        self.assertIn('https://github.com/fixture/upstream/tree/' + self.commit_id, self.commands[0])
        self.assertEqual(self.commands[0], bundle['acquisition']['command'])

    def test_all_selection_and_explicit_revision(self):
        bundle, resolution = self.acquire(revision=self.commit_id, all_skills=True)
        self.assertEqual(['fixture-one', 'fixture-two'], [s['slug'] for s in bundle['skills']])
        self.assertEqual(self.commit_id, resolution['requested_ref'])

    def test_missing_and_ambiguous_selection_fail(self):
        with self.assertRaisesRegex(ValueError, 'not found'):
            self.acquire(skills=['absent'])
        directory = self.repo / 'duplicate'
        directory.mkdir()
        (directory / 'SKILL.md').write_bytes((self.repo / 'nested/fixture-one/SKILL.md').read_bytes())
        self.commit()
        with self.assertRaisesRegex(ValueError, 'Ambiguous'):
            self.acquire(all_skills=True)
        self.assertEqual([], self.commands)

    def test_missing_transformed_extra_and_mode_changed_files_fail(self):
        damages = [lambda root: (root / 'fixture-one/asset.bin').unlink(),
                   lambda root: (root / 'fixture-one/asset.bin').write_bytes(b'changed'),
                   lambda root: (root / 'fixture-one/extra.txt').write_bytes(b'extra'),
                   lambda root: (root / 'fixture-one/SKILL.md').chmod(0o755)]
        for damage in damages:
            self.damage = damage
            with self.subTest(damage=damage), self.assertRaises(ValueError):
                self.acquire(skills=['fixture-one'])
            self.assertFalse(self.projects[-1].exists())

    def test_nested_support_files_and_executable_mode_are_preserved(self):
        directory = self.repo / 'nested/fixture-one/scripts'
        directory.mkdir()
        script = directory / 'probe.sh'
        script.write_bytes(b'#!/bin/sh\nexit 91\n')
        script.chmod(0o755)
        self.commit()
        bundle, _ = self.acquire(skills=['fixture-one'])
        artifact = next(f for f in bundle['skills'][0]['files'] if f['path'] == 'scripts/probe.sh')
        self.assertEqual('100755', artifact['git_mode'])
        self.assertEqual(script.read_bytes(), base64.b64decode(artifact['content_base64']))

    def test_unsafe_selected_symlink_fails_before_installer(self):
        (self.repo / 'nested/fixture-one/link').symlink_to('/etc/passwd')
        self.commit()
        with self.assertRaisesRegex(ValueError, 'Unsupported Git mode'):
            self.acquire(skills=['fixture-one'])
        self.assertEqual([], self.commands)

    def test_selection_required_and_mutually_exclusive(self):
        for args in ({}, {'skills': ['fixture-one'], 'all_skills': True},
                     {'skills': ['fixture-one', 'fixture-one']}):
            with self.subTest(args=args), self.assertRaises(ValueError):
                self.acquire(**args)


if __name__ == '__main__':
    unittest.main()
