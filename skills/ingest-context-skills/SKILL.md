---
name: ingest-context-skills
description: Import verified upstream Context Skill files into local PostgreSQL and share a lockfile to reproduce the catalog on another machine.
---

# Ingest context skills

Use the `context-skills` launcher beside this file. Resolve its location from this skill's directory or the supporting-files directory reported by `npx skills use`. It runs a pinned importer package through `uv`. The host needs Python 3.11 or newer, `uv`, Git, Node.js, and `npx`. Database writes require an existing local instance managed by `package-context-sql-blue`.

Run in the directory that should own the shared `context-skills.lock.json`. Invoke the launcher by its absolute path, or copy it there before using these examples:

```sh
./context-skills ingest getcolors/skills --skill redis-single-node
./context-skills ingest getcolors/skills --skill redis-single-node neon-single-node --revision main
./context-skills ingest getcolors/skills --all --dry-run
```

Sources may be `owner/repo`, a GitHub HTTPS repository URL, or a commit-, branch-, or tag-qualified GitHub `/tree/` URL. With no revision, ingestion resolves the default branch once. `--revision` selects a branch, tag, or full commit; a conflicting URL reference fails. Select named skills with `--skill` or explicitly select all with `--all`. Selection does not certify that the source follows a Context Skill standard.

The importer resolves the actual `skills` CLI version, invokes that exact version against the resolved full commit, and independently compares every selected path and byte with Git. `--skills-cli-version` chooses an exact acquisition CLI version. Missing, transformed, extra, or unsafe files fail verification. Never repair installer omissions by copying files from Git, and never execute scripts in imported payloads.

Ingesting another selection from the same repository merges it with the skills already selected in the lockfile. The importer acquires all selected skills together at the newly resolved revision. An existing `--all` selection remains an all-skills selection.

When the lockfile includes other repositories, ingestion reproduces their recorded selections at their locked commits too. This ensures a fresh database receives the complete locked catalog. A failure in any repository aborts the import.

Successful ingestion stores complete files, searchable projections, and acquisition provenance in PostgreSQL, then publishes the lockfile. Prior versions and task notes remain available. Temporary downloads are removed and are not needed for retrieval. `--dry-run` acquires and verifies the payload and previews the result without changing the database or lockfile.

Share the lockfile through the project's usual version control. On another initialized machine:

```sh
./context-skills sync --locked
```

Locked sync uses the recorded commits, selected skills, parser version, and content hashes. It does not resolve newer source references. A different acquisition CLI version is allowed only if the resulting payload verifies. The same catalog content and version IDs are reproduced; acquisition timestamps and local provenance belong to each machine. The lockfile contains no credentials, task notes, or local paths. It does not preserve upstream files if their repository becomes unavailable.

Refresh requested branches or tags deliberately:

```sh
./context-skills update
```

Review the resulting lockfile diff. Update reevaluates an original `--all` selection; locked sync keeps its recorded selection. A skill name already owned by another source fails instead of replacing that source. Use `--lockfile PATH` for a different lockfile location.

The helper uses the managed instance's administrative connection for ingestion. Defaults are `~/.local/share/context-sql` for state and `~/.config/context-sql/connections.json` for application connection settings; use `--state-dir` and `--config` for a custom instance. Do not print credentials or give the normal `sql-context` reader catalog-write privileges.

Database commit and filesystem publication cannot be atomic together. If lockfile publication fails after the database commit, use the reported digest to recover the committed lock snapshot:

```sh
./context-skills export-lock --digest DIGEST --lockfile context-skills.lock.json
```

Report the selected revisions, imported counts, verification result, and lockfile location. Importing source verification claims does not rerun the builds they describe. Use `sql-context` to retrieve the imported evidence and manage task notes afterward.
