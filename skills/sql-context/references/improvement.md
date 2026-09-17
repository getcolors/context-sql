# Improve retrieval using request traces

Use only traces the authenticated account can access and that the user authorized for this review. Do not broaden sharing to build an evaluation corpus. Query `trace.prompt` and its linked `trace.query` records in bounded slices. Prompt summaries are not original prompts; missing result captures prevent exact response reconstruction.

Use this review prompt with the selected trace records:

> Review these requests, SQL operations, outcomes, and captured evidence. Identify repeated retrieval difficulties supported by specific prompt/query IDs. Propose the smallest useful change to reference queries, indexes, views, or the data model. Explain which request becomes easier and what provenance or access rules must remain intact. Separate observed outcomes from hypotheses. Supply a candidate migration only when simpler query changes cannot address the demonstrated problem. Define an evaluation using held-out requests and identify any missing evidence. Do not execute production DDL.

Prefer a query or reference improvement when it solves the problem. Consider an index for measured expensive access paths, a view for repeated joins, and a schema change for repeated structural gaps. Fewer SQL calls are not sufficient evidence of better answers.

Evaluate proposed changes in a separate database using representative, authorized data. Replay successful and failed requests plus held-out requests. Compare evidence relevance, citation correctness, response completeness, query time, output size, and tenant isolation. Include private, tenant, and public memory access tests using real restricted identities. Preserve exact catalog bytes and immutable version references.

Save the proposal, cited traces, baseline, candidate results, and limitations as working memory. Keep acceptance separate from the LLM's recommendation. An operator reviews and versions accepted migrations and their rollback/recovery plan before production application. This skill does not schedule background self-modification or grant the query role DDL privileges.
