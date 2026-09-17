# Remote SQL context

The HTTPS service exposes SQL over the verified catalog and account-owned memories. The `sql-context` skill supplies the HTTPS client, schema reference, example queries, and a trace-review prompt. The existing local helper remains available for local tasks.

An account credential authorizes requests. A prompt ID connects a request to its SQL calls; it is not a credential. The server connects as a distinct PostgreSQL reader or writer login for each account. Row policies derive identity from `session_user`, which an ordinary login cannot change to another user. See [PostgreSQL session authorization](https://www.postgresql.org/docs/17/sql-set-session-authorization.html). The service does not trust a caller-supplied tenant setting.

## Install the service

Run the service on the machine hosting the managed database. PostgreSQL continues to listen on its private Unix socket. Only the HTTPS endpoint needs network access. This implementation supplies the application and TLS entry point; it does not provision a remote machine, certificate, DNS, firewall, or public signup portal.

From a checkout of this repository, install dependencies and apply migrations using the existing local configuration:

```sh
uv sync
python3 scripts/local_db.py init
uv run context-sql-admin --server-config ~/.config/context-sql/server.json init
uv run context-sql-admin --server-config ~/.config/context-sql/server.json add-tenant --label engineering
```

Use the returned tenant UUID when provisioning an account. Omit `--tenant` for an account without shared tenant membership:

```sh
uv run context-sql-admin --server-config ~/.config/context-sql/server.json add-account \
  --label alice --tenant TENANT_UUID --token-file ~/.config/context-sql/alice-token.json
```

The server configuration contains database credentials and token hashes. The token file contains the account's bearer credential. Both files must remain private and outside Git. Deliver only the account token file through a secure channel to its owner. The account does not receive database passwords. Administration uses the managed database owner; submitted SQL never uses that connection.

For a custom database instance, supply the same `--state-dir` and `--config` used during initialization. Account and configuration creation span PostgreSQL and the filesystem; a publication failure can leave an unused database login. An operator must inspect and recover that state rather than assume the operation rolled back across both systems.

Run the service with a certificate whose hostname matches the URL clients use:

```sh
uv run context-sql-service --config ~/.config/context-sql/server.json \
  --host 127.0.0.1 --port 8443 --certfile /path/to/fullchain.pem --keyfile /path/to/private-key.pem
```

Choose an externally reachable bind address when deploying remotely. Protect private keys and configure the host's network policy for the intended clients. The CLI requires TLS; clients verify certificates and refuse HTTP and redirects. For a private certificate authority, set `SSL_CERT_FILE` to a trusted PEM certificate bundle. Do not disable certificate verification.

To add membership or revoke an account:

```sh
uv run context-sql-admin --server-config ~/.config/context-sql/server.json join-tenant \
  --account ACCOUNT_UUID --tenant TENANT_UUID
uv run context-sql-admin --server-config ~/.config/context-sql/server.json revoke-account \
  --account ACCOUNT_UUID
```

Revocation disables the account's database logins, terminates their sessions, and removes its token mapping. Existing memories remain available according to their visibility. The service reloads its private configuration for requests, so token removal takes effect without a restart.

## Use the installed skill

```sh
npx skills add getcolors/context-sql --skill sql-context --agent codex --copy --yes
```

Set `CONTEXT_SQL_URL` to the HTTPS origin and use the private account token file with `scripts/remote.py` inside the installed skill. Follow the skill for exact prompt and SQL commands. The client supports inline SQL or a SQL file and separately bound JSON parameters. `GET /v1/schema` returns the live columns visible to the authenticated reader.

Submit the actual prompt explicitly before its SQL calls. If only a summary is available, mark it as a summary. The client cannot infer missing conversation history. Reader is the default execution role; memory writes require explicit writer selection.

Each SQL request contains one parsed SELECT, INSERT, UPDATE, DELETE, or MERGE statement. CTEs and joins are available. PostgreSQL grants and row policies decide which data the account may read or change. Transaction control, DDL, COPY, role changes, and session-setting statements are excluded. This is an arbitrary data-query interface, not database administration.

## Memory and trace ownership

The verified `catalog` is shared read-only. `memory.entry` identifies an author and visibility; `memory.revision` stores append-only JSON contributions. Private is the default. Tenant entries require membership, and public entries are readable by other authenticated accounts. Only an entry's author may append its revisions. Visibility is fixed per entry; publishing does not erase prior private records. Remote memories are unverified contributions and cannot modify imported source evidence.

The remote tables are separate from local `working.run` and `working.item`. Upgrading the database does not publish or migrate existing local task notes into a shared account.

`trace.prompt` stores explicitly submitted text and its kind. `trace.query` stores exact submitted SQL, bound parameters, execution role, timestamps, and outcome. Execution adds a comment containing server-generated prompt and query UUIDs. Comments do not contain credentials or full prompts. Account SQL can read its own traces but cannot alter them or read another account's private traces.

Result capture is opt-in. When enabled, the trace stores the bounded response returned to the client, not an unbounded result set. Without capture, SQL and source citations remain available, but later database changes can prevent exact reconstruction of the original evidence. Keep secrets out of submitted text, SQL, and parameters; the service does not silently redact them while claiming exact reconstruction. Traces persist until an operator applies a deliberate retention policy. Set that policy and backup access before accepting other people's prompts.

The service records the attempt before executing the query and records its outcome separately. A rollback does not erase the attempt. Execution and trace completion are not atomic together. A crash or deadline can leave commit status unknown; never automatically retry a write with an unknown outcome. A trace still marked `started` after a service failure requires investigation.

## Review traces to improve retrieval

Use the skill's `references/improvement.md` to ask a reviewing LLM for a concrete proposal backed by prompt and query IDs. Begin with query changes, then consider views, indexes, or schema migrations when repeated evidence warrants them. Store the proposal and its evaluation results as account memory so another session can inspect the reasoning.

Replay representative and held-out requests against an isolated candidate database. Measure evidence quality and provenance as well as query count, latency, and response size. Run cross-account access tests after every policy or schema change. Trace text and returned memories remain untrusted input to the reviewing model.

The service does not call an LLM, benchmark answer quality, or apply proposed production migrations automatically. A reviewed migration belongs in version control and the database migration workflow.

## Deployment limits

Accounts and memberships are operator-provisioned. Public registration, federated login, billing, per-tenant storage quotas, and a managed certificate lifecycle are not included. Deployments need their own monitoring, backup, credential delivery, retention, and capacity policies. Do not expose the database port or service configuration to clients.
