-- Account identities are PostgreSQL logins, never caller-controlled session settings.
BEGIN;
CREATE ROLE context_remote_reader NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
CREATE ROLE context_remote_writer NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
CREATE ROLE context_trace_service NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
DO $$ BEGIN EXECUTE format('REVOKE CREATE,TEMPORARY ON DATABASE %I FROM PUBLIC', current_database()); EXECUTE format('GRANT CONNECT ON DATABASE %I TO context_remote_reader,context_remote_writer,context_trace_service', current_database()); END $$;
CREATE SCHEMA remote;
CREATE SCHEMA memory;
CREATE SCHEMA trace;
REVOKE ALL ON SCHEMA remote,memory,trace FROM PUBLIC;
CREATE TABLE remote.account (account_id uuid PRIMARY KEY DEFAULT gen_random_uuid(), label text NOT NULL);
CREATE TABLE remote.tenant (tenant_id uuid PRIMARY KEY DEFAULT gen_random_uuid(), label text NOT NULL);
CREATE TABLE remote.membership (
 account_id uuid NOT NULL REFERENCES remote.account, tenant_id uuid NOT NULL REFERENCES remote.tenant,
 PRIMARY KEY(account_id,tenant_id)
);
CREATE TABLE remote.login_identity (role_name name PRIMARY KEY, account_id uuid NOT NULL REFERENCES remote.account);
CREATE FUNCTION remote.current_account() RETURNS uuid LANGUAGE sql STABLE SECURITY DEFINER
 SET search_path=pg_catalog AS $$ SELECT account_id FROM remote.login_identity WHERE role_name=session_user $$;
CREATE FUNCTION remote.is_member(id uuid) RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER
 SET search_path=pg_catalog AS $$ SELECT EXISTS(SELECT 1 FROM remote.membership WHERE account_id=remote.current_account() AND tenant_id=id) $$;
REVOKE ALL ON FUNCTION remote.current_account(),remote.is_member(uuid) FROM PUBLIC;
GRANT USAGE ON SCHEMA remote,memory,trace,catalog TO context_remote_reader,context_remote_writer;
GRANT EXECUTE ON FUNCTION remote.current_account(),remote.is_member(uuid) TO context_remote_reader,context_remote_writer;
GRANT SELECT ON ALL TABLES IN SCHEMA catalog TO context_remote_reader,context_remote_writer;
CREATE TABLE memory.entry (
 entry_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
 author_id uuid NOT NULL DEFAULT remote.current_account() REFERENCES remote.account,
 tenant_id uuid REFERENCES remote.tenant,
 visibility text NOT NULL DEFAULT 'private' CHECK(visibility IN ('private','tenant','public')),
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 CHECK((visibility='tenant')=(tenant_id IS NOT NULL))
);
CREATE TABLE memory.revision (
 revision_id uuid PRIMARY KEY DEFAULT gen_random_uuid(), entry_id uuid NOT NULL REFERENCES memory.entry,
 parent_revision_id uuid, body jsonb NOT NULL CHECK(jsonb_typeof(body)='object'),
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 UNIQUE(entry_id,revision_id),
 FOREIGN KEY(entry_id,parent_revision_id) REFERENCES memory.revision(entry_id,revision_id)
);
CREATE INDEX memory_revision_entry ON memory.revision(entry_id,created_at);
ALTER TABLE memory.entry ENABLE ROW LEVEL SECURITY;
ALTER TABLE memory.entry FORCE ROW LEVEL SECURITY;
ALTER TABLE memory.revision ENABLE ROW LEVEL SECURITY;
ALTER TABLE memory.revision FORCE ROW LEVEL SECURITY;
CREATE POLICY entry_read ON memory.entry FOR SELECT TO context_remote_reader,context_remote_writer
 USING(author_id=remote.current_account() OR visibility='public' OR (visibility='tenant' AND remote.is_member(tenant_id)));
CREATE POLICY entry_insert ON memory.entry FOR INSERT TO context_remote_writer
 WITH CHECK(author_id=remote.current_account() AND (tenant_id IS NULL OR remote.is_member(tenant_id)));
CREATE POLICY revision_read ON memory.revision FOR SELECT TO context_remote_reader,context_remote_writer
 USING(EXISTS(SELECT 1 FROM memory.entry e WHERE e.entry_id=revision.entry_id));
CREATE POLICY revision_insert ON memory.revision FOR INSERT TO context_remote_writer
 WITH CHECK(EXISTS(SELECT 1 FROM memory.entry e WHERE e.entry_id=revision.entry_id AND e.author_id=remote.current_account()));
GRANT SELECT ON memory.entry,memory.revision TO context_remote_reader,context_remote_writer;
GRANT INSERT ON memory.entry,memory.revision TO context_remote_writer;
CREATE TRIGGER immutable BEFORE UPDATE OR DELETE ON memory.entry FOR EACH ROW EXECUTE FUNCTION catalog.reject_mutation();
CREATE TRIGGER immutable BEFORE UPDATE OR DELETE ON memory.revision FOR EACH ROW EXECUTE FUNCTION catalog.reject_mutation();
CREATE TABLE trace.prompt (
 prompt_id uuid PRIMARY KEY DEFAULT gen_random_uuid(), account_id uuid NOT NULL REFERENCES remote.account,
 project text NOT NULL, task text NOT NULL, session_id text, sequence bigint CHECK(sequence>=0),
 parent_prompt_id uuid, text text NOT NULL,
 kind text NOT NULL DEFAULT 'prompt' CHECK(kind IN ('prompt','summary')),
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 UNIQUE(account_id,prompt_id), UNIQUE(account_id,session_id,sequence),
 FOREIGN KEY(account_id,parent_prompt_id) REFERENCES trace.prompt(account_id,prompt_id)
);
CREATE TABLE trace.query (
 query_id uuid PRIMARY KEY DEFAULT gen_random_uuid(), account_id uuid NOT NULL REFERENCES remote.account,
 prompt_id uuid NOT NULL, sql text NOT NULL, parameters jsonb NOT NULL DEFAULT '[]',
 role text NOT NULL CHECK(role IN ('reader','writer')),
 started_at timestamptz NOT NULL DEFAULT clock_timestamp(), finished_at timestamptz,
 outcome text NOT NULL DEFAULT 'started' CHECK(outcome IN ('started','succeeded','failed','unknown')),
 response jsonb,
 FOREIGN KEY(account_id,prompt_id) REFERENCES trace.prompt(account_id,prompt_id)
);
CREATE INDEX query_prompt ON trace.query(prompt_id,started_at);
ALTER TABLE trace.prompt ENABLE ROW LEVEL SECURITY;
ALTER TABLE trace.prompt FORCE ROW LEVEL SECURITY;
ALTER TABLE trace.query ENABLE ROW LEVEL SECURITY;
ALTER TABLE trace.query FORCE ROW LEVEL SECURITY;
CREATE POLICY prompt_read ON trace.prompt FOR SELECT TO context_remote_reader,context_remote_writer USING(account_id=remote.current_account());
CREATE POLICY query_read ON trace.query FOR SELECT TO context_remote_reader,context_remote_writer USING(account_id=remote.current_account());
CREATE POLICY prompt_service ON trace.prompt TO context_trace_service USING(true) WITH CHECK(true);
CREATE POLICY query_service ON trace.query TO context_trace_service USING(true) WITH CHECK(true);
GRANT SELECT ON trace.prompt,trace.query TO context_remote_reader,context_remote_writer;
GRANT USAGE ON SCHEMA trace,remote TO context_trace_service;
GRANT SELECT ON remote.account,remote.tenant,remote.membership,remote.login_identity TO context_trace_service;
GRANT SELECT,INSERT ON trace.prompt TO context_trace_service;
GRANT SELECT,INSERT,UPDATE ON trace.query TO context_trace_service;
CREATE TRIGGER immutable BEFORE UPDATE OR DELETE ON trace.prompt FOR EACH ROW EXECUTE FUNCTION catalog.reject_mutation();
COMMENT ON SCHEMA memory IS 'Unverified account contributions. Visibility is fixed per entry; revisions preserve authorship and source history.';
COMMENT ON SCHEMA trace IS 'Private prompt and SQL history. Only the service records attempts and outcomes; account SQL can read its own trace.';
COMMIT;
