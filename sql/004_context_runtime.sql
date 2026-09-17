BEGIN;
-- Historical runs remain accessible by UUID. New runs also have stable task keys.
ALTER TABLE working.run ADD COLUMN project_key text;
ALTER TABLE working.run ADD COLUMN task_key text;
ALTER TABLE working.run ADD CONSTRAINT run_task_identity CHECK (
  (project_key IS NULL AND task_key IS NULL) OR
  (project_key IS NOT NULL AND task_key IS NOT NULL AND
   length(project_key) BETWEEN 1 AND 4096 AND length(task_key) BETWEEN 1 AND 128)
);
ALTER TABLE working.run ADD CONSTRAINT run_project_task_unique
  UNIQUE (owner_name, project_key, task_key);
COMMENT ON COLUMN working.run.project_key IS 'Canonical absolute project path chosen by the client. A routing key, not an authorization boundary.';
COMMENT ON COLUMN working.run.task_key IS 'Explicit task label used to find the same run after a client restart.';
COMMIT;
