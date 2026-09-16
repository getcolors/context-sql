-- psql: -v symptom='NOAUTH' -v run_id='00000000-0000-0000-0000-000000000001'
-- Route cheaply: descriptions before loading full files.
SELECT s.slug, s.kind, v.routing_description
FROM catalog.skill s JOIN catalog.skill_version v
ON (v.skill_slug,v.version_id)=(s.slug,s.current_version)
WHERE v.routing_description ILIKE '%' || :'symptom' || '%';

-- Rank matching current sections, always returning immutable citations.
SELECT skill_slug, kind, heading, citation, body
FROM catalog.current_section
WHERE search @@ plainto_tsquery('simple', :'symptom')
ORDER BY ts_rank_cd(search, plainto_tsquery('simple', :'symptom')) DESC,
         skill_slug, path, ordinal LIMIT 6;

-- Error punctuation can disappear in FTS: use exact substring fallback.
SELECT skill_slug, heading, citation, body FROM catalog.current_section
WHERE position(lower(:'symptom') IN lower(body)) > 0
ORDER BY skill_slug,path,ordinal LIMIT 6;

-- Pins are literal source table rows. Heading scope distinguishes provider/build sets.
SELECT p.component,p.value_text,s.heading_path,v.git_commit
FROM catalog.pin_row p JOIN catalog.section s
ON (s.skill_slug,s.version_id,s.path,s.ordinal)=(p.skill_slug,p.version_id,p.path,p.section_ordinal)
JOIN catalog.skill k ON k.slug=p.skill_slug AND k.current_version=p.version_id
JOIN catalog.skill_version v ON (v.skill_slug,v.version_id)=(p.skill_slug,p.version_id)
WHERE p.skill_slug='redis-single-node' ORDER BY p.path,p.line_number;

-- Evals are fixtures, not successful evaluation results.
SELECT name,prompt,expected_output,assertions FROM catalog.current_eval
WHERE skill_slug='redis-single-node' ORDER BY ordinal;

-- Reconstruct bounded working context. Byte budgets are NOT token budgets.
WITH ranked AS (
 SELECT *,sum(octet_length(body)) OVER (ORDER BY priority DESC,item_id) AS cumulative_bytes
 FROM working.item WHERE run_id=:'run_id'::uuid AND state='active'
)
SELECT item_id,kind,body,source_skill,source_version FROM ranked
WHERE cumulative_bytes<=16000 ORDER BY priority DESC,item_id;

-- Retire stale runs (executed by the owning login; cascading delete removes items).
DELETE FROM working.run WHERE expires_at < now();
