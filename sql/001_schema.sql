-- PostgreSQL 16+. Run once as migration owner in an empty database.
BEGIN;
CREATE SCHEMA catalog;
CREATE SCHEMA working;
CREATE TABLE catalog.skill (
  slug text PRIMARY KEY CHECK (slug ~ '^[a-z0-9]+(-[a-z0-9]+)*$'),
  kind text NOT NULL CHECK (kind IN ('context', 'generic')),
  current_version text
);
CREATE TABLE catalog.skill_version (
  skill_slug text NOT NULL REFERENCES catalog.skill(slug),
  version_id text NOT NULL CHECK (version_id ~ '^[0-9a-f]{64}$'),
  repository_url text NOT NULL,
  git_commit text NOT NULL CHECK (git_commit ~ '^[0-9a-f]{40,64}$'),
  source_directory text NOT NULL,
  routing_description text NOT NULL,
  importer_version text NOT NULL,
  evidence_status text NOT NULL DEFAULT 'source_reported' CHECK (evidence_status = 'source_reported'),
  PRIMARY KEY (skill_slug, version_id)
);
ALTER TABLE catalog.skill ADD FOREIGN KEY (slug, current_version)
  REFERENCES catalog.skill_version(skill_slug, version_id) DEFERRABLE INITIALLY DEFERRED;
CREATE TABLE catalog.source_file (
  skill_slug text NOT NULL,
  version_id text NOT NULL,
  path text NOT NULL CHECK (path !~ '(^/|(^|/)\.\.(/|$))'),
  content bytea NOT NULL,
  sha256 text NOT NULL CHECK (sha256 = encode(sha256(content), 'hex')),
  line_count integer NOT NULL CHECK (line_count >= 0),
  PRIMARY KEY (skill_slug, version_id, path),
  FOREIGN KEY (skill_slug, version_id) REFERENCES catalog.skill_version
);
CREATE TABLE catalog.section (
  skill_slug text NOT NULL,
  version_id text NOT NULL,
  path text NOT NULL,
  ordinal integer NOT NULL CHECK (ordinal >= 0),
  kind text NOT NULL CHECK (kind IN ('routing','failure','pins','acceptance','contract','prose')),
  heading text NOT NULL,
  heading_path text[] NOT NULL,
  start_line integer NOT NULL CHECK (start_line > 0),
  end_line integer NOT NULL CHECK (end_line >= start_line),
  body text NOT NULL,
  search tsvector GENERATED ALWAYS AS (
    setweight(to_tsvector('simple', heading), 'A') || setweight(to_tsvector('simple', body), 'B')
  ) STORED,
  PRIMARY KEY (skill_slug, version_id, path, ordinal),
  FOREIGN KEY (skill_slug, version_id, path) REFERENCES catalog.source_file
);
CREATE INDEX section_search_idx ON catalog.section USING gin(search);
CREATE INDEX section_kind_idx ON catalog.section(kind, skill_slug, version_id);
CREATE TABLE catalog.pin_row (
  skill_slug text NOT NULL, version_id text NOT NULL, path text NOT NULL,
  section_ordinal integer NOT NULL, line_number integer NOT NULL CHECK (line_number > 0),
  component text NOT NULL, value_text text NOT NULL, cells jsonb NOT NULL,
  PRIMARY KEY (skill_slug, version_id, path, line_number),
  FOREIGN KEY (skill_slug, version_id, path, section_ordinal)
    REFERENCES catalog.section(skill_slug, version_id, path, ordinal)
);
CREATE TABLE catalog.eval_case (
  skill_slug text NOT NULL, version_id text NOT NULL, path text NOT NULL,
  ordinal integer NOT NULL CHECK (ordinal >= 0),
  source_id text, name text, prompt text NOT NULL, expected_output text,
  assertions jsonb NOT NULL CHECK (jsonb_typeof(assertions) = 'array'),
  raw_case jsonb NOT NULL CHECK (jsonb_typeof(raw_case) = 'object'),
  PRIMARY KEY (skill_slug, version_id, path, ordinal),
  FOREIGN KEY (skill_slug, version_id, path) REFERENCES catalog.source_file
);
-- Version material is append-only, including derived representations.
CREATE FUNCTION catalog.reject_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN RAISE EXCEPTION 'Catalog version material is immutable; import a new revision'; END $$;
DO $$ DECLARE t text; BEGIN
  FOREACH t IN ARRAY ARRAY['skill_version','source_file','section','pin_row','eval_case'] LOOP
    EXECUTE format('CREATE TRIGGER immutable BEFORE UPDATE OR DELETE ON catalog.%I FOR EACH ROW EXECUTE FUNCTION catalog.reject_mutation()', t);
  END LOOP;
END $$;
CREATE VIEW catalog.current_section AS
SELECT s.*, v.git_commit, v.repository_url,
  v.repository_url || '/blob/' || v.git_commit || '/' || v.source_directory || '/' || s.path ||
  '#L' || s.start_line || '-L' || s.end_line AS citation
FROM catalog.section s JOIN catalog.skill k ON k.slug=s.skill_slug AND k.current_version=s.version_id
JOIN catalog.skill_version v USING (skill_slug, version_id);
CREATE VIEW catalog.current_eval AS
SELECT e.* FROM catalog.eval_case e JOIN catalog.skill k ON k.slug=e.skill_slug AND k.current_version=e.version_id;
-- Working memory has a different owner/lifecycle from published knowledge.
CREATE TABLE working.run (
  run_id uuid PRIMARY KEY,
  owner_name name NOT NULL DEFAULT session_user,
  task text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  expires_at timestamptz NOT NULL DEFAULT now() + interval '7 days',
  CHECK (expires_at > created_at)
);
CREATE TABLE working.item (
  run_id uuid NOT NULL REFERENCES working.run ON DELETE CASCADE,
  item_id bigint GENERATED ALWAYS AS IDENTITY,
  kind text NOT NULL CHECK (kind IN ('observation','hypothesis','decision','todo','tool_result','summary')),
  body text NOT NULL,
  priority integer NOT NULL DEFAULT 0,
  state text NOT NULL DEFAULT 'active' CHECK (state IN ('active','superseded','done')),
  source_skill text, source_version text, source_path text, source_section integer,
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (run_id, item_id),
  FOREIGN KEY (source_skill,source_version,source_path,source_section)
    REFERENCES catalog.section(skill_slug,version_id,path,ordinal) MATCH FULL
);
CREATE INDEX working_active_idx ON working.item(run_id, priority DESC) WHERE state='active';
ALTER TABLE working.run ENABLE ROW LEVEL SECURITY;
ALTER TABLE working.run FORCE ROW LEVEL SECURITY;
CREATE POLICY run_owner ON working.run USING (owner_name=session_user) WITH CHECK (owner_name=session_user);
ALTER TABLE working.item ENABLE ROW LEVEL SECURITY;
ALTER TABLE working.item FORCE ROW LEVEL SECURITY;
CREATE POLICY item_owner ON working.item USING
  (EXISTS (SELECT 1 FROM working.run r WHERE r.run_id=item.run_id AND r.owner_name=session_user));
REVOKE ALL ON SCHEMA catalog, working FROM PUBLIC;
REVOKE ALL ON ALL TABLES IN SCHEMA catalog, working FROM PUBLIC;
COMMIT;
COMMENT ON TABLE catalog.skill_version IS 'Immutable source revision. source_reported preserves author claims; import does not establish live verification.';
COMMENT ON TABLE catalog.source_file IS 'Lossless source bytes from Git, independently checkable by SHA-256. Not an executable asset copy.';
COMMENT ON TABLE catalog.section IS 'Mechanical nonoverlapping Markdown slices. Headings and line bounds cite source, not curated claim assertions.';
COMMENT ON TABLE catalog.pin_row IS 'Literal Markdown pin-table rows. heading_path in the referenced section scopes provider/build sets.';
COMMENT ON COLUMN catalog.pin_row.value_text IS 'Source text, not a machine-resolved version or compatibility range. Pin prose remains in sections.';
COMMENT ON TABLE catalog.eval_case IS 'Evaluation inputs and expected outputs, not execution results or proof of passing.';
COMMENT ON TABLE working.item IS 'Agent-authored working memory, never promoted implicitly to the source catalog. Optional version-scoped citation.';
