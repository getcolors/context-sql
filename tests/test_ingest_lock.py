"""Lock replay, selection updates, and publication failure behavior without network."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from package_context_sql_blue import ingest, ingest_lock as locks

ROOT = Path(__file__).resolve().parents[1]


class LockTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.snapshot = json.loads((ROOT / 'data/skills.json').read_text())
        cls.names = sorted(s['slug'] for s in cls.snapshot['skills'])[:2]

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'context-skills.lock.json'
        self.resolution = {'requested_source': 'getcolors/skills', 'requested_ref': 'main'}
        self.bundle = self.subset(self.names[:1])
        self.document = locks.document([locks.entry_from_bundle(self.bundle, self.resolution)])
        self.path.write_text(json.dumps(self.document))
        self.original = self.path.read_bytes()
        self.cli = patch.object(ingest, 'resolve_cli_version', return_value='1.9.0').start()
        self.addCleanup(patch.stopall)
        self.acquirer = patch.object(ingest, 'acquire', side_effect=self.fake_acquire).start()
        self.commit = patch.object(ingest, 'commit_import', side_effect=lambda b, r, d, *a: locks.digest(d)).start()

    def subset(self, names, version='1.6.0'):
        bundle = deepcopy(self.snapshot)
        bundle['skills'] = [s for s in bundle['skills'] if s['slug'] in names]
        manifest = [{'path': s['slug'] + '/' + f['path'], 'mode': f['git_mode'], 'sha256': f['sha256']}
                    for s in sorted(bundle['skills'], key=lambda s: s['slug'])
                    for f in sorted(s['files'], key=lambda f: f['path'])]
        bundle['acquisition']['manifest_sha256'] = hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest()
        bundle['acquisition']['skills_cli_version'] = version
        return bundle

    def fake_acquire(self, source, revision=None, *, skills=None, all_skills=False, cli_version=None):
        return self.subset(self.names if all_skills else skills, cli_version), dict(self.resolution)

    def args(self, *args):
        return ingest.parser().parse_args([*args, '--lockfile', str(self.path)])

    def pending(self):
        return list(self.path.parent.glob('*.pending'))

    def test_ingest_merges_existing_selection(self):
        result = ingest.execute(self.args('ingest', 'getcolors/skills', '--skill', self.names[1]))
        self.assertTrue(result['committed'])
        self.assertEqual(self.names, self.acquirer.call_args.kwargs['skills'])
        locked = locks.load(self.path)
        self.assertEqual(self.names, [s['name'] for s in locked['imports'][0]['skills']])
        self.assertEqual(1, len(locked['imports']))

    def retained_repository(self):
        bundle = self.subset(self.names[1:])
        bundle['repository_url'] = 'https://github.com/another/skills'
        resolution = {'requested_source': 'another/skills', 'requested_ref': 'main'}
        entry = locks.entry_from_bundle(bundle, resolution, all_skills=True)
        self.document = locks.document([self.document['imports'][0], entry])
        self.path.write_text(json.dumps(self.document))
        self.original = self.path.read_bytes()
        return bundle, resolution

    def test_ingest_reproduces_retained_repositories_at_locked_commits(self):
        retained, resolution = self.retained_repository()
        def acquire(source, revision=None, **kwargs):
            if source == retained['repository_url']:
                self.assertEqual(retained['git_commit'], revision)
                self.assertEqual(self.names[1:], kwargs['skills'])
                self.assertFalse(kwargs.get('all_skills', False))
                return retained, resolution
            return self.fake_acquire(source, revision, **kwargs)
        self.acquirer.side_effect = acquire
        ingest.execute(self.args('ingest', 'getcolors/skills', '--skill', self.names[0]))
        self.assertEqual(2, self.acquirer.call_count)
        bundles = self.commit.call_args.args[0]
        self.assertEqual({self.snapshot['repository_url'], retained['repository_url']},
                         {bundle['repository_url'] for bundle in bundles})
        self.assertEqual(2, len(locks.load(self.path)['imports']))

    def test_failed_retained_repository_verification_preserves_previous_lock(self):
        retained, resolution = self.retained_repository()
        retained['skills'][0]['routing_description'] += ' Changed projection.'
        def acquire(source, revision=None, **kwargs):
            if source == retained['repository_url']:
                return retained, resolution
            return self.fake_acquire(source, revision, **kwargs)
        self.acquirer.side_effect = acquire
        with self.assertRaisesRegex(ValueError, 'projections differ'):
            ingest.execute(self.args('ingest', 'getcolors/skills', '--skill', self.names[0]))
        self.commit.assert_not_called()
        self.assertEqual(self.original, self.path.read_bytes())
        self.assertEqual([], self.pending())

    def test_sync_replays_exact_commit_and_all_selection_without_expanding(self):
        self.document['imports'][0]['selection']['all'] = True
        self.path.write_text(json.dumps(self.document))
        before = self.path.read_bytes()
        result = ingest.execute(self.args('sync', '--locked'))
        self.assertTrue(result['committed'])
        self.assertEqual((self.snapshot['repository_url'], self.snapshot['git_commit']), self.acquirer.call_args.args)
        self.assertEqual(self.names[:1], self.acquirer.call_args.kwargs['skills'])
        self.assertNotIn('all_skills', self.acquirer.call_args.kwargs)
        self.assertEqual('1.9.0', result['imports'][0]['acquisitionCliVersion'])
        self.assertEqual(before, self.path.read_bytes())
        self.assertEqual([], self.pending())

    def test_update_resolves_original_reference_and_expands_all(self):
        self.document['imports'][0]['selection']['all'] = True
        self.path.write_text(json.dumps(self.document))
        ingest.execute(self.args('update'))
        self.assertEqual(('getcolors/skills', 'main'), self.acquirer.call_args.args)
        self.assertTrue(self.acquirer.call_args.kwargs['all_skills'])
        self.assertIsNone(self.acquirer.call_args.kwargs['skills'])
        self.assertEqual(2, len(locks.load(self.path)['imports'][0]['skills']))

    def test_tampered_manifest_and_projection_fail_before_database(self):
        for field in ('manifestSha256', 'projectionSha256'):
            with self.subTest(field=field):
                doc = deepcopy(self.document)
                doc['imports'][0][field] = '0' * 64
                self.path.write_text(json.dumps(doc))
                before = self.path.read_bytes()
                with self.assertRaises(ValueError):
                    ingest.execute(self.args('sync', '--locked'))
                self.commit.assert_not_called()
                self.assertEqual(before, self.path.read_bytes())
                self.assertEqual([], self.pending())

    def test_changed_derived_records_fail_even_with_identical_source_files(self):
        bundle = deepcopy(self.bundle)
        bundle['skills'][0]['routing_description'] += ' Altered projection.'
        self.acquirer.side_effect = None
        self.acquirer.return_value = bundle, self.resolution
        with self.assertRaisesRegex(ValueError, 'projections differ'):
            ingest.execute(self.args('sync', '--locked'))
        self.commit.assert_not_called()
        self.assertEqual(self.original, self.path.read_bytes())

    def test_dry_run_does_not_write_database_lock_or_guard(self):
        result = ingest.execute(self.args('ingest', 'getcolors/skills', '--skill', self.names[1], '--dry-run'))
        self.assertTrue(result['dryRun'])
        self.commit.assert_not_called()
        self.assertEqual(self.original, self.path.read_bytes())
        self.assertEqual([self.path], list(self.path.parent.iterdir()))

    def test_dry_run_new_destination_does_not_create_directories(self):
        self.path = self.path.parent / 'absent' / 'lock.json'
        ingest.execute(self.args('ingest', 'getcolors/skills', '--skill', self.names[0], '--dry-run'))
        self.commit.assert_not_called()
        self.assertFalse(self.path.parent.exists())

    def test_same_skill_name_cannot_be_owned_by_two_repositories(self):
        other = deepcopy(self.document['imports'][0])
        other.update(repository='https://github.com/another/skills', requestedSource='another/skills')
        with self.assertRaisesRegex(ValueError, 'globally unique'):
            locks.document([self.document['imports'][0], other])

    def test_database_failure_preserves_lock_and_removes_pending(self):
        self.commit.side_effect = RuntimeError('Database unavailable')
        with self.assertRaisesRegex(RuntimeError, 'Database unavailable'):
            ingest.execute(self.args('ingest', 'getcolors/skills', '--skill', self.names[1]))
        self.assertEqual(self.original, self.path.read_bytes())
        self.assertEqual([], self.pending())

    def test_publication_failure_can_recover_database_snapshot(self):
        with patch.object(ingest, 'publish_lock', side_effect=OSError('Filesystem unavailable')):
            with self.assertRaises(ingest.PublicationError) as failure:
                ingest.execute(self.args('ingest', 'getcolors/skills', '--skill', self.names[1]))
        doc = self.commit.call_args.args[2]
        digest = locks.digest(doc)
        self.assertIn(digest, str(failure.exception))
        self.assertIn('export-lock', str(failure.exception))
        self.assertEqual(self.original, self.path.read_bytes())
        self.assertEqual(1, len(self.pending()))
        with patch.object(ingest, 'export_lock', return_value=doc) as export:
            result = ingest.execute(self.args('export-lock', '--digest', digest))
        self.assertEqual(digest, result['lockDigest'])
        self.assertEqual(doc, locks.load(self.path))
        self.assertEqual(digest, export.call_args.args[2])

    def test_recovery_checks_database_snapshot_digest(self):
        with patch.object(ingest, 'export_lock', return_value=self.document):
            with self.assertRaisesRegex(ValueError, 'digest differs'):
                ingest.execute(self.args('export-lock', '--digest', '0' * 64))
        self.assertEqual(self.original, self.path.read_bytes())

    def test_untrusted_lock_schema(self):
        mutations = [
            lambda d: d.update(secret='untrusted'),
            lambda d: d.update(format=True),
            lambda d: d['imports'].append(deepcopy(d['imports'][0])),
            lambda d: d['imports'][0].update(commit='main'),
            lambda d: d['imports'][0].update(parserVersion='unknown'),
            lambda d: d['imports'][0]['skills'].append(deepcopy(d['imports'][0]['skills'][0])),
            lambda d: d['imports'][0]['skills'][0].update(sourceDirectory='../escape'),
            lambda d: d['imports'][0]['manifest'][0].update(path='../escape'),
            lambda d: d['imports'][0]['manifest'][0].update(mode='120000'),
            lambda d: d['imports'][0]['selection'].update(all='true'),
        ]
        for index, mutate in enumerate(mutations):
            with self.subTest(index=index):
                doc = deepcopy(self.document)
                mutate(doc)
                with self.assertRaises(ValueError):
                    locks.validate(doc)

    def test_duplicate_json_keys_and_symlinks_are_rejected(self):
        self.path.write_text('{"format":1,"format":1,"imports":[]}')
        with self.assertRaisesRegex(ValueError, 'Duplicate JSON key'):
            locks.load(self.path)
        link = self.path.with_name('linked.json')
        link.symlink_to(self.path)
        with self.assertRaisesRegex(ValueError, 'not a symlink'):
            locks.load(link)


if __name__ == '__main__':
    unittest.main()
