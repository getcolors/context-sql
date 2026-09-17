# Validation record

Run date: 2026-09-17. Database: PostgreSQL 18.6. Source snapshot: `getcolors/skills@3c82f5c9fc1400f748988e8295ab3af7cf5994d5`. Acquisition CLI: `skills@1.6.0`.

## Checks that passed

- The importer downloaded all fourteen selected skills through the pinned CLI in a temporary project. An independent fetch of the same upstream commit confirmed all 80 file paths and exact bytes. The regenerated snapshot preserves the previous snapshot's source bytes.
- A second remote acquisition with `--check` verified the recorded payload, acquisition recipe, manifest, and SQL. It left the original acquisition timestamp and identifier unchanged.
- All 35 Python tests passed. They cover payload completeness, changed bytes, installer omissions, extra files, binary assets, unsafe paths, symlinks, Gitlinks, pinned commands, staging cleanup, revision identity, acquisition provenance, and deterministic artifact checks.
- Parser regressions cover delimiter text inside descriptions, closing fences with trailing text, tables inside code fences, and Unicode separators that must not shift source line citations.
- `scripts/check.sh` created a disposable PostgreSQL cluster, applied migrations 001 through 003, and loaded the seed twice. Integrity assertions, immutable acquisition records, foreign keys, and SQL examples passed.
- Database retrieval reproduced every file's bytes, hash, Git mode, and line count. Acquisition fields, timestamps, skill links, and the reconstructed manifest digest matched the snapshot. A binary fixture preserved all byte values, NUL bytes, and line endings.
- A separate disposable database exercised migration 003 with historical rows already present. Loading the new seed twice preserved the old bytes and left their unrecorded Git modes and acquisition links absent.
- Restricted login connections could read acquisition provenance and could not change it. Catalog write denial, role escalation denial, and working-memory isolation checks passed.
- Subagents reviewed the CLI behavior, documentation, acquisition code, and test coverage. `git diff --check` passed.

The database checks use the generated snapshot after the importer has removed its staging directory. They do not read a source checkout or installed skill files. Acquisition tests use local fixtures; the two remote importer runs separately exercised GitHub and npm access.

## Earlier page checks

On 2026-09-16, Chromium rendered the page at desktop and mobile widths without JavaScript errors or horizontal overflow. Query tabs, filtering, empty results, and expandable descriptions worked. The four displayed SQL examples ran against PostgreSQL. A temporary public tunnel returned the reviewed page bytes. Those browser and tunnel checks were not repeated for this change, which updates the page's explanatory text.

## Limits

The 110 eval cases are imported fixtures, not successful model runs. No comparison against file search or embeddings was performed. The original infrastructure builds were not repeated. Database tests used a local Unix socket and trust authentication. They did not test network authentication or a production connection pool.

There is no deployed query gateway. Output limits, query parsing, cancellation, query auditing, and production authentication still require implementation and testing before exposing a SQL tool. The static page exposes no database connection.
