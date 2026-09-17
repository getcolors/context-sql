"""Atomic administrative catalog ingestion into an already initialized local database."""
import base64
import hashlib
import json
import os
from pathlib import Path

import psycopg
from psycopg import sql
from psycopg.types.json import Jsonb

from .local_db import LocalDB, PORT


def canonical_json(document):
    return json.dumps(document, sort_keys=True, separators=(',', ':'), ensure_ascii=False)


def connection(state_dir: Path, config: Path):
    db = LocalDB(state_dir, config)
    db.load()
    # libpq otherwise honors PGHOSTADDR even with an explicit Unix socket host.
    for name in list(os.environ):
        if name.startswith('PG'):
            del os.environ[name]
    return psycopg.connect(host=str(db.socket), port=PORT, dbname='context_sql',
                           user='context_admin', password=db.credentials['admin'],
                           connect_timeout=5, options='-c search_path=pg_catalog -c statement_timeout=60000')


def insert_exact(cursor, table, row):
    """Refuse a collision whose immutable values differ, including derived values."""
    columns = list(row)
    values = [Jsonb(v) if isinstance(v, dict) else v for v in row.values()]
    for index, column in enumerate(columns):
        if column in ('command', 'cells', 'assertions', 'raw_case', 'document'):
            values[index] = Jsonb(row[column])
    identifier = sql.Identifier('catalog', table)
    cursor.execute(sql.SQL('INSERT INTO {} ({}) VALUES ({}) ON CONFLICT DO NOTHING').format(
        identifier, sql.SQL(',').join(map(sql.Identifier, columns)),
        sql.SQL(',').join(sql.Placeholder() for _ in columns)), values)
    clauses = sql.SQL(' AND ').join(sql.SQL('{} IS NOT DISTINCT FROM {}').format(
        sql.Identifier(column), sql.Placeholder()) for column in columns)
    cursor.execute(sql.SQL('SELECT 1 FROM {} WHERE {}').format(identifier, clauses), values)
    if cursor.fetchone() is None:
        raise ValueError(f'Immutable catalog collision in {table}')


def skill_rows(bundle, skill):
    key = dict(skill_slug=skill['slug'], version_id=skill['version_id'])
    yield 'skill_version', dict(**key, repository_url=bundle['repository_url'],
        git_commit=bundle['git_commit'], source_directory=skill.get('source_directory', skill['slug']),
        routing_description=skill['routing_description'], importer_version=bundle['importer_version'])
    for file in skill['files']:
        fkey = dict(**key, path=file['path'])
        yield 'source_file', dict(**fkey, content=base64.b64decode(file['content_base64'], validate=True),
            sha256=file['sha256'], line_count=file['line_count'], git_mode=file['git_mode'])
        for section in file['sections']:
            yield 'section', dict(**fkey, **section)
        for pin in file['pins']:
            yield 'pin_row', dict(**fkey, **pin)
        for ordinal, case in enumerate(file['evals']):
            yield 'eval_case', dict(**fkey, ordinal=ordinal,
                source_id=str(case['id']) if 'id' in case else None,
                name=case.get('name'), prompt=case['prompt'],
                expected_output=case.get('expected_output', case.get('expected_behaviour', case.get('expected_behavior'))),
                assertions=case.get('assertions', []), raw_case=case)


def commit_import(bundles, resolutions, lock_document, state_dir: Path, config: Path):
    if not bundles or len(bundles) != len(resolutions):
        raise ValueError('Each bundle requires a source resolution')
    digest = hashlib.sha256(canonical_json(lock_document).encode()).hexdigest()
    seen = set()
    with connection(state_dir, config) as conn, conn.cursor() as cursor:
        cursor.execute('SELECT pg_advisory_xact_lock(748193560219)')
        cursor.execute("SELECT to_regclass('catalog.ingestion_lock')")
        if cursor.fetchone()[0] is None:
            raise ValueError('Database requires migration 006; initialize with the current package first')
        for bundle, resolution in zip(bundles, resolutions):
            if (bundle['acquisition']['repository_url'] != bundle['repository_url'] or
                    bundle['acquisition']['git_commit'] != bundle['git_commit']):
                raise ValueError('Acquisition source differs from bundle source')
            for skill in bundle['skills']:
                slug = skill['slug']
                if slug in seen:
                    raise ValueError(f'Duplicate selected skill: {slug}')
                seen.add(slug)
                cursor.execute('SELECT kind FROM catalog.skill WHERE slug=%s', (slug,))
                existing = cursor.fetchone()
                if existing and existing[0] != skill['kind']:
                    raise ValueError(f'Skill kind collision: {slug}')
                cursor.execute('SELECT DISTINCT repository_url FROM catalog.skill_version WHERE skill_slug=%s', (slug,))
                if any(row[0] != bundle['repository_url'] for row in cursor.fetchall()):
                    raise ValueError(f'Skill repository collision: {slug}')
            insert_exact(cursor, 'acquisition', bundle['acquisition'])
            insert_exact(cursor, 'source_resolution', dict(
                acquisition_id=bundle['acquisition']['acquisition_id'],
                requested_source=resolution['requested_source'], requested_ref=resolution['requested_ref'],
                resolved_at=resolution['resolved_at']))
            for skill in bundle['skills']:
                slug, version = skill['slug'], skill['version_id']
                cursor.execute('INSERT INTO catalog.skill(slug,kind) VALUES (%s,%s) ON CONFLICT DO NOTHING',
                               (slug, skill['kind']))
                counts = {name: 0 for name in ('skill_version', 'source_file', 'section', 'pin_row', 'eval_case')}
                for table, row in skill_rows(bundle, skill):
                    insert_exact(cursor, table, row)
                    counts[table] += 1
                for table, count in counts.items():
                    cursor.execute(sql.SQL('SELECT count(*) FROM {} WHERE skill_slug=%s AND version_id=%s').format(
                        sql.Identifier('catalog', table)), (slug, version))
                    if cursor.fetchone()[0] != count:
                        raise ValueError(f'Incomplete or duplicate version payload: {slug} ({table})')
                insert_exact(cursor, 'skill_acquisition', dict(acquisition_id=bundle['acquisition']['acquisition_id'],
                                                              skill_slug=slug, version_id=version))
                cursor.execute('UPDATE catalog.skill SET current_version=%s WHERE slug=%s', (version, slug))
        insert_exact(cursor, 'ingestion_lock', dict(sha256=digest, document=lock_document))
    return digest


def export_lock(state_dir: Path, config: Path, lock_hash=None):
    with connection(state_dir, config) as conn, conn.cursor() as cursor:
        conn.read_only = True
        cursor.execute('SELECT document FROM catalog.ingestion_lock WHERE (%s::text IS NULL OR sha256=%s) '
                       'ORDER BY created_at DESC,sha256 LIMIT 1', (lock_hash, lock_hash))
        row = cursor.fetchone()
        if row is None:
            raise ValueError('No matching imported lock in this database')
        return row[0]
