\set ON_ERROR_STOP on
-- Run only in the disposable database created by scripts/check.sh.
CREATE ROLE context_test_alice LOGIN;
CREATE ROLE context_test_bob LOGIN;
GRANT context_reader, context_writer TO context_test_alice, context_test_bob;
SET SESSION AUTHORIZATION context_test_alice;
INSERT INTO working.run(run_id, task) VALUES ('11111111-1111-1111-1111-111111111111', 'Diagnose a source-reported symptom');
INSERT INTO working.item(run_id,kind,body) VALUES ('11111111-1111-1111-1111-111111111111','hypothesis','This is private session memory, not catalog evidence.');
DO $$ BEGIN
  IF (SELECT count(*) FROM catalog.skill) <> 14 THEN RAISE EXCEPTION 'reader cannot see corpus'; END IF;
  BEGIN
    UPDATE catalog.skill SET kind='generic';
    RAISE EXCEPTION 'catalog write unexpectedly allowed';
  EXCEPTION WHEN insufficient_privilege THEN NULL; END;
  BEGIN
    INSERT INTO working.run(run_id,owner_name,task) VALUES ('22222222-2222-2222-2222-222222222222','context_test_bob','spoof');
    RAISE EXCEPTION 'owner spoof unexpectedly allowed';
  EXCEPTION WHEN insufficient_privilege THEN NULL; END;
END $$;
RESET SESSION AUTHORIZATION;
SET SESSION AUTHORIZATION context_test_bob;
-- Changing an arbitrary setting must not change the login identity used by RLS.
SET app.current_tenant_id = 'context_test_alice';
DO $$ BEGIN
  IF EXISTS (SELECT FROM working.run) OR EXISTS (SELECT FROM working.item) THEN
    RAISE EXCEPTION 'cross-login working memory disclosure';
  END IF;
  BEGIN
    INSERT INTO working.item(run_id,kind,body) VALUES ('11111111-1111-1111-1111-111111111111','observation','cross-owner write');
    RAISE EXCEPTION 'cross-login write unexpectedly allowed';
  EXCEPTION WHEN insufficient_privilege THEN NULL; END;
END $$;
RESET SESSION AUTHORIZATION;
CREATE ROLE context_test_reader LOGIN;
GRANT context_reader TO context_test_reader;
SET SESSION AUTHORIZATION context_test_reader;
DO $$ BEGIN
  BEGIN
    DELETE FROM catalog.acquisition;
    RAISE EXCEPTION 'reader acquisition write unexpectedly allowed';
  EXCEPTION WHEN insufficient_privilege THEN NULL; END;
  BEGIN
    INSERT INTO working.run(run_id,task) VALUES ('33333333-3333-3333-3333-333333333333','reader write');
    RAISE EXCEPTION 'reader write unexpectedly allowed';
  EXCEPTION WHEN insufficient_privilege THEN NULL; END;
END $$;
RESET SESSION AUTHORIZATION;
SELECT 'role and login isolation checks passed' AS result;
