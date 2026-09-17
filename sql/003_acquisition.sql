-- Apply after 001_schema.sql and 002_roles.sql, including on existing databases.
BEGIN;
-- Historical rows predate mode capture. Do not invent provenance for those rows.
ALTER TABLE catalog.source_file ADD COLUMN git_mode text
  CHECK (git_mode IN ('100644', '100755'));

CREATE TABLE catalog.acquisition (
  acquisition_id text PRIMARY KEY CHECK (acquisition_id ~ '^[0-9a-f]{64}$'),
  repository_url text NOT NULL CHECK (repository_url = 'https://github.com/getcolors/skills'),
  git_commit text NOT NULL CHECK (git_commit ~ '^[0-9a-f]{40}$'),
  skills_cli_version text NOT NULL CHECK (skills_cli_version ~ '^[0-9]+\.[0-9]+\.[0-9]+$'),
  command jsonb NOT NULL CHECK (jsonb_typeof(command) = 'array'),
  acquired_at timestamptz NOT NULL,
  manifest_sha256 text NOT NULL CHECK (manifest_sha256 ~ '^[0-9a-f]{64}$'),
  verification_method text NOT NULL CHECK (verification_method = 'git-tree-and-byte-comparison-v1')
);
CREATE TABLE catalog.skill_acquisition (
  acquisition_id text NOT NULL REFERENCES catalog.acquisition,
  skill_slug text NOT NULL,
  version_id text NOT NULL,
  PRIMARY KEY (acquisition_id, skill_slug),
  FOREIGN KEY (skill_slug, version_id) REFERENCES catalog.skill_version
);
CREATE TRIGGER immutable BEFORE UPDATE OR DELETE ON catalog.acquisition
  FOR EACH ROW EXECUTE FUNCTION catalog.reject_mutation();
CREATE TRIGGER immutable BEFORE UPDATE OR DELETE ON catalog.skill_acquisition
  FOR EACH ROW EXECUTE FUNCTION catalog.reject_mutation();
REVOKE ALL ON catalog.acquisition, catalog.skill_acquisition FROM PUBLIC;
GRANT SELECT ON catalog.acquisition, catalog.skill_acquisition TO context_reader, context_writer;
COMMENT ON TABLE catalog.acquisition IS 'Pinned skills CLI download, UTC acquisition time, and independently verified upstream file manifest. Import does not reverify source claims.';
COMMENT ON TABLE catalog.skill_acquisition IS 'Connects immutable skill versions to their verified downloads. Reacquisition may reuse a content version.';
COMMENT ON COLUMN catalog.source_file.git_mode IS 'Upstream regular-file mode. NULL for historical imports without recorded modes.';
COMMENT ON TABLE catalog.source_file IS 'Exact acquired bytes, compared with upstream Git objects before import. Retrieval needs no staged files.';
COMMIT;
