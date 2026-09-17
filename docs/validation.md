# Validation record

Run date: 2026-09-17. Database: PostgreSQL 18.6. Source snapshot: `getcolors/skills@3c82f5c9fc1400f748988e8295ab3af7cf5994d5`. Acquisition CLI: `skills@1.6.0`.

## Checks that passed

- The importer downloaded all fourteen selected skills through the pinned CLI in a temporary project. An independent fetch of the same upstream commit confirmed all 80 file paths and exact bytes. The regenerated snapshot preserves the previous snapshot's source bytes.
- A remote acquisition with `--check` verified the recorded payload, acquisition recipe, manifest, and SQL after the runtime changes. It left the original acquisition timestamp and identifier unchanged.
- All 45 Python discovery tests and 13 runner integration tests passed. Import checks cover payload completeness, changed bytes, installer omissions, extra files, binary assets, unsafe paths, symlinks, Gitlinks, pinned commands, staging cleanup, revision identity, acquisition provenance, and deterministic artifacts.
- Parser regressions cover delimiter text inside descriptions, closing fences with trailing text, tables inside code fences, and Unicode separators that must not shift source line citations.
- `scripts/check.sh` created a disposable PostgreSQL cluster, applied migrations 001 through 005, and loaded the seed twice. Integrity assertions, immutable acquisition records, foreign keys, and SQL examples passed.
- Database retrieval reproduced every file's bytes, hash, Git mode, and line count. Acquisition fields, timestamps, skill links, and the reconstructed manifest digest matched the snapshot. A binary fixture preserved all byte values, NUL bytes, and line endings.
- A separate disposable database exercised migration 003 with historical rows already present. Loading the new seed twice preserved the old bytes and left their unrecorded Git modes and acquisition links absent.
- Restricted login connections could read acquisition provenance and could not change it. Catalog write denial, role escalation denial, and working-memory isolation checks passed.
- Runtime tests covered stable task identity, project routing, SQL injection strings as parameters, note states, historical citations after current-version changes, Unicode byte limits, pagination, complete file reconstruction, expiry, configuration permissions, and administrative-login rejection.
- An exclusive table lock caused the reader operation to time out without leaving an idle transaction. Releasing the lock restored successful search. An oversized write response rolled back task creation. A separate worker test confirmed external deadline termination.
- Service tests created a separate SCRAM-authenticated cluster, rejected a wrong password and catalog writes, preserved credentials across repeated initialization, detected a changed migration checksum, and restored all 80 source files from a private backup.
- The skill validator passed for both the tracked and installed `sql-context` skill. The installed helper matched its tracked source.
- Subagents implemented and reviewed the service, skill, runner tests, and documentation. `git diff --check` passed.

The database checks use the generated snapshot after the importer has removed its staging directory. They do not read a source checkout or installed corpus files. Acquisition tests use local fixtures; remote importer runs separately exercised GitHub and npm access.

## Persistent local instance and agent handoff

The local instance runs as the enabled `context-sql.service` user service. User lingering is enabled, so it can start at boot without an interactive login. Its data and private backups live in `~/.local/share/context-sql/`. It uses SCRAM over a private Unix socket and has no TCP listener. Reader and writer credentials live outside Git in a mode-0600 configuration file. The installed skill is at `~/.codex/skills/sql-context/`.

An independent agent used only the installed skill and SQL helper to investigate a Redis health-probe report. It saved a hypothesis and a next-action note, each linked to an immutable source section. The database was then stopped and started through the service commands.

A second agent received only the skill location, project path, and task key. It recovered run `bf135d93-4f33-4d24-84a3-f5d21c63ac40` and both notes. It resolved `redis-single-node` version `6e8961d46a7c8bda07defbf639967dc5b28c995d331fbbcd8509454de2583ed5`, `references/failure-catalogue.md` section 2 and `SKILL.md` section 6 through PostgreSQL. The agent preserved the distinction between a source-reported probe failure and an unverified application-authentication problem.

Neither agent read a source checkout or snapshot, inspected connection secrets, executed corpus scripts, or contacted Redis. A private backup was created after the handoff. This check demonstrates persistence and evidence recovery across database and agent restarts. It is not a diagnosis benchmark.

## Portable project identity

The runner resumes tasks by a portable project ID and task key under the same database login. Tests moved the checkout path, omitted optional path metadata, and used different project IDs with the same local path. Adoption tests preserved legacy UUIDs and cited notes, rejected conflicting identities and inaccessible or expired runs, and kept the original path key as provenance.

A separate disposable database applied migration 005 to historical runs owned by different logins. It preserved their original columns and complete notes, copied historical paths to the separate metadata field, and left portable IDs unassigned. A table owner without RLS bypass failed the migration and rolled back its schema changes instead of silently skipping other owners.

The persistent local instance was backed up before migration 005. Both notes from the earlier agent handoff and their citations restored identically after the migration and installed-skill update. The service remained active. The full database check script and a fresh pinned remote acquisition check passed after these changes.

An independent agent used the installed skill to save a checkpoint for `getcolors/context-sql` and task `portable-project-identity`. It resumed from a temporary working directory without supplying a local path. Run `8388dcb4-1e4c-4a6e-8b13-bde4e68e0226`, the recorded checkout path, and the complete saved note were unchanged.

## Earlier page checks

On 2026-09-16, Chromium rendered the page at desktop and mobile widths without JavaScript errors or horizontal overflow. Query tabs, filtering, empty results, and expandable descriptions worked. The four displayed SQL examples ran against PostgreSQL. A temporary public tunnel returned the reviewed page bytes. Those browser and tunnel checks were not repeated for this change, which updates the page's explanatory text.

## Limits

The 110 eval cases are imported fixtures, not successful model runs. No comparison against file search or embeddings was performed. The original infrastructure builds were not repeated. Legacy role tests use trust authentication in a disposable cluster; service tests also cover SCRAM authentication. Network transport and production connection pools were not tested.

The local helper accepts fixed parameterized operations. It has no arbitrary-SQL endpoint, query-audit store, per-task call budget, or model-token accounting. Database roles do not isolate the Unix account that owns the cluster. The static page exposes no database connection.
