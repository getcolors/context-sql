"""Exercise revision identity and unsafe inputs with a real disposable Git repo."""
import importlib.util
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('snapshot_importer', ROOT / 'scripts/import_skills.py')
importer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(importer)


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name)
        self.git('init', '-q')
        self.git('config', 'user.name', 'Snapshot test')
        self.git('config', 'user.email', 'snapshot@example.invalid')
        self.git('remote', 'add', 'origin', 'https://github.com/getcolors/skills.git')
        for slug in importer.SKILLS:
            directory = self.repo / slug
            directory.mkdir()
            (directory / 'SKILL.md').write_text(f'---\nname: {slug}\ndescription: Diagnose the fixture symptom.\n---\n# Evidence\nFirst revision.\n')
        self.commit()

    def git(self, *args):
        return subprocess.check_output(['git', '-C', str(self.repo), *args], stderr=subprocess.DEVNULL).decode().strip()

    def commit(self):
        self.git('add', '.')
        self.git('-c', 'commit.gpgsign=false', 'commit', '-qm', 'Fixture revision')

    def test_committed_change_versions_and_dirty_edits_do_not(self):
        before = importer.build(self.repo)
        path = self.repo / importer.SKILLS[0] / 'SKILL.md'
        path.write_text(path.read_text() + 'New observation.\n')
        self.assertEqual(before, importer.build(self.repo))
        self.commit()
        after = importer.build(self.repo)
        self.assertNotEqual(before['skills'][0]['version_id'], after['skills'][0]['version_id'])
        self.assertEqual(before, importer.build(self.repo, before['git_commit']))

    def test_symlink_is_rejected(self):
        (self.repo / importer.SKILLS[0] / 'outside.md').symlink_to('/etc/passwd')
        self.commit()
        with self.assertRaises(ValueError):
            importer.build(self.repo)

    def test_invalid_frontmatter_is_rejected(self):
        path = self.repo / importer.SKILLS[0] / 'SKILL.md'
        path.write_text('---\nname: wrong-name\ndescription: Invalid fixture.\n---\n')
        self.commit()
        with self.assertRaises(ValueError):
            importer.build(self.repo)

    def test_wrong_origin_is_rejected(self):
        self.git('remote', 'set-url', 'origin', 'https://github.com/example/unrelated.git')
        with self.assertRaises(ValueError):
            importer.build(self.repo)
