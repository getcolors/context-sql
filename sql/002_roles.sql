-- Run as role administrator once. Login creation/credential delivery are operator-owned.
CREATE ROLE context_reader NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
CREATE ROLE context_writer NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
GRANT USAGE ON SCHEMA catalog TO context_reader,context_writer;
GRANT SELECT ON ALL TABLES IN SCHEMA catalog TO context_reader,context_writer;
GRANT USAGE ON SCHEMA working TO context_writer;
GRANT SELECT,INSERT,UPDATE,DELETE ON working.run,working.item TO context_writer;
GRANT USAGE ON ALL SEQUENCES IN SCHEMA working TO context_writer;
-- No CREATE on either schema; no EXECUTE paths to modify published catalog.
-- Grant these groups to separate scoped LOGINs. Never give the agent migration ownership.
