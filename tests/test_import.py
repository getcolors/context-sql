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
                try:
                    text = raw.decode('utf-8')
                except UnicodeDecodeError:
                    self.assertEqual([], artifact['sections'])
                    self.assertEqual([], artifact['pins'])
                    self.assertEqual([], artifact['evals'])
                    continue
                lines=text.splitlines(keepends=True)
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

    def test_fence_with_trailing_text_does_not_end_code(self):
        text = '# Heading\n```sh\n```still-code\n# shell comment\n```\n## Sub\nbody\n'
        sections = module.sections(text, 'SKILL.md')
        self.assertEqual(['Heading', 'Sub'], [s['heading'] for s in sections])
        self.assertEqual(text, ''.join(s['body'] for s in sections))

    def test_fenced_tables_are_not_pin_evidence(self):
        text = ('# Pins\n```md\n| Component | Version |\n| --- | --- |\n'
                '| example-only | v0 |\n```\n'
                '| Component | Version |\n| --- | --- |\n| actual | v1 |\n')
        section = module.sections(text, 'references/pins.md')[0]
        pins = module.pin_rows(section)
        self.assertEqual(['actual'], [pin['component'] for pin in pins])
        self.assertEqual(9, pins[0]['line_number'])

    def test_description_may_contain_yaml_delimiter_text(self):
        description = 'Diagnose --- including the important failure condition.'
        metadata = module.frontmatter(
            f'---\nname: fixture\ndescription: {description}\n---\n# Body\n', 'fixture')
        self.assertEqual(description, metadata['description'])

    def test_frontmatter_requires_delimiters_and_a_mapping(self):
        for text in ('---\n- list\n---\n', 'name: fixture\n',
                     '---\nname: fixture\ndescription: Missing close.\n'):
            with self.subTest(text=text), self.assertRaises(ValueError):
                module.frontmatter(text, 'fixture')

    def test_unicode_separators_do_not_change_citation_line_numbers(self):
        text = '# Heading\nFirst\u2028same line\u2029still same line\n## Next\nLast\n'
        sections = module.sections(text, 'SKILL.md')
        self.assertEqual([(1, 2), (3, 4)],
                         [(section['start_line'], section['end_line']) for section in sections])
        self.assertEqual(text, ''.join(section['body'] for section in sections))
        self.assertEqual(['# Heading\n', 'First\u2028same line\u2029still same line\n',
                          '## Next\n', 'Last\n'], module.source_lines(text))

    def test_committed_sql_matches_manifest(self):
        self.assertEqual((ROOT/'data/skills.sql').read_text(),module.sql(self.bundle))

    def test_literal_sql_escaping(self):
        self.assertEqual("'can''t $not_shell$'",module.lit("can't $not_shell$"))


if __name__=='__main__': unittest.main()
