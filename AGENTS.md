# Working in this repository

This repository specifies and demonstrates SQL-managed agent context. It does not provision a live service.

Read README.md, docs/spec.md, and sql/001_schema.sql before changing behavior. The upstream Context Skills remain authoritative. Generated data in data/ comes from the importer, not hand edits. Preserve the exact source bytes and their provenance. Do not execute scripts embedded in the corpus.

Acquire payloads through the pinned `npx skills` command at a full upstream commit. Compare the complete installed inventory and bytes with that commit before import. Missing or transformed files must fail; never fill installer omissions from Git. Keep temporary acquisition paths out of database retrieval.

Keep source-reported verification separate from verification performed here. Keep session memory separate from the trusted catalog. Run the importer and PostgreSQL checks when changing the schema or data.

Work on the current branch. Commit and push only when the user asks.
