#!/usr/bin/env python3
"""Check database bytes and acquisition history in the disposable test cluster."""
import base64
from datetime import datetime
import hashlib
import json
from pathlib import Path
import subprocess
import uuid


ROOT = Path(__file__).resolve().parents[1]


def psql(statement, database=None):
    command = ['psql', '-X', '-qAt', '-v', 'ON_ERROR_STOP=1']
    if database:
        command.extend(['-d', database])
    return subprocess.check_output(command, input=statement, text=True)


def rows(query, database=None):
    return [json.loads(line) for line in psql(
        f'SELECT row_to_json(result) FROM ({query}) result;', database).splitlines()]


def check_snapshot(bundle, database=None, historical=False):
    expected = {(skill['slug'], skill['version_id'], file['path']): file
                for skill in bundle['skills'] for file in skill['files']}
    stored = rows("SELECT skill_slug, version_id, path, encode(content, 'base64') AS content, "
                  "sha256, git_mode, line_count FROM catalog.source_file", database)
    actual = {(row['skill_slug'], row['version_id'], row['path']): row for row in stored}
    extra = set(actual) - set(expected)
    assert extra == ({('historical-skill', 'a' * 64, 'SKILL.md')} if historical else set()), extra
    assert set(expected) <= set(actual), 'Database lost source files'
    manifest = []
    for key, file in expected.items():
        row = actual[key]
        raw = base64.b64decode(row['content'])
        assert raw == base64.b64decode(file['content_base64']), key
        assert row['sha256'] == file['sha256'] == hashlib.sha256(raw).hexdigest(), key
        assert row['git_mode'] == file['git_mode'], key
        assert row['line_count'] == file['line_count'], key
        manifest.append(dict(path=f'{key[0]}/{key[2]}', mode=row['git_mode'], sha256=row['sha256']))
    acquisition = dict(bundle['acquisition'])
    manifest.sort(key=lambda file: file['path'])
    assert hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest() == acquisition['manifest_sha256']
    acquisitions = rows('SELECT * FROM catalog.acquisition', database)
    assert len(acquisitions) == 1, 'Repeat import duplicated acquisition records'
    stored_acquisition = acquisitions[0]
    assert datetime.fromisoformat(stored_acquisition.pop('acquired_at')) == datetime.fromisoformat(acquisition.pop('acquired_at'))
    assert stored_acquisition == acquisition, 'Acquisition provenance differs'
    links = rows('SELECT acquisition_id, skill_slug, version_id FROM catalog.skill_acquisition', database)
    assert {(row['acquisition_id'], row['skill_slug'], row['version_id']) for row in links} == {
        (acquisition['acquisition_id'], skill['slug'], skill['version_id']) for skill in bundle['skills']}
    assert len(links) == len(bundle['skills']), 'Repeat import duplicated skill acquisition links'


def check_binary(bundle):
    skill = bundle['skills'][0]
    raw = bytes(range(256)) + b'\x00\xff\r\n\x00binary tail'
    digest = hashlib.sha256(raw).hexdigest()
    line_count = raw.count(b'\n') + 1
    result = psql(f"""
BEGIN;
INSERT INTO catalog.source_file
  (skill_slug, version_id, path, content, sha256, line_count, git_mode)
VALUES ('{skill['slug']}', '{skill['version_id']}', 'test-binary-roundtrip.bin',
        decode('{raw.hex()}', 'hex'), '{digest}', {line_count}, '100755');
SELECT json_build_object('content', encode(content, 'hex'), 'sha256', sha256,
                         'git_mode', git_mode, 'line_count', line_count)
FROM catalog.source_file WHERE path = 'test-binary-roundtrip.bin';
ROLLBACK;
""")
    stored = json.loads(result)
    assert bytes.fromhex(stored['content']) == raw
    assert stored['sha256'] == digest
    assert stored['git_mode'] == '100755'
    assert stored['line_count'] == raw.count(b'\n') + 1
    assert psql("SELECT count(*) FROM catalog.source_file WHERE path = 'test-binary-roundtrip.bin';").strip() == '0'


def check_upgrade(bundle):
    database = 'context_sql_upgrade_' + uuid.uuid4().hex
    subprocess.run(['createdb', database], check=True)
    try:
        psql((ROOT / 'sql/001_schema.sql').read_text(), database)
        old_bytes = b'Historical source before acquisition tracking.\n'
        psql(f"""
INSERT INTO catalog.skill (slug, kind) VALUES ('historical-skill', 'generic');
INSERT INTO catalog.skill_version
  (skill_slug, version_id, repository_url, git_commit, source_directory, routing_description, importer_version)
VALUES ('historical-skill', '{'a' * 64}', 'https://github.com/getcolors/skills',
        '{'b' * 40}', 'historical-skill', 'Historical test fixture', '1');
INSERT INTO catalog.source_file (skill_slug, version_id, path, content, sha256, line_count)
VALUES ('historical-skill', '{'a' * 64}', 'SKILL.md', decode('{old_bytes.hex()}', 'hex'),
        '{hashlib.sha256(old_bytes).hexdigest()}', 1);
UPDATE catalog.skill SET current_version = '{'a' * 64}' WHERE slug = 'historical-skill';
""", database)
        # Privilege groups already exist in this test cluster from migration 002.
        psql((ROOT / 'sql/003_acquisition.sql').read_text(), database)
        seed = (ROOT / 'data/skills.sql').read_text()
        psql(seed, database)
        psql(seed, database)
        check_snapshot(bundle, database, historical=True)
        old = rows("SELECT encode(content, 'hex') AS content, git_mode, sha256, line_count "
                   "FROM catalog.source_file WHERE skill_slug = 'historical-skill'", database)
        assert old == [dict(content=old_bytes.hex(), git_mode=None,
                            sha256=hashlib.sha256(old_bytes).hexdigest(), line_count=1)]
        assert psql("SELECT current_version FROM catalog.skill WHERE slug = 'historical-skill';", database).strip() == 'a' * 64
    finally:
        psql(f'DROP DATABASE {database};')


def main():
    bundle = json.loads((ROOT / 'data/skills.json').read_text())
    check_snapshot(bundle)
    check_binary(bundle)
    check_upgrade(bundle)
    print('All source bytes, acquisition provenance, binary storage, and migration history checks passed.')


if __name__ == '__main__':
    main()
