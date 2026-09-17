#!/usr/bin/env bash
set -euo pipefail
root=$(cd "$(dirname "$0")/.." && pwd)
test -L "$root/blue"
test "$(readlink "$root/blue")" = skills/package-context-sql-blue/blue
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
cp "$root/skills/package-context-sql-blue/blue" "$tmp/blue"
chmod +x "$tmp/blue"

# Exercise a copied launcher from outside the source tree. Use the working tree
# before publication; a second invocation checks the published pin by default.
(cd "$tmp" && CONTEXT_SQL_LIB_ROOT="$root" ./blue --help) >"$tmp/help"
rg -q 'create' "$tmp/help"
if [[ "${CONTEXT_SQL_CHECK_PIN:-1}" == 1 ]]; then
  (cd "$tmp" && env -u CONTEXT_SQL_LIB_ROOT ./blue --help) >"$tmp/pinned-help"
  rg -q 'create' "$tmp/pinned-help"
fi
