BEGIN;
-- Refuse a partial backfill when the migration login cannot see every owner.
SET LOCAL row_security = off;
ALTER TABLE working.run ADD COLUMN project_id text;
ALTER TABLE working.run ADD COLUMN local_path text;
-- Preserve historical identity and UUIDs; never infer a repository from a path.
UPDATE working.run SET local_path=project_key WHERE project_key LIKE '/%';
ALTER TABLE working.run DROP CONSTRAINT run_task_identity;
ALTER TABLE working.run ADD CONSTRAINT run_task_identity CHECK (
  (project_key IS NULL AND project_id IS NULL AND task_key IS NULL) OR
  (task_key IS NOT NULL AND length(task_key) BETWEEN 1 AND 128 AND
   (project_key IS NOT NULL OR project_id IS NOT NULL))
);
ALTER TABLE working.run ADD CONSTRAINT run_portable_project CHECK (
  project_id IS NULL OR
  (length(project_id) <= 255 AND
   project_id ~ '^[a-z0-9][a-z0-9._-]*(/[a-z0-9][a-z0-9._-]*)+$')
);
ALTER TABLE working.run ADD CONSTRAINT run_legacy_project CHECK (
  project_key IS NULL OR length(project_key) BETWEEN 1 AND 4096
);
ALTER TABLE working.run ADD CONSTRAINT run_local_path CHECK (
  local_path IS NULL OR (local_path LIKE '/%' AND length(local_path) <= 4096)
);
ALTER TABLE working.run ADD CONSTRAINT run_project_id_task_unique
  UNIQUE (owner_name, project_id, task_key);
COMMENT ON COLUMN working.run.project_id IS 'Portable namespace/name ID chosen by the client. A routing key, not authorization.';
COMMENT ON COLUMN working.run.local_path IS 'Last explicitly supplied absolute checkout path. Optional metadata, never task identity.';
COMMENT ON COLUMN working.run.project_key IS 'Historical path-based identity retained as provenance. New clients use project_id.';
COMMIT;
