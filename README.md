# SQL-managed agent context

Use PostgreSQL as a versioned catalog of Context Skills and as a separate store for an agent's working notes. The agent queries the catalog, selects relevant evidence, and cites the original source revision and lines. Its notes cannot overwrite published skill knowledge.

The supplied essays describe this approach but do not define a formal protocol. This repository proposes a [specification](docs/spec.md), explains the [research and tradeoffs](docs/research.md), and supplies a tested schema and reproducible import. Open [index.html](index.html) for the detailed single-page explanation.

## What is included

| Artifact | Purpose |
| --- | --- |
| [sql/001_schema.sql](sql/001_schema.sql) | Source versions, exact file bytes, searchable sections, pin rows, eval fixtures, and separate working memory |
| [sql/002_roles.sql](sql/002_roles.sql) | Catalog reader and working-memory writer privilege groups |
| [sql/queries.sql](sql/queries.sql) | Routing, full-text and literal search, pins, evals, bounded note selection, and expiry cleanup |
| [data/skills.json](data/skills.json) | Portable snapshot with base64 source bytes and derived records |
| [data/skills.sql](data/skills.sql) | Transactional, repeatable seed for PostgreSQL |
| [scripts/import_skills.py](scripts/import_skills.py) | Deterministic import from committed Git objects |
| [docs/spec.md](docs/spec.md) | Proposed SQL context contract and implementation coverage |
| [docs/input-sources.json](docs/input-sources.json) | Exact essay URLs and content digests |

The snapshot contains **14 skills, 80 files, 755 sections, 179 pin rows, and 110 eval cases**, from `getcolors/skills` commit `3c82f5c9fc1400f748988e8295ab3af7cf5994d5`. Thirteen skills are Context Skills. `refresh-oci-token` is a generic skill. The importer stores its script as source material and never executes it.

The original files remain authoritative in `getcolors/skills`. This repository's data is a generated database snapshot, not another maintained implementation of their companion packages. Verification claims are source-reported. Importing the files does not repeat the builds described by those claims.

## Load the database

Requirements are PostgreSQL 16 or newer and `psql`. The integration checks ran on PostgreSQL 18.6. These commands target a new, dedicated database using migration-owner credentials. Role creation requires a role administrator. Choose connection settings through the usual `PGHOST`, `PGPORT`, and `PGUSER` environment variables without committing credentials.

```sh
createdb context_sql
psql -X -v ON_ERROR_STOP=1 -d context_sql -f sql/001_schema.sql
psql -X -v ON_ERROR_STOP=1 -d context_sql -f sql/002_roles.sql
psql -X -v ON_ERROR_STOP=1 -d context_sql -f data/skills.sql
psql -X -v ON_ERROR_STOP=1 -d context_sql \
  -v symptom='NOAUTH' \
  -v run_id='11111111-1111-1111-1111-111111111111' \
  -f sql/queries.sql
```

Run the migrations once. The seed may run again without duplicating rows. It explicitly selects the imported versions as current, so loading an older seed intentionally changes the current pointers. Historical version rows remain intact.

Create application login roles separately. A retrieval login should inherit only `context_reader`. A separately authorized note-writing login can inherit `context_writer`. Neither should own database objects or have role administration, superuser, or `BYPASSRLS` privileges. Working memory policies use the authenticated `session_user`; a shared pool login does not separate application users. The catalog is shared by one trusted workspace.

The examples include a note-cleanup `DELETE` intended for a writer or operator. They demonstrate database operations, not an unrestricted SQL endpoint for an agent.

## Reproduce the import

Python 3 and PyYAML are needed only to rebuild the snapshot. Git must be available, and the requested commit must exist in the source clone.

```sh
uv venv .venv
uv pip install --python .venv/bin/python -r requirements.txt
.venv/bin/python scripts/import_skills.py --source ../skills \
  --revision 3c82f5c9fc1400f748988e8295ab3af7cf5994d5 --output data --check
```

Omit `--check` to regenerate. The importer reads committed Git objects, not local worktree edits. A skill's version digest includes the source commit, file digests, and parser version. Pin extraction preserves literal table cells and heading scope; it does not infer version ranges from prose. Eval JSON remains intact even when optional field names vary.

## Run the checks

With PostgreSQL binaries and Python dependencies on `PATH`:

```sh
PATH="$PWD/.venv/bin:$PATH" ./scripts/check.sh
```

The script creates and removes its own Unix-socket-only PostgreSQL cluster. Run it as a non-root user. It does not connect to an existing service. It verifies source reconstruction, section coverage, deterministic SQL generation, schema loading, repeat import, foreign keys, immutable revisions, retrieval examples, denied catalog writes, and working-memory isolation. Separate local login connections also test visibility and denied role escalation. The temporary cluster uses local trust authentication; this is not a password or network authentication test.

See [docs/validation.md](docs/validation.md) for the recorded checks. The 110 imported eval cases are fixtures. No model diagnosis benchmark or production gateway security evaluation is claimed. [The specification's coverage table](docs/spec.md#10-implementation-coverage) identifies remaining runtime work, including byte limits, cancellation, query audit, token accounting, and reviewed promotion of new knowledge.

## View the page

```sh
python3 -m http.server 8769 --bind 127.0.0.1
```

For a public temporary preview, copy `index.html` into a separate directory, serve that directory, and point `cloudflared tunnel --url http://127.0.0.1:8769` at it. The published preview for this task serves only the page. It exposes no SQL endpoint or database connection.
