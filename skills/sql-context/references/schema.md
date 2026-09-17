# SQL reference

Use `scripts/remote.py schema` for the connected service's schema. These relationships explain the bundled query examples; migrations on the service determine the available columns and permissions.

## Verified source catalog

- `catalog.skill`: `slug`, `kind` (`context` or `generic`), `current_version`.
- `catalog.skill_version`: composite key `(skill_slug, version_id)`; `repository_url`, `git_commit`, `source_directory`, `routing_description`, `evidence_status`.
- `catalog.source_file`: key `(skill_slug, version_id, path)`; exact `content` bytea, `sha256`, `line_count`. Paths are relative to the skill root.
- `catalog.section`: key `(skill_slug, version_id, path, ordinal)`; `heading`, `heading_path`, `start_line`, `end_line`, `body`, indexed `search` tsvector.
- `catalog.current_section`: current sections plus upstream `git_commit`, `repository_url`, and `citation` URL.

Use immutable version IDs when saving citations. A current-version pointer can change after ingestion. `evidence_status = source_reported` preserves upstream claims and does not mean the service repeated their tests. Catalog writes use separate verified ingestion, never the SQL client.

## Shared memory

- `memory.entry`: `entry_id` UUID, `author_id` UUID, nullable `tenant_id`, `visibility` (`private`, `tenant`, `public`), `created_at`.
- `memory.revision`: `revision_id` UUID, `entry_id`, nullable `parent_revision_id`, object-valued `body` JSONB, `created_at`.

The server supplies author identity from the authenticated account. Use the defaults; SQL arguments cannot select a different author. Tenant visibility requires a tenant ID and membership. Private entries are visible to their author, tenant entries to members, and public entries to authenticated accounts. Only authors append revisions. Both tables preserve prior records; visibility is set when the entry is created. Parent revision IDs belong to the same entry. Branching revisions are possible, so timestamp order alone does not establish an accepted latest revision.

The example JSON body uses `project`, `task`, `kind`, `text`, and optional immutable catalog `source` references. These keys are conventions in the example, not a structured schema enforced by the database. Keep agent-authored observations distinguishable from verified source files.

## Prompt and query traces

`trace.prompt` records the explicitly submitted text or summary, account, project, task, parent, and timestamp. `trace.query` links SQL and parameters to a prompt and records execution state. Read your account's traces through SQL; service-owned connections write them. Ordinary reader/writer SQL cannot edit trace history. Prompt and query UUIDs correlate operations and grant no access themselves.

Results are reconstructable only when captured. A trace with an attempt but no completion may indicate interruption; it is not proof of rollback. Inspect the affected memory before retrying a write.
