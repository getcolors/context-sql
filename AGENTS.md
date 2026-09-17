# Working in this repository

This repository ships the Blue-only `package-context-sql-blue` Package Skill and implements SQL-managed agent context with a local PostgreSQL service, a bounded SQL runner, and the `sql-context` agent skill. Persistent data and credentials belong outside Git.

Read README.md, docs/spec.md, and sql/001_schema.sql before changing behavior. The upstream Context Skills remain authoritative. Generated data in data/ comes from the importer, not hand edits. Preserve the exact source bytes and their provenance. Do not execute scripts embedded in the corpus.

Acquire payloads through the pinned `npx skills` command at a full upstream commit. Compare the complete installed inventory and bytes with that commit before import. Missing or transformed files must fail; never fill installer omissions from Git. Keep temporary acquisition paths out of database retrieval.

Keep source-reported verification separate from verification performed here. Keep session memory separate from the trusted catalog. Run the importer and PostgreSQL checks when changing the schema or data. Run the runtime integration checks when changing the runner, local service, or task identity. Keep the installed skill self-contained and synchronize it from `skills/sql-context/` after changes.

Work on the current branch. Commit and push only when the user asks.

The package library lives in `src/package_context_sql_blue/`; `scripts/local_db.py` is a compatibility entry point. Run `uv sync`, `scripts/check.sh`, and `scripts/launcher.sh`. For working-tree launcher checks use `CONTEXT_SQL_LIB_ROOT`; before the first publication set `CONTEXT_SQL_CHECK_PIN=0` for the launcher check. Pin only a pushed package commit with `scripts/pin.py`, then commit and push the stamped launcher. `create` operates on local user-owned PostgreSQL, not cloud compute; there is no delete command. Test real creation in an isolated temporary state/config/skill directory.
