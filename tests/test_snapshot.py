"""Verify downloaded payloads against a disposable upstream Git repository."""
import base64
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('snapshot_importer', ROOT / 'scripts/import_skills.py')
importer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(importer)


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name) / 'upstream'
        self.repo.mkdir()
        self.payload = Path(self.temp.name) / 'installed'
        self.git('init', '-q')
        self.git('config', 'user.name', 'Snapshot test')
        self.git('config', 'user.email', 'snapshot@example.invalid')
        for slug in importer.SKILLS:
            directory = self.repo / slug
            directory.mkdir()
            (directory / 'SKILL.md').write_text(
                f'---\nname: {slug}\ndescription: Diagnose the fixture symptom.\n---\n'
                '# Evidence\nFirst revision.\n')
        self.commit()

    def git(self, *args):
        return subprocess.check_output(
            ['git', '-C', str(self.repo), *args], stderr=subprocess.DEVNULL
        ).decode().strip()

    def commit(self):
        self.git('add', '.')
        self.git('-c', 'commit.gpgsign=false', 'commit', '-qm', 'Fixture revision')
        return self.git('rev-parse', 'HEAD')

    def stage(self, commit=None):
        commit = commit or self.git('rev-parse', 'HEAD')
        inventory = importer.source_inventory(self.repo, commit)
        for path, entry in inventory.items():
            target = self.payload / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(entry['raw'])
            target.chmod(int(entry['mode'], 8) & 0o777)
        return inventory

    def test_inventory_uses_committed_bytes(self):
        commit = self.git('rev-parse', 'HEAD')
        before = importer.source_inventory(self.repo, commit)
        path = self.repo / importer.SKILLS[0] / 'SKILL.md'
        path.write_text(path.read_text() + 'New observation.\n')
        (path.parent / 'untracked.md').write_text('Not published.\n')
        self.assertEqual(before, importer.source_inventory(self.repo, commit))
        after_commit = self.commit()
        self.assertNotEqual(before, importer.source_inventory(self.repo, after_commit))
        self.assertEqual(before, importer.source_inventory(self.repo, commit))

    def test_source_symlink_is_rejected(self):
        (self.repo / importer.SKILLS[0] / 'outside.md').symlink_to('/etc/passwd')
        commit = self.commit()
        with self.assertRaises(ValueError):
            importer.source_inventory(self.repo, commit)

    def test_unselected_external_symlink_is_rejected(self):
        (self.repo / 'unselected').mkdir()
        (self.repo / 'unselected' / 'SKILL.md').symlink_to('/etc/passwd')
        commit = self.commit()
        with self.assertRaisesRegex(ValueError, 'Unsafe unselected source symlink'):
            importer.source_inventory(self.repo, commit)

    def test_unselected_documentation_link_to_tracked_file_is_allowed(self):
        before = importer.source_inventory(self.repo, self.git('rev-parse', 'HEAD'))
        (self.repo / 'AGENTS.md').write_text('Repository instructions.\n')
        (self.repo / 'CLAUDE.md').symlink_to('AGENTS.md')
        commit = self.commit()
        self.assertEqual(before, importer.source_inventory(self.repo, commit))

    def test_gitlink_is_rejected(self):
        commit = self.git('rev-parse', 'HEAD')
        self.git('update-index', '--add', '--cacheinfo',
                 f'160000,{commit},{importer.SKILLS[0]}/nested')
        self.git('-c', 'commit.gpgsign=false', 'commit', '-qm', 'Gitlink fixture')
        with self.assertRaisesRegex(ValueError, 'Unsupported Git mode 160000'):
            importer.source_inventory(self.repo, self.git('rev-parse', 'HEAD'))

    def test_backslash_in_source_path_is_rejected(self):
        (self.repo / importer.SKILLS[0] / 'unsafe\\name.md').write_text('Invalid path.\n')
        commit = self.commit()
        with self.assertRaisesRegex(ValueError, 'Unsafe source path'):
            importer.source_inventory(self.repo, commit)

    def test_missing_source_skill_is_rejected(self):
        shutil.rmtree(self.repo / importer.SKILLS[0])
        commit = self.commit()
        with self.assertRaises(ValueError):
            importer.source_inventory(self.repo, commit)

    def test_downloaded_bytes_match_the_complete_inventory(self):
        slug = importer.SKILLS[0]
        (self.repo / slug / 'metadata.json').write_bytes(b'{"extra": true}\r\n')
        (self.repo / slug / 'asset.bin').write_bytes(b'\x00\xff\x80\r\n')
        script = self.repo / slug / 'script.sh'
        script.write_bytes(b'#!/bin/sh\nexit 93\n')
        script.chmod(0o755)
        self.commit()
        inventory = self.stage()
        verified = importer.verify_payload(self.payload, inventory)
        self.assertEqual(inventory, verified)
        self.assertEqual(b'\x00\xff\x80\r\n', verified[f'{slug}/asset.bin']['raw'])

    def test_missing_installer_file_is_rejected(self):
        slug = importer.SKILLS[0]
        (self.repo / slug / 'metadata.json').write_text('{}\n')
        self.commit()
        inventory = self.stage()
        (self.payload / slug / 'metadata.json').unlink()
        with self.assertRaises(ValueError):
            importer.verify_payload(self.payload, inventory)

    def test_transformed_installer_file_is_rejected(self):
        inventory = self.stage()
        path = self.payload / importer.SKILLS[0] / 'SKILL.md'
        path.write_bytes(path.read_bytes().replace(b'\n', b'\r\n'))
        with self.assertRaises(ValueError):
            importer.verify_payload(self.payload, inventory)

    def test_unexpected_installer_file_is_rejected(self):
        inventory = self.stage()
        (self.payload / importer.SKILLS[0] / 'injected.txt').write_text('Unexpected')
        with self.assertRaises(ValueError):
            importer.verify_payload(self.payload, inventory)

    def test_installer_symlink_is_rejected_even_with_matching_bytes(self):
        inventory = self.stage()
        path = self.payload / importer.SKILLS[0] / 'SKILL.md'
        path.unlink()
        path.symlink_to(self.repo / importer.SKILLS[0] / 'SKILL.md')
        with self.assertRaises(ValueError):
            importer.verify_payload(self.payload, inventory)

    def bundle(self, commit=None):
        commit = commit or self.git('rev-parse', 'HEAD')
        inventory = self.stage(commit)
        payload = importer.verify_payload(self.payload, inventory)
        acquisition = importer.acquisition_record(payload, importer.SOURCE_URL, commit)
        return importer.build_bundle(payload, commit, acquisition)

    def test_bundle_keeps_binary_files_after_staging_is_removed(self):
        slug = importer.SKILLS[0]
        raw = b'\x00\xff\x80\r\n'
        (self.repo / slug / 'asset.bin').write_bytes(raw)
        self.commit()
        bundle = self.bundle()
        shutil.rmtree(self.payload)
        shutil.rmtree(self.repo)
        artifact = next(file for file in bundle['skills'][0]['files'] if file['path'] == 'asset.bin')
        self.assertEqual(raw, base64.b64decode(artifact['content_base64']))
        self.assertEqual([], artifact['sections'])
        self.assertEqual([], artifact['pins'])
        self.assertEqual([], artifact['evals'])
        self.assertIn('catalog.source_file', importer.sql(bundle))

    def test_revision_changes_version_but_acquisition_time_does_not(self):
        before = self.bundle()
        again = self.bundle()
        self.assertEqual(before['skills'], again['skills'])
        path = self.repo / importer.SKILLS[0] / 'SKILL.md'
        path.write_text(path.read_text() + 'New observation.\n')
        self.commit()
        after = self.bundle()
        self.assertNotEqual(before['skills'][0]['version_id'], after['skills'][0]['version_id'])

    def test_invalid_frontmatter_is_rejected(self):
        path = self.repo / importer.SKILLS[0] / 'SKILL.md'
        path.write_text('---\nname: wrong-name\ndescription: Invalid fixture.\n---\n')
        self.commit()
        with self.assertRaises(ValueError):
            self.bundle()

    def test_acquisition_requires_remote_source_and_full_revision(self):
        with patch.object(importer, 'git') as git:
            for revision in (None, 'HEAD', 'main', 'abc123', 'a' * 39, 'A' * 40):
                with self.subTest(revision=revision), self.assertRaises(ValueError):
                    importer.build(revision=revision)
            with self.assertRaises(ValueError):
                importer.build(source=str(self.repo), revision=self.git('rev-parse', 'HEAD'))
            git.assert_not_called()

    def test_build_runs_pinned_installer_and_removes_staging(self):
        commit = self.git('rev-parse', 'HEAD')
        inventory = importer.source_inventory(self.repo, commit)
        real_git = importer.git
        real_run = subprocess.run
        projects = []
        commands = []

        def fixture_git(repo, *args):
            if args[0] == 'fetch':
                self.assertEqual(importer.SOURCE_URL, args[-2])
                args = (*args[:-2], str(self.repo), args[-1])
            return real_git(repo, *args)

        def fixture_run(command, **kwargs):
            if command[0] != 'npx':
                return real_run(command, **kwargs)
            commands.append(command)
            projects.append(Path(kwargs['cwd']))
            for path, entry in inventory.items():
                target = projects[-1] / '.agents' / 'skills' / path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(entry['raw'])
            return subprocess.CompletedProcess(command, 0, '', '')

        with patch.object(importer, 'git', side_effect=fixture_git), patch.object(
                importer.subprocess, 'run', side_effect=fixture_run):
            bundle = importer.build(revision=commit)
        self.assertEqual(1, len(commands))
        command = commands[0]
        self.assertIn(f'skills@{importer.SKILLS_CLI_VERSION}', command)
        self.assertIn(f'{importer.SOURCE_URL}/tree/{commit}', command)
        self.assertIn('--copy', command)
        self.assertEqual(importer.SKILLS, command[command.index('--skill') + 1:command.index('--agent')])
        self.assertEqual('codex', command[command.index('--agent') + 1])
        self.assertFalse(projects[0].exists())
        self.assertEqual(commit, bundle['git_commit'])
        self.assertEqual(command, bundle['acquisition']['command'])
        self.assertEqual(importer.SKILLS_CLI_VERSION, bundle['acquisition']['skills_cli_version'])
        self.assertEqual(importer.SOURCE_URL, bundle['acquisition']['repository_url'])
        self.assertIn('acquired_at', bundle['acquisition'])
        self.assertNotIn(str(projects[0]), importer.sql(bundle))

    def test_installer_failure_removes_staging_without_returning_a_bundle(self):
        commit = self.git('rev-parse', 'HEAD')
        inventory = importer.source_inventory(self.repo, commit)
        projects = []

        def failed_install(command, **kwargs):
            projects.append(Path(kwargs['cwd']))
            return subprocess.CompletedProcess(command, 1, '', 'download failed')

        with patch.object(importer, 'git', return_value=commit.encode()), patch.object(
                importer, 'source_inventory', return_value=inventory), patch.object(
                importer.subprocess, 'run', side_effect=failed_install):
            with self.assertRaisesRegex(ValueError, 'download failed'):
                importer.build(revision=commit)
        self.assertEqual(1, len(projects))
        self.assertFalse(projects[0].exists())

    def write_snapshot(self, bundle):
        output = Path(self.temp.name) / 'data'
        output.mkdir(exist_ok=True)
        (output / 'skills.json').write_text(json.dumps(bundle))
        (output / 'skills.sql').write_text(importer.sql(bundle))
        return output

    def test_check_keeps_recorded_acquisition_time_and_sql(self):
        recorded = self.bundle()
        output = self.write_snapshot(recorded)
        expected = {path.name: path.read_bytes() for path in output.iterdir()}
        fresh = self.bundle()
        self.assertNotEqual(recorded['acquisition']['acquired_at'], fresh['acquisition']['acquired_at'])
        importer.check_artifacts(fresh, output)
        self.assertEqual(expected, {path.name: path.read_bytes() for path in output.iterdir()})

    def test_check_rejects_tampered_provenance(self):
        bundle = self.bundle()
        bundle['acquisition']['acquired_at'] = '2000-01-01T00:00:00+00:00'
        output = self.write_snapshot(bundle)
        with self.assertRaisesRegex(ValueError, 'acquisition digest'):
            importer.check_artifacts(self.bundle(), output)

    def test_check_rejects_changed_command_even_with_valid_acquisition_id(self):
        bundle = self.bundle()
        bundle['acquisition']['command'][2] = 'skills@0.0.0'
        provenance = dict(bundle['acquisition'])
        del provenance['acquisition_id']
        bundle['acquisition']['acquisition_id'] = hashlib.sha256(
            json.dumps(provenance, sort_keys=True).encode()).hexdigest()
        output = self.write_snapshot(bundle)
        with self.assertRaisesRegex(ValueError, 'skills.json'):
            importer.check_artifacts(self.bundle(), output)

    def test_check_rejects_altered_acquisition_id(self):
        bundle = self.bundle()
        bundle['acquisition']['acquisition_id'] = '0' * 64
        output = self.write_snapshot(bundle)
        with self.assertRaisesRegex(ValueError, 'acquisition digest'):
            importer.check_artifacts(self.bundle(), output)

    def test_successful_installer_with_missing_payload_fails_and_removes_staging(self):
        commit = self.git('rev-parse', 'HEAD')
        inventory = importer.source_inventory(self.repo, commit)
        projects = []

        def incomplete_install(command, **kwargs):
            projects.append(Path(kwargs['cwd']))
            payload = projects[-1] / '.agents' / 'skills'
            payload.mkdir(parents=True)
            for path, entry in list(inventory.items())[1:]:
                target = payload / path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(entry['raw'])
            return subprocess.CompletedProcess(command, 0, '', '')

        with patch.object(importer, 'git', return_value=commit.encode()), patch.object(
                importer, 'source_inventory', return_value=inventory), patch.object(
                importer.subprocess, 'run', side_effect=incomplete_install):
            with self.assertRaisesRegex(ValueError, 'Installed inventory differs'):
                importer.build(revision=commit)
        self.assertEqual(1, len(projects))
        self.assertFalse(projects[0].exists())

    def test_check_rejects_changed_content_and_sql(self):
        bundle = self.bundle()
        output = self.write_snapshot(bundle)
        path = self.repo / importer.SKILLS[0] / 'SKILL.md'
        path.write_text(path.read_text() + 'New observation.\n')
        self.commit()
        with self.assertRaisesRegex(ValueError, 'skills.json'):
            importer.check_artifacts(self.bundle(), output)
        (output / 'skills.sql').write_text('-- truncated\n')
        with self.assertRaisesRegex(ValueError, 'skills.sql'):
            importer.check_artifacts(bundle, output)

    def test_installer_directory_symlink_is_rejected(self):
        inventory = self.stage()
        path = self.payload / importer.SKILLS[0]
        shutil.rmtree(path)
        path.symlink_to(self.repo / importer.SKILLS[0], target_is_directory=True)
        with self.assertRaises(ValueError):
            importer.verify_payload(self.payload, inventory)


if __name__ == '__main__':
    unittest.main()
