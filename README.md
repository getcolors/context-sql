# SQL-managed agent context

Use PostgreSQL as a versioned catalog of Context Skills and as a separate store for an agent's working notes. The agent queries the catalog, selects relevant evidence, and cites the original source revision and lines. Its notes cannot overwrite published skill knowledge.

The supplied essays describe this approach but do not define a formal protocol. This repository proposes a [specification](docs/spec.md), explains the [research and tradeoffs](docs/research.md), and supplies a local PostgreSQL service, an agent skill, and a reproducible import. Open [index.html](index.html) for the detailed single-page explanation.

## What is included

| Artifact | Purpose |
| --- | --- |
| [sql/001_schema.sql](sql/001_schema.sql) | Source versions, exact file bytes, searchable sections, pin rows, eval fixtures, and separate working memory |
| [sql/002_roles.sql](sql/002_roles.sql) | Catalog reader and working-memory writer privilege groups |
| [sql/003_acquisition.sql](sql/003_acquisition.sql) | Immutable acquisition provenance linked to skill versions |
| [sql/004_context_runtime.sql](sql/004_context_runtime.sql) | Original project-path and task keys for working memory |
| [sql/005_portable_projects.sql](sql/005_portable_projects.sql) | Portable project IDs, separate local paths, and preserved legacy task identities |
| [skills/sql-context/SKILL.md](skills/sql-context/SKILL.md) | Agent workflow and its bounded SQL runner |
| [skills/package-context-sql-blue/SKILL.md](skills/package-context-sql-blue/SKILL.md) | Package Skill to create and initialize PostgreSQL on a new local machine |
| [scripts/local_db.py](scripts/local_db.py) | Initialize, start, stop, inspect, and back up local PostgreSQL |
| [sql/queries.sql](sql/queries.sql) | Routing, full-text and literal search, pins, evals, bounded note selection, and expiry cleanup |
| [data/skills.json](data/skills.json) | Portable snapshot with base64 source bytes and derived records |
| [data/skills.sql](data/skills.sql) | Transactional, repeatable seed for PostgreSQL |
| [scripts/import_skills.py](scripts/import_skills.py) | Pinned `npx skills` acquisition and byte verification against the upstream commit |
| [docs/spec.md](docs/spec.md) | Proposed SQL context contract and implementation coverage |
| [docs/input-sources.json](docs/input-sources.json) | Exact essay URLs and content digests |

The snapshot contains **14 skills, 80 files, 755 sections, 179 pin rows, and 110 eval cases**, from `getcolors/skills` commit `3c82f5c9fc1400f748988e8295ab3af7cf5994d5`. Thirteen skills are Context Skills. `refresh-oci-token` is a generic skill. The importer stores its script as source material and never executes it.

The original files remain authoritative in `getcolors/skills`. This repository's data is a generated database snapshot, not another maintained implementation of their companion packages. Verification claims are source-reported. Importing the files does not repeat the builds described by those claims.

## Create on a new machine with the Package Skill

The Blue Package Skill creates local PostgreSQL, loads the verified catalog, and installs the `sql-context` agent skill. Run it as your normal user with PostgreSQL 16 or newer binaries, Python 3.11 or newer, `uv`, and Git on `PATH`. The Package Skill includes an optional [devenv.nix](devenv.nix) toolchain. Skill acquisition also needs Node.js and `npx`.

Create a deployment directory, then install from a reviewed full commit of this repository. Replace `<published-commit>` with that commit's 40-character SHA:

```sh
mkdir context-sql-local
cd context-sql-local
npx --yes skills@1.6.0 add \
  'https://github.com/getcolors/context-sql/tree/<published-commit>' \
  --skill package-context-sql-blue --agent codex --copy --yes
cp .agents/skills/package-context-sql-blue/blue blue
```

If the host uses Nix, devenv, and direnv, copy the supplied toolchain files to install PostgreSQL and the other runtime tools into the development environment:

```sh
cp .agents/skills/package-context-sql-blue/devenv.nix devenv.nix
cp .agents/skills/package-context-sql-blue/.envrc .envrc
direnv allow
```

Create `colors.yml` with these non-secret settings:

```yaml
profile: context-sql-local
context-sql-state-dir: ~/.local/share/context-sql
context-sql-config: ~/.config/context-sql/connections.json
context-sql-skill-dir: ~/.codex/skills/sql-context
context-sql-install-skill: true
context-sql-service: false
```

```sh
./blue build
./blue create --dry-run
./blue create
./blue status
```

`build` writes the plan under `.colors/<profile>/`. `create --dry-run` skips side effects. `create` initializes the cluster, applies migrations, imports the supplied catalog, and installs the agent skill. Existing instances keep their notes and credentials when you repeat `create`. The database and connection settings stay outside the checkout. PostgreSQL listens only on its private Unix socket.

Set `context-sql-service: true` to enable a systemd user service on Linux. The default starts the instance without installing a service. Automatic startup before login also requires user lingering. Use `./blue stop`, `./blue start`, and `./blue backup` for local lifecycle operations. There is no `delete` or automated restore command.

A new machine gets the catalog with empty task memory. Portable project IDs do not transfer notes between databases. To preserve task history, take a backup on the old machine and restore it separately.

Open a new Codex session after installation and invoke `$sql-context`. The [Package Skill](skills/package-context-sql-blue/SKILL.md) manages the local database; the [agent skill](skills/sql-context/SKILL.md) queries its catalog and maintains task notes. The installed agent skill records the configured connection-file path, including custom paths. `CONTEXT_SQL_CONFIG` overrides it when needed. When updating the Package Skill, copy its `blue` launcher to the deployment root again after installation.

The repository's root [colors.yml](colors.yml) supplies the same defaults for running `./blue` from a source checkout. Use `CONTEXT_SQL_LIB_ROOT="$PWD" ./blue ...` to test uncommitted package changes. After pushing a package commit, `python3 scripts/pin.py` stamps the launcher with its published SHA. Commit and push the updated launcher separately.

## Use locally from a checkout

Requirements are PostgreSQL 16 or newer, Python 3.11 or newer, and `uv` on a Unix host. Automatic service startup uses systemd. From this checkout:

```sh
python3 scripts/local_db.py init
python3 scripts/local_db.py status
./context catalog
./context start --project getcolors/redis --local-path "$PWD" --task investigate-redis \
  --description 'Investigate Redis authentication failures'
./context search --query 'NOAUTH' --skill redis-single-node
```

`init` creates a dedicated cluster outside Git, applies the migrations, loads the verified seed, and creates separate catalog-reader and memory-writer logins. PostgreSQL listens on a private Unix socket with SCRAM authentication and no TCP listener. Connection settings live in `~/.config/context-sql/connections.json` with mode `0600`. Do not print or commit this file.

Use these commands to manage the instance:

```sh
python3 scripts/local_db.py start
python3 scripts/local_db.py stop
python3 scripts/local_db.py backup
python3 scripts/local_db.py enable
```

`enable` installs and starts a systemd user service. Starting at boot without a login also requires user lingering. The database and backups persist in `~/.local/share/context-sql/` by default. Use `backup --output /absolute/path/context-sql.dump` to choose a new backup file. Use `status` to inspect the paths and server state. After refreshing `data/skills.sql`, run `python3 scripts/local_db.py import` to load that seed into the local instance.

Install the bootstrap skill and its helper for Codex:

```sh
mkdir -p "${CODEX_HOME:-$HOME/.codex}/skills"
cp -R skills/sql-context "${CODEX_HOME:-$HOME/.codex}/skills/"
```

The skill uses `uv run --script <skill-directory>/scripts/context.py`; `./context` invokes the same helper from this checkout. Its script pins `psycopg[binary]` to `3.2.10`. Once dependencies are cached, catalog retrieval and note restoration need only the running database and this helper. They do not read import staging or a skills source clone.

Use a stable project ID such as `getcolors/redis` and one task key per piece of work. `start` returns the existing run UUID for that project and task under the same database login, even when the checkout moves. Project IDs contain at least two slash-separated components, each matching `[a-z0-9][a-z0-9._-]*`, and at most 255 characters overall. Use `--local-path "$PWD"` to record the current checkout as separate metadata. The helper resolves this path to an absolute path and keeps the last explicitly supplied value. Omitting it preserves the previous value. A project ID does not synchronize separate databases.

Use `restore --run UUID` before continuing, and `runs --project getcolors/redis` to find earlier tasks. Record decisions, open questions, and selected immutable source references with `note`. Mark completed or replaced notes with `state`. The explicit `expire` command deletes expired runs and their notes across all projects owned by the writer login. See the [skill](skills/sql-context/SKILL.md) for examples.

After upgrading an existing local instance, run `python3 scripts/local_db.py init` to apply new migrations. Earlier tasks keep their UUIDs and notes. List tasks that still use legacy identities with `./context runs --legacy`, then assign a portable project ID explicitly:

```sh
./context adopt --run UUID --project getcolors/redis --local-path "$PWD"
```

`adopt` accepts an unexpired legacy run with a task key and preserves its UUID, notes, and legacy project key. It refuses a project/task combination already assigned to another run. The migration does not infer repository identity from a directory name.

The runner executes fixed parameterized SQL operations. It does not accept arbitrary SQL. Each response contains at most 50 rows and 65,536 serialized JSON bytes, including the envelope. `--max-bytes` can lower the byte cap to 1,024. Text and file slices accept `--length` up to 8,192; note bodies accept at most 16,384 UTF-8 bytes. The helper reports truncation and continuation offsets. It sets a five-second SQL timeout, a two-second lock timeout, and an eight-second external deadline.

Catalog reads use the reader login. Notes and task restoration use the writer login under row security. Tasks under the same login share access. These role controls do not isolate the Unix account that owns the database and can read its administrative credentials. Notes are external memory; the skill cannot enlarge the model's context window or edit its conversation history.

## Load into another PostgreSQL instance

Requirements are PostgreSQL 16 or newer and `psql`. The integration checks ran on PostgreSQL 18.6. These commands target a new, dedicated database using migration-owner credentials. Role creation requires a role administrator. Choose connection settings through the usual `PGHOST`, `PGPORT`, and `PGUSER` environment variables without committing credentials.

```sh
createdb context_sql
psql -X -v ON_ERROR_STOP=1 -d context_sql -f sql/001_schema.sql
psql -X -v ON_ERROR_STOP=1 -d context_sql -f sql/002_roles.sql
psql -X -v ON_ERROR_STOP=1 -d context_sql -f sql/003_acquisition.sql
psql -X -v ON_ERROR_STOP=1 -d context_sql -f sql/004_context_runtime.sql
psql -X -v ON_ERROR_STOP=1 -d context_sql -f sql/005_portable_projects.sql
psql -X -v ON_ERROR_STOP=1 -d context_sql -f data/skills.sql
psql -X -v ON_ERROR_STOP=1 -d context_sql \
  -v symptom='NOAUTH' \
  -v run_id='11111111-1111-1111-1111-111111111111' \
  -f sql/queries.sql
```

Run the migrations once in order. Migration 005 requires an owner with `BYPASSRLS` or superuser access to backfill all historical owners; it fails rather than skip hidden rows. An existing database with migrations 001 and 002 needs migration 003 before loading the seed and migrations 004 and 005 before using the task runner. The seed may run again without duplicating rows. It explicitly selects the imported versions as current, so loading an older seed intentionally changes the current pointers. Historical version rows remain intact.

Create application login roles separately. A retrieval login should inherit only `context_reader`. A separately authorized note-writing login can inherit `context_writer`. Neither should own database objects or have role administration, superuser, or `BYPASSRLS` privileges. Working memory policies use the authenticated `session_user`; a shared pool login does not separate application users. The catalog is shared by one trusted workspace.

The examples include a note-cleanup `DELETE` intended for a writer or operator. They demonstrate database operations, not an unrestricted SQL endpoint for an agent.

## Reproduce the import

Rebuilding requires Python 3, PyYAML, Git, Node.js, `npx`, and network access to GitHub and npm. No local skills checkout is needed. The importer requires a full upstream commit SHA and pins the acquisition CLI to `skills@1.6.0`. Loading the supplied SQL snapshot requires none of these acquisition tools.

```sh
uv venv .venv
uv pip install --python .venv/bin/python -r requirements.txt
.venv/bin/python scripts/import_skills.py --source https://github.com/getcolors/skills \
  --revision 3c82f5c9fc1400f748988e8295ab3af7cf5994d5 --output data --check
```

Omit `--check` to regenerate. The importer invokes `npx --yes skills@1.6.0 add` with the commit-qualified source URL, all fourteen skill names, and `--agent codex --copy --yes` in an isolated temporary project. It independently fetches that Git commit to verify the complete inventory and exact bytes. Missing, transformed, extra, or unsafe files fail before output is replaced. Git verification never supplies an omitted file.

PostgreSQL stores every selected payload file verbatim, including scripts and binary assets, with relative paths and SHA-256 hashes. Acquisition provenance records the source, commit, CLI version, arguments, selected skills, acquisition time, and verified inventory. Staging is removed after acquisition; retrieval needs only the database. Scripts in the corpus are never executed.

A fresh acquisition records the UTC time when download verification completes, while identical content retains the same skill versions. `--check` downloads and verifies the payload again, compares content and deterministic SQL, and ignores only the new acquisition timestamp and identifier. It does not rewrite the recorded acquisition history.

A skill's version digest includes the source commit, file paths, Git modes, file digests, and parser version. Pin extraction preserves literal table cells and heading scope; it does not infer version ranges from prose. Eval JSON remains intact even when optional field names vary.

## Run the checks

With PostgreSQL binaries and Python dependencies on `PATH`:

```sh
uv sync
./scripts/check.sh
./scripts/launcher.sh
```

The script creates and removes its own Unix-socket-only PostgreSQL clusters. Run it as a non-root user with `uv` available. It does not connect to the persistent service. It verifies source reconstruction, section coverage, deterministic SQL generation, schema loading, repeat import, foreign keys, immutable revisions, retrieval examples, denied catalog writes, and working-memory isolation. It compares every stored file and acquisition record with the snapshot, checks binary storage, and tests migration from a database with historical imports. Runner tests exercise paging, output limits, deadlines, rollback, and task restoration. Service tests also check SCRAM authentication, lifecycle commands, and backup restoration. Legacy role tests use trust authentication. Network transport remains untested. These checks use local fixtures; the importer command above separately verifies a real remote download.

See [docs/validation.md](docs/validation.md) for the recorded checks. The 110 imported eval cases are fixtures. No model diagnosis benchmark or production gateway security evaluation is claimed. [The specification's coverage table](docs/spec.md#10-implementation-coverage) identifies remaining work, including query audit, model-token accounting, and reviewed promotion of new knowledge.

## View the page

```sh
python3 -m http.server 8769 --bind 127.0.0.1
```

For a public temporary preview, copy `index.html` into a separate directory, serve that directory, and point `cloudflared tunnel --url http://127.0.0.1:8769` at it. The published preview for this task serves only the page. It exposes no SQL endpoint or database connection.
