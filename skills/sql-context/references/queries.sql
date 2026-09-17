-- Execute one selected statement at a time. Bind %s values with --params JSON.
-- Discovery: only Context Skills, excluding generic Agent Skills.
SELECT k.slug, v.version_id, v.routing_description, v.git_commit
FROM catalog.skill k JOIN catalog.skill_version v
  ON (v.skill_slug, v.version_id) = (k.slug, k.current_version)
WHERE k.kind = 'context' ORDER BY k.slug LIMIT 30;

-- Full-text evidence search. Parameters: ["PostHog storage"]
SELECT skill_slug, version_id, path, ordinal, heading, citation,
       left(body, 2000) AS excerpt
FROM catalog.current_section
WHERE search @@ plainto_tsquery('simple', %s)
ORDER BY skill_slug, path, ordinal LIMIT 20;

-- Exact source bytes, one bounded slice. Parameters: [1, "slug", "version-id", "SKILL.md"]
SELECT path, sha256, octet_length(content) AS total_bytes,
       encode(substring(content FROM %s FOR 4096), 'base64') AS content_base64
FROM catalog.source_file
WHERE skill_slug = %s AND version_id = %s AND path = %s;
-- For the statement above bind [1, "slug", "version-id", "SKILL.md"].
-- PostgreSQL byte offsets start at 1; request subsequent slices explicitly.

-- Restore visible memory. Parameters: ["amiorin/posthog", "create-package-skill"]
SELECT e.entry_id, e.author_id, e.visibility, r.revision_id,
       r.parent_revision_id, r.body, r.created_at
FROM memory.entry e JOIN memory.revision r USING (entry_id)
WHERE r.body->>'project' = %s AND r.body->>'task' = %s
ORDER BY r.created_at DESC, r.revision_id LIMIT 30;

-- Create private memory; requires --role writer. Keep the returned entry_id.
INSERT INTO memory.entry DEFAULT VALUES RETURNING entry_id;

-- Append its initial revision in the next invocation; requires --role writer.
-- Parameters: ["entry-uuid", "amiorin/posthog", "create-package-skill", "decision", "Use official deployment documentation"]
INSERT INTO memory.revision(entry_id, body)
VALUES (%s::uuid, jsonb_build_object('project', %s::text, 'task', %s::text,
                                    'kind', %s::text, 'text', %s::text))
RETURNING entry_id, revision_id;

-- Revise your entry without overwriting history; requires --role writer.
-- Parameters: ["entry-uuid", "parent-revision-uuid", "{\"text\":\"Updated observation\"}"]
INSERT INTO memory.revision(entry_id, parent_revision_id, body)
VALUES (%s::uuid, %s::uuid, %s::jsonb) RETURNING revision_id;

-- Trace reconstruction for an owned prompt. Parameters: ["prompt-uuid"]
SELECT p.*, q.* FROM trace.prompt p LEFT JOIN trace.query q USING (prompt_id)
WHERE p.prompt_id = %s::uuid LIMIT 30;
