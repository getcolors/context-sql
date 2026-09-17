\set ON_ERROR_STOP on
DO $$
DECLARE n integer;
BEGIN
 SELECT count(*) INTO n FROM catalog.skill;
 IF n<>14 THEN RAISE EXCEPTION 'Expected 14 skills, got %',n; END IF;
 SELECT count(*) INTO n FROM catalog.skill WHERE kind='context';
 IF n<>13 THEN RAISE EXCEPTION 'Expected 13 Context Skills'; END IF;
 SELECT count(*) INTO n FROM catalog.source_file;
 IF n<>80 THEN RAISE EXCEPTION 'Expected 80 source files'; END IF;
 IF EXISTS (SELECT FROM catalog.skill WHERE current_version IS NULL) THEN RAISE EXCEPTION 'Missing current version'; END IF;
 IF NOT EXISTS (SELECT FROM catalog.current_section WHERE search @@ plainto_tsquery('simple','NOAUTH'))
 THEN RAISE EXCEPTION 'Symptom lookup returned no matches'; END IF;
 BEGIN
   UPDATE catalog.skill_version SET repository_url='invalid';
   RAISE EXCEPTION 'Immutable trigger failed';
 EXCEPTION WHEN raise_exception THEN
   IF SQLERRM NOT LIKE 'Catalog version material is immutable%' THEN RAISE; END IF;
 END;
 BEGIN
   UPDATE catalog.acquisition SET acquired_at=now();
   RAISE EXCEPTION 'Acquisition mutation unexpectedly allowed';
 EXCEPTION WHEN raise_exception THEN
   IF SQLERRM NOT LIKE 'Catalog version material is immutable%' THEN RAISE; END IF;
 END;
 BEGIN
   DELETE FROM catalog.skill_acquisition;
   RAISE EXCEPTION 'Acquisition link deletion unexpectedly allowed';
 EXCEPTION WHEN raise_exception THEN
   IF SQLERRM NOT LIKE 'Catalog version material is immutable%' THEN RAISE; END IF;
 END;
 BEGIN
   INSERT INTO catalog.section VALUES ('redis-single-node',repeat('0',64),'SKILL.md',0,'prose','bad','{}',1,1,'bad',DEFAULT);
   RAISE EXCEPTION 'Cross-version foreign key accepted';
 EXCEPTION WHEN foreign_key_violation THEN NULL;
 END;
END $$;
