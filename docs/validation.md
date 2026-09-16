# Validation record

Run date: 2026-09-16. Database: PostgreSQL 18.6. Source snapshot: `getcolors/skills@3c82f5c9fc1400f748988e8295ab3af7cf5994d5`.

## Checks that passed

- Nine Python tests cover the corpus inventory, source hashes and reconstruction, line slices, eval preservation, fenced headings, SQL serialization, committed revision changes, ignored worktree changes, malformed frontmatter, symlink rejection, and unrelated repository rejection.
- `scripts/import_skills.py --check` reproduced both committed data files from the pinned source commit.
- `scripts/check.sh` created a disposable PostgreSQL cluster, applied the schema and grants, and imported the seed twice. Integrity assertions and all SQL examples passed.
- Catalog updates failed under the reader role. A reader could not assume another login. Separate login connections saw only their own working notes. A spoofed application tenant setting did not change that policy.
- Chromium rendered the page at desktop and mobile widths without JavaScript errors or horizontal page overflow. Query tabs, skill filtering, empty search results, and expandable descriptions worked.
- All four SQL examples displayed by the page executed against PostgreSQL with the captured corpus.
- A public HTTPS request through the temporary cloudflared tunnel returned the same page bytes as the reviewed local artifact.

## What these results do not establish

The 110 eval cases are imported fixtures, not successful model runs. No comparison against file search or embeddings was performed. The original infrastructure builds were not repeated. The role tests used a local Unix socket and trust authentication; they did not test network authentication or a production connection pool.

There is no deployed query gateway. Output limits, query parsing, cancellation, query auditing, and production authentication are specified but require implementation and testing before exposing a SQL tool. The public preview serves static HTML only.
