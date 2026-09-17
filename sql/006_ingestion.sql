-- Acquisition supports independently verified Context Skills from other repositories.
BEGIN;
ALTER TABLE catalog.acquisition DROP CONSTRAINT acquisition_repository_url_check;
ALTER TABLE catalog.acquisition ADD CONSTRAINT acquisition_repository_url_check
  CHECK (repository_url ~ '^https://github[.]com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$');
ALTER TABLE catalog.acquisition DROP CONSTRAINT acquisition_skills_cli_version_check;
ALTER TABLE catalog.acquisition ADD CONSTRAINT acquisition_skills_cli_version_check
  CHECK (skills_cli_version ~ '^[0-9]+[.][0-9]+[.][0-9]+(-[0-9A-Za-z.-]+)?([+][0-9A-Za-z.-]+)?$');
CREATE TABLE catalog.source_resolution (
  acquisition_id text PRIMARY KEY REFERENCES catalog.acquisition,
  requested_source text NOT NULL,
  requested_ref text NOT NULL,
  resolved_at timestamptz NOT NULL
);
CREATE TABLE catalog.ingestion_lock (
  sha256 text PRIMARY KEY CHECK (sha256 ~ '^[0-9a-f]{64}$'),
  document jsonb NOT NULL CHECK (jsonb_typeof(document) = 'object'),
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TRIGGER immutable BEFORE UPDATE OR DELETE ON catalog.source_resolution
  FOR EACH ROW EXECUTE FUNCTION catalog.reject_mutation();
CREATE TRIGGER immutable BEFORE UPDATE OR DELETE ON catalog.ingestion_lock
  FOR EACH ROW EXECUTE FUNCTION catalog.reject_mutation();
REVOKE ALL ON catalog.source_resolution, catalog.ingestion_lock FROM PUBLIC;
GRANT SELECT ON catalog.source_resolution, catalog.ingestion_lock TO context_reader, context_writer;
COMMENT ON TABLE catalog.source_resolution IS 'Original source and reference resolved once before acquiring the exact recorded commit.';
COMMENT ON TABLE catalog.ingestion_lock IS 'Portable catalog lock committed atomically with all imported payloads; excludes task memory and local credentials.';
COMMIT;
