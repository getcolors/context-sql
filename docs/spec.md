# SQL-managed context specification

Status: proposed contract for this repository. This document is not an extension endorsed by Agent Skills, PostgreSQL, YC, or the talk's author. It defines intended behavior; the README and test output identify what the reference implementation currently exercises.

## 1. Scope and decision

Use PostgreSQL to store and query the fourteen requested skill directories. Thirteen are Context Skills. `refresh-oci-token` is a generic procedural skill and must retain that classification. Its executable script is stored as evidence, never executed during ingestion or retrieval.

Keep Git as the authoring system for established skill content. The database contains versioned snapshots and searchable projections. Agent observations and task state belong in separate working tables. An agent can choose what to load and what to retain for its task. It cannot turn an unverified observation into an authoritative Context Skill merely by writing a row.

The initial deployment boundary is one trusted workspace. A database credential authorizes access to that workspace's shared catalog. Working-memory policies use `session_user` to separate database logins. Tasks under the same login share that login's access. A connection pool that reuses one login for different people does not provide person-level isolation. The design does not treat a profile name, a task identifier, or an application setting as proof of user identity.

## 2. Source and version contract

The importer MUST preserve each allowed source file's content, relative path, content digest, source repository, source commit, and skill association. It MUST reject path traversal and MUST NOT follow a symlink outside the selected skill root. A file that cannot be decoded without loss needs a binary representation or an explicit rejection. Silent replacement characters are not acceptable.

A skill version MUST identify the exact imported content. The importer MUST distinguish a clean committed snapshot from a dirty working tree. A commit SHA alone does not identify uncommitted edits. A content digest identifies those bytes, but it must not imply that they exist at a GitHub commit URL.

The reference importer reads Git objects at the requested commit and ignores uncommitted and untracked files. The captured checkout was clean. This choice makes its commit citations exact, but users must commit an intended source change before importing it.

Import time records when the catalog saw content. It is not a build-verification date. The importer MUST NOT fill a missing verification date with the current date. A rerun against identical input MUST produce the same logical catalog without duplicate versions. A changed input MUST produce a new version and preserve earlier evidence.

Section projections MUST retain their source file and line interval. Heading detection MUST respect fenced code blocks. The complete source remains available so a consumer can recover surrounding qualifications. The parser MUST identify its own version so projections can be regenerated after a parsing fix.

Pins MUST preserve source text and distinguish extracted values from unparsed prose. A detected version string does not establish a dependency relationship unless its source supports that relationship. Evaluation cases MUST preserve the source case identifier, prompt, expected behavior, assertions, and any irregular extra fields.

## 3. Relational responsibilities

The schema SHOULD separate these responsibilities. Physical table names live in the migration.

| Responsibility | Required information |
| --- | --- |
| Skill identity | Stable name, kind, source location, current-version pointer |
| Skill version | Content digest, source revision, routing description, provenance metadata |
| Source file | Relative path, raw content, digest, media type |
| Searchable section | Version, file, heading, line bounds, body, search representation |
| Pin | Component or source label, raw version evidence, source location, extraction status |
| Evaluation case | Version, user prompt, expected behavior, assertions, source payload |
| Agent task and context selection | Task identifier, ordered evidence references, selection reason, budget metadata |
| Observation or proposal | Author, evidence links, tentative status, review outcome |
| Query record | Actor, task, query identifier, outcome, duration, output size |

Use foreign keys for relationships and uniqueness constraints for identities. Use JSONB for irregular frontmatter and evaluation payloads. Search projections MUST be derived from preserved sources. A prose fragment tagged as a failure is searchable evidence, not a separately verified factual claim.

A current-version view is a convenience. A saved context selection MUST refer to immutable version and section identifiers so that a later import does not rewrite the basis of an earlier answer. If an operator retires a version, saved tasks must still explain which version they used.

## 4. Discovery and retrieval protocol

The agent starts with the schema description and a short catalog of skill names, kinds, and routing descriptions. It MUST NOT load every full skill before deciding relevance. This follows the [Agent Skills progressive-loading model](https://agentskills.io/specification).

For a user report, the reader SHOULD perform these steps.

1. Search literal error text and exact component or skill names. Preserve punctuation for this path.
2. Search indexed text for related symptoms. Apply provider, topology, component, and version filters when those facts are known.
3. Return a small candidate set with identifiers, source paths, source lines, version digest, and excerpts. Ranking scores order candidates; they are not confidence probabilities.
4. Load the selected skill's main instructions and the relevant reference sections. Expand around a section when it depends on a qualification or prior step.
5. Retrieve the pin and acceptance evidence needed to judge applicability. Mark unknown compatibility when the user's version does not match the documented version set.
6. Save the selected immutable references with a reason and an order. Cite these references in the answer.

An empty result is not evidence that no relevant skill exists. The agent may revise the query, inspect the catalog, or disclose that it found no supporting evidence. Query retries consume the same task budget.

Full-text search is the initial ranking method. An exact-match path complements it. Later embedding search MUST retain version filters and source citations, and MUST demonstrate benefit on held-out prompts before becoming the default.

## 5. Budgets and query execution

Recommended starting limits are 50 returned rows, 64 KiB of serialized output, a five-second statement timeout, and a maximum of eight retrieval calls per task. These are configuration defaults to evaluate, not measured optimal values. The controller reserves answer space before spending the remaining prompt budget on context.

The executor MUST apply both row and byte limits. One row can contain an entire large document. It MUST stop output before the byte cap, report truncation, and return a continuation strategy or the query needed to request a narrower slice. It SHOULD record actual model-token counts when a tokenizer is available. Character estimates MUST be labeled estimates.

The executor MUST use a restricted database role and a read-only transaction for retrieval. It MUST roll back or close the transaction on every path, including timeout and serialization failure. It MUST enforce an external deadline as well as the database timeout. Session defaults alone are insufficient if submitted SQL can change them.

A general SQL endpoint MUST accept one parsed read query, reject transaction control and stacked statements, and account for data-modifying CTEs and functions with side effects. A string-prefix test for `SELECT` is insufficient. A narrower reference reader MAY expose parameterized retrieval operations whose SQL remains inspectable. It must document that it does not accept unrestricted SQL.

JSON is the default for nested provenance. CSV is optional for flat tables. The service MUST escape either format correctly and return the same provenance fields. It MUST measure serialization cost before claiming token savings.

## 6. Privileges and untrusted content

Use separate owner, importer, catalog-reader, and working-memory-writer responsibilities. The agent MUST NOT connect as the owner, a superuser, or a role with `BYPASSRLS`, role administration, or server-file access. A read role MUST NOT inherit an importer or writer role. Revoke schema creation and unnecessary function execution where they could expose mutation or external access.

The proposed production executor MUST audit accessible extensions, functions, and `SECURITY DEFINER` code. A read-only query can still call a function. Read-only transactions and a replica supplement grants; they do not replace an authorization boundary.

Imported Markdown, query results, and executable script text are untrusted input. The agent MUST NOT treat an instruction embedded in retrieved material as authority to change permissions, reveal credentials, execute a script, or ignore task constraints. Stored shell and SQL examples require separate execution authorization.

For a later multi-tenant service, prefer separate databases and credentials. If shared tables are necessary, authenticated database identity must determine access, and every join path must preserve it. `SET LOCAL app.tenant_id` plus `current_setting` is not a security boundary when the agent can change that setting. PostgreSQL documents mutable [session settings](https://www.postgresql.org/docs/17/config-setting.html) and [RLS bypass rules](https://www.postgresql.org/docs/17/ddl-rowsecurity.html).

A public documentation tunnel MUST serve only reviewed static artifacts. Database ports, database credentials, local secret files, and query APIs must not become accessible through that tunnel.

## 7. Write-back and review

A task may append notes and selected evidence to working memory through a separately authorized write operation. Notes MUST identify their author and task. They MUST remain distinguishable from imported skill evidence. The catalog reader MUST NOT gain catalog writes to support note-taking.

A proposed correction MUST link the relevant source version, the new observation, and reproduction evidence. Promotion requires an explicit review decision and an update to the upstream authoring repository or another documented publisher. A successful import alone cannot promote a proposal.

Nightly analysis may suggest a new view or a revised skill. It MUST NOT overwrite verification claims because a language model found the revision persuasive. Record rejected and superseded proposals so later agents can see why they were not adopted. Do not store credentials, raw authentication tokens, or unnecessary personal data in notes or query logs.

## 8. Version decay and verification

Claims hold within the documented verification conditions. A new application pin means compatibility is unknown until the skill's retest procedure supplies evidence. Time since import is not a correctness score.

A future verification record SHOULD identify the tested version set, provider, topology, test procedure, result, timestamp, and evidence location. It should be possible to distinguish `supported-by-source`, `reverified`, `needs-retest`, `contradicted`, and `retired`. An importer must not infer `reverified` from a sentence that merely contains the word "verified".

Queries SHOULD expose the status and the pinned conditions with the retrieved claim. A version mismatch should prevent an unqualified recommendation. It need not hide the evidence, because an older diagnosis may still help a user investigate.

## 9. Acceptance and evaluation

Structural acceptance MUST prove that all fourteen directories appear, that the generic skill is classified correctly, and that each captured file can be reconstructed with its original digest. Reimport MUST be idempotent. A changed source fixture MUST create a distinguishable version. Malformed metadata and unsafe paths MUST fail with useful errors.

Database acceptance MUST run migrations and seed data in real PostgreSQL. It MUST exercise retrieval joins, foreign-key failures, text search, empty searches, and bounded output. Test the restricted login rather than only `SET ROLE` from an owner session. Confirm denied catalog writes, denied role escalation, and the absence of unintended server-file or network functions. If working-memory writes exist, test that those writes cannot alter the catalog.

Security tests MUST include stacked statements, data-modifying CTEs, oversized cells, cancellation, malicious instructions in source text, and attempts to change session settings. A deployment that claims tenant isolation also needs cross-tenant read and write tests under real tenant credentials.

Behavioral evaluation MUST use the imported user-in-trouble prompts. Split tuning and held-out cases by scenario, not by random paraphrases of the same incident. Compare a file-search baseline, SQL text search, and any proposed semantic search with the same model, task budget, and corpus revision. Record routing recall at five candidates, reciprocal rank, correct diagnosis, citation coverage, unsupported claims, version-mismatch handling, result bytes, model tokens, latency, and tool calls.

No database integrity test establishes diagnosis quality. No retrieval hit proves the answer followed acceptance doctrine. Human-reviewed expected outcomes are needed for behavioral claims. Release reports MUST separate tests actually run from proposed tests, and MUST avoid inventing a benchmark result for this initial artifact set.

## 10. Implementation coverage

The repository demonstrates storage and SQL access patterns. Its migration is a subset of the proposed service contract. This table prevents a requirement above from being mistaken for a completed runtime.

| Capability | Reference implementation | Remaining work |
| --- | --- | --- |
| Skill identities and versions | `catalog.skill` and `catalog.skill_version` distinguish kind, content version, source revision, and importer version. | Add a documented policy for retiring published versions. |
| Original files and citations | `catalog.source_file` stores bytes and hashes. `catalog.section` records headings and line bounds. The current-section view builds commit-based citations. | Add explicit media types and ingestion timestamps if a service needs them. |
| Pin evidence | `catalog.pin_row` preserves table cells and source locations. | Extraction status is not a dedicated field. Prose pins remain in sections and require interpretation. |
| Imported evals | `catalog.eval_case` preserves prompts, assertions, expected output, and raw JSON. | Run model-based routing and diagnosis evaluation. Imported cases alone are not evaluation results. |
| Catalog changes | Version tables reject updates and deletes. A current-version pointer selects active content. | Review and promotion are not implemented. Immutability does not restrict a database owner who can alter the schema. |
| Task memory | `working.run` and `working.item` store task text, notes, priority, state, and optional immutable section references. | Dedicated selection order, selection reasons, token budgets, and review outcomes have no dedicated fields. |
| Working-memory access | Forced RLS policies use `session_user`. The catalog is shared within the workspace. | A pooled service must map authenticated users to distinct database identities or introduce a separately tested authorization design. |
| SQL examples | Queries demonstrate discovery, search, citations, and joins. | No authenticated arbitrary-SQL gateway, parsed-query filter, enforced output budget, or external cancellation service is provided. |
| Query tracing | The specification defines query-record requirements. | No query-audit table or audit ingestion service is implemented. |
| Verification | Source evidence is labeled `source_reported`. | No fresh infrastructure verification, automatic version compatibility proof, or claimed LLM benchmark. |

`tests/access.sql` changes session authorization in a disposable superuser test session to exercise login-based policy semantics. `scripts/check.sh` also opens independent restricted-login connections over the disposable cluster's trusted Unix socket. These checks cover per-login visibility, denied catalog writes, and denied role escalation. They do not prove password authentication, network transport, or connection-pool behavior. A deployed service must test those separately.
