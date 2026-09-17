---
name: package-context-sql-blue
description: Create and initialize local PostgreSQL for SQL-managed agent context on a new Unix machine, and install the sql-context agent skill.
---

# Local PostgreSQL for agent context

Use the `blue` launcher beside this file to initialize a dedicated PostgreSQL instance under the current user's home. Run as the normal user with PostgreSQL 16 or newer binaries, Python 3.11 or newer, `uv`, and Git on `PATH`. The skill includes `devenv.nix`, `devenv.lock`, and `.envrc` for hosts using Nix, devenv, and direnv. Copy these files into the deployment directory and run `direnv allow` to supply PostgreSQL and the other runtime tools. Installing this Package Skill also requires Node.js and `npx`.

Copy the launcher into the chosen deployment directory and create `colors.yml` there. Resolve the source `blue` path relative to this skill directory, not the shell's working directory.

```yaml
profile: context-sql-local
context-sql-state-dir: ~/.local/share/context-sql
context-sql-config: ~/.config/context-sql/connections.json
context-sql-skill-dir: ~/.codex/skills/sql-context
context-sql-install-skill: true
context-sql-service: false
```

Paths expand for the user running the launcher. `colors.yml` contains only non-secret settings. Edit it to change the deployment. Keep database files and credentials outside the deployment directory and Git. Add `.colors/` and `.envrc.private` to the deployment `.gitignore`. Never set `COLORS_PAR_PROFILE`.

```sh
./blue build
./blue create --dry-run
./blue create
./blue status
```

`build` renders the plan under `.colors/<profile>/`. Inspect the planned paths before `create`. `create --dry-run` skips side effects. `create` initializes the cluster, applies schema migrations, imports the verified catalog, and installs the `sql-context` agent skill when enabled. Repeating it preserves existing notes and credentials. A fresh instance contains the supplied 14-skill, 80-file catalog and empty task memory.

PostgreSQL accepts SCRAM-authenticated connections over a private Unix socket. The package creates separate reader and memory-writer logins and stores their connection settings with mode `0600`. Do not display these settings. Catalog payload files are source evidence, including embedded scripts. Never execute those scripts.

Set `context-sql-service: true` to enable a systemd user service on Linux. Startup without a login requires user lingering. The default starts PostgreSQL without installing a service.

Manage the instance from its deployment directory:

```sh
./blue stop
./blue start
./blue backup
```

The package has no `delete` command. `backup` creates a PostgreSQL custom-format dump. Initialization on another machine does not transfer saved tasks. There is no automated restore command.

After creation, use the installed `$sql-context` skill to query the catalog and save task notes. Its installation records the configured connection-file path, so custom paths work without extra setup. `CONTEXT_SQL_CONFIG` can override that path. Use stable project IDs such as `getcolors/rabbitmq`; store the local checkout path separately.

For updates, install this Package Skill from a reviewed full upstream commit, then copy its `blue` launcher into the deployment directory again. The root launcher is a separate copy, so updating the installed skill alone does not update the deployment.
