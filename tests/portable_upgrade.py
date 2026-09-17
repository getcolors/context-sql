#!/usr/bin/env python3
"""Verify migration 005 against real historical rows in a disposable database."""
import json
from pathlib import Path
import subprocess
import uuid


ROOT = Path(__file__).resolve().parents[1]


def psql(statement, database, check=True):
    return subprocess.run(
        ['psql', '-X', '-qAt', '-v', 'ON_ERROR_STOP=1', '-d', database],
        input=statement, text=True, capture_output=True, check=check)


def rows(statement, database):
    return [json.loads(line) for line in psql(
        f'SELECT row_to_json(r) FROM ({statement}) r;', database).stdout.splitlines()]


def main():
    suffix = uuid.uuid4().hex
    database = 'portable_upgrade_' + suffix
    role = 'portable_owner_' + suffix
    subprocess.run(['createdb', database], check=True)
    role_created = False
    try:
        for filename in ('001_schema.sql', '003_acquisition.sql', '004_context_runtime.sql'):
            psql((ROOT / 'sql' / filename).read_text(), database)
        psql((ROOT / 'data/skills.sql').read_text(), database)
        run_ids = [str(uuid.uuid4()) for _ in range(3)]
        # Different owners exercise the FORCE RLS backfill. The last fixture
        # predates migration 004 and deliberately has no path or task key.
        for ordinal, run_id in enumerate(run_ids):
            path = f'/old/checkouts/project-{ordinal}'
            identity = (f"'{path}', 'investigate'" if ordinal < 2 else 'NULL, NULL')
            psql(f"""
INSERT INTO working.run (run_id, owner_name, task, project_key, task_key)
VALUES ('{run_id}', 'historical_owner_{ordinal}', 'Preserve original task', {identity});
INSERT INTO working.item
    (run_id, kind, body, source_skill, source_version, source_path, source_section)
SELECT '{run_id}', 'decision', 'Original note with Unicode: café',
       skill_slug, version_id, path, ordinal
FROM catalog.section ORDER BY skill_slug, version_id, path, ordinal LIMIT 1;
""", database)
        run_columns = 'run_id,owner_name,task,created_at,expires_at,project_key,task_key'
        before_runs = rows(f'SELECT {run_columns} FROM working.run ORDER BY run_id', database)
        before_notes = rows('SELECT * FROM working.item ORDER BY run_id,item_id', database)
        migration = (ROOT / 'sql/005_portable_projects.sql').read_text()

        # A table owner without BYPASSRLS must fail, not silently backfill only
        # its own rows. All migration DDL must roll back with that failure.
        psql(f'CREATE ROLE {role} NOLOGIN NOSUPERUSER NOBYPASSRLS;', database)
        role_created = True
        psql(f'GRANT USAGE,CREATE ON SCHEMA working TO {role}; '
             f'ALTER TABLE working.run OWNER TO {role};', database)
        denied = psql(f'SET ROLE {role};\n' + migration, database, check=False)
        assert denied.returncode != 0, 'Migration silently accepted a role restricted by RLS'
        assert 'row-level security' in denied.stderr, denied.stderr
        assert not rows("SELECT column_name FROM information_schema.columns WHERE "
                        "table_schema='working' AND table_name='run' AND column_name='project_id'",
                        database), 'Failed migration left partial DDL'

        psql(migration, database)
        after_runs = rows(f'SELECT {run_columns} FROM working.run ORDER BY run_id', database)
        assert after_runs == before_runs, 'Migration changed historical identity or timestamps'
        assert rows('SELECT * FROM working.item ORDER BY run_id,item_id', database) == before_notes, \
            'Migration changed notes or source references'
        migrated = rows('SELECT run_id,project_id,project_key,local_path FROM working.run', database)
        assert len(migrated) == 3
        for row in migrated:
            assert row['project_id'] is None, 'Migration guessed a portable project ID'
            assert row['local_path'] == row['project_key'], 'Migration lost the historical path'
        print('Portable project upgrade preserves all owners, historical UUIDs, paths, notes, and citations.')
    finally:
        subprocess.run(['dropdb', database], check=True)
        if role_created:
            subprocess.run(['psql', '-X', '-q', '-v', 'ON_ERROR_STOP=1',
                            '-c', f'DROP ROLE {role};'], check=True)


if __name__ == '__main__':
    main()
