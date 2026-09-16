import base64
import hashlib
import importlib.util
import json
from pathlib import Path
import unittest

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('importer',ROOT/'scripts/import_skills.py')
module=importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class ImportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bundle=json.loads((ROOT/'data/skills.json').read_text())

    def test_inventory_classification(self):
        self.assertEqual(set(module.SKILLS),{s['slug'] for s in self.bundle['skills']})
        self.assertEqual(13,sum(s['kind']=='context' for s in self.bundle['skills']))
        self.assertEqual(['refresh-oci-token'],[s['slug'] for s in self.bundle['skills'] if s['kind']=='generic'])

    def test_lossless_hash_and_citations(self):
        for skill in self.bundle['skills']:
            for artifact in skill['files']:
                raw=base64.b64decode(artifact['content_base64'],validate=True)
                self.assertEqual(artifact['sha256'],hashlib.sha256(raw).hexdigest())
                lines=raw.decode().splitlines(keepends=True)
                self.assertEqual(artifact['line_count'],len(lines))
                if artifact['path'].endswith('.md'):
                    self.assertEqual(raw.decode(),''.join(s['body'] for s in artifact['sections']))
                for section in artifact['sections']:
                    self.assertEqual(section['body'],''.join(lines[section['start_line']-1:section['end_line']]))
                for pin in artifact['pins']:
                    self.assertIn(pin['component'],lines[pin['line_number']-1])
                if artifact['evals']:
                    self.assertEqual(artifact['evals'],json.loads(raw)['evals'])

    def test_fenced_headings_are_not_sections(self):
        text='# Heading\n```sh\n# shell comment\n```\n## Sub\nbody\n'
        sections=module.sections(text,'SKILL.md')
        self.assertEqual(['Heading','Sub'],[s['heading'] for s in sections])
        self.assertEqual(['Heading','Sub'],sections[1]['heading_path'])

    def test_committed_sql_matches_manifest(self):
        self.assertEqual((ROOT/'data/skills.sql').read_text(),module.sql(self.bundle))

    def test_literal_sql_escaping(self):
        self.assertEqual("'can''t $not_shell$'",module.lit("can't $not_shell$"))


if __name__=='__main__': unittest.main()
