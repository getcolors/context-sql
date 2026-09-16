# Research and source assessment

Research date: 2026-09-16.

## What specification exists?

There is no published SQL context-management standard in the supplied material. The material describes an architecture and practitioner experience. This repository supplies a proposed, testable specification in [spec.md](spec.md), a PostgreSQL model, and a conversion of the requested skills.

The closest primary event record is [PlanetScale's August SF Database Meetup](https://planetscale.com/events/sf-database-meetup-august). It names Lewis Ellis of Conductor and the talk "Giving Agents SQL", held August 19, 2026. The supplied context-warehouse essay links [this recording](https://www.youtube.com/watch?v=uMudSbZ79-A). The recording could not be fetched through the research browser, so this research does not claim transcript verification or endorse its timestamp-specific anecdotes.

Two existing specifications govern the skills being converted. The [Agent Skills specification](https://agentskills.io/specification) defines the portable directory format, YAML frontmatter, and progressive loading. The [Colors Context Skill standard](https://github.com/getcolors/workspace/blob/main/standards/context-skill.md) adds the verified-build definition, symptom routing, provenance, version pins, failure catalogue, trouble-shaped evals, and no-second-copy rule. Neither prescribes a database schema. SQL storage should preserve these contracts and retain a path back to the original files.

## The supplied essays

All three complete essays were read from the exact revisions supplied by the user.

| Essay | What it contributes | How this design uses it |
| --- | --- | --- |
| [YC AI playbook](https://gist.githubusercontent.com/amiorin/e87a69b752a627c12f1a10b4483e0f6d/raw/7334c526f4381f40f5477c87c76b03b254938b19/yc-ai-playbook.md) | Shared organizational data, schema-aware queries, query feedback, skill improvement | Separate a readable catalog from proposals that need verification before publication. |
| [Context warehouse](https://gist.githubusercontent.com/amiorin/2b112bdcc763d26871d77ff7165ce3fa/raw/6173373082adec618d0a14520276b2ec91f3f521/context-warehouse.md) | SQL as a common interface, persisted operational state, ingestion, views | Let the agent discover, select, cite, and revisit records through relational queries. |
| [Context in a database](https://gist.githubusercontent.com/amiorin/22ba97453a3503b74b25a004f38ca5fa/raw/1010f97b122677d29150d8a204064d7c92df0a44/context-in-a-db.md) | Read roles, query budgets, CSV output, curated views | Specify privileges and output bounds as runtime requirements. Correct the tenant-isolation claim below. |

The essays are secondary accounts. Their prose is not a protocol, a benchmark, or a security proof. Their agreement establishes a useful design direction, not independent corroboration.

## Claims checked and limits retained

[Y Combinator's own episode announcement](https://www.linkedin.com/posts/y-combinator_over-the-past-year-weve-been-building-our-activity-7465431734097063936-i8yJ) describes Pete Koomen's internal infrastructure, more than 350 tools, shared database access, and overnight skill loops. This supports the broad account. It does not establish every anecdote in the supplied essays. The essays variously identify "engineer Jared" and partner Jared Friedman, and attribute related decisions to different people. No schema or security requirement here depends on resolving that attribution.

[PostHog's context warehouse article](https://posthog.com/blog/what-is-a-context-warehouse), dated July 21, 2026, describes S3 storage and a DuckDB instance per organization in a Firecracker microVM. It also describes a PostgreSQL wire-protocol endpoint. A compatible wire protocol does not make that storage engine PostgreSQL. The integration example is relevant to a much larger organization-wide data warehouse. Fourteen skills do not require S3, a lakehouse, or DuckDB.

The claim that SQL outperforms vector retrieval is workload-dependent. None of the supplied essays contains a controlled evaluation for this skill corpus. Exact error text, versions, file paths, and component names make PostgreSQL text search a reasonable starting point. Add semantic retrieval only if held-out trouble prompts show failures that lexical retrieval cannot resolve.

The "up to 50%" CSV token saving is unverified for these records. Multiline Markdown can need substantial CSV escaping. Measure CSV against JSON with the target tokenizer. A hard byte or token budget matters more than the serialization preference.

Ingestion removes the need to call an upstream API for each query. It still faces upstream rate limits, synchronization delays, deletion handling, and source permissions. Store source revisions and ingestion time rather than describing a mirror as immune to API limits.

A read replica cannot eliminate all query risk. It can isolate ordinary writes from the primary, but expensive reads consume resources and authorized results can still disclose sensitive data. [PostgreSQL's transaction documentation](https://www.postgresql.org/docs/17/sql-set-transaction.html) also distinguishes read-only transactions from a complete ban on disk writes. Use grants, restricted functions, output limits, and cancellation together.

## The tenant-setting defect

The context-in-a-db essay suggests that `SET LOCAL app.current_tenant_id` plus a filtered view guarantees isolation for arbitrary SQL. That guarantee is false when the querying role can change the same setting. A caller can use `SET` or invoke `set_config` within a `SELECT`. Rolling back later does not undo a disclosure. PostgreSQL documents these [session-setting mechanisms](https://www.postgresql.org/docs/17/config-setting.html).

This repository chooses one trusted workspace per catalog and makes no multi-tenant isolation claim. For separate customers, use separate databases and credentials by default. If a later deployment uses shared tables, derive identity from authenticated database roles with no cross-tenant role membership, and test each relation, view, and function under the real login. Do not trust a tenant identifier supplied by the agent.

PostgreSQL [row security](https://www.postgresql.org/docs/17/ddl-rowsecurity.html) can enforce policies, but superusers and `BYPASSRLS` roles bypass them. Owners normally bypass them too. The [view documentation](https://www.postgresql.org/docs/17/sql-createview.html) explains the distinction between owner and invoker privileges. The prototype uses forced RLS for per-login working memory and keeps the workspace catalog shared. This limits the claim to database-login isolation; it does not establish an authenticated multi-tenant web service.

## Recommended model and alternatives

Use immutable source snapshots plus normalized relational projections. Preserve the original Markdown and other source bytes, then expose sections, pins, and evaluation cases through joins. Keep hashes and line locations so an answer can cite evidence rather than an uncited summary. Keep machine-extracted material distinguishable from human-verified claims.

A single JSONB document per skill would preserve flexible metadata, but it would make cross-skill pin queries, section citations, and evaluation tracking harder. A table for every possible prose category would force the importer to invent certainty. The mixed model gives stable relationships typed columns and leaves irregular source metadata in JSONB.

A vector database alone does not enforce version relationships or reproduce a skill's source. A graph database would add an operational dependency for relationships PostgreSQL can represent with foreign keys. Plain files remain the upstream authoring format. This database is a rebuildable catalog with working memory beside it.

PostgreSQL's [text-search controls](https://www.postgresql.org/docs/17/textsearch-controls.html) provide query parsing and ranking. [GIN text-search indexes](https://www.postgresql.org/docs/17/textsearch-indexes.html) support indexed lookup. Preserve literal source text beside the search representation because stemming and tokenization can obscure punctuation-heavy errors.

The delivered initial scope is a searchable catalog with SQL examples. A bounded SQL reader is the recommended next runtime. A production agent service, authenticated network API, automatic promotion of learned claims, semantic ranking, and multi-tenant isolation require separate implementation and tests. The specification names those requirements without claiming they already exist.
