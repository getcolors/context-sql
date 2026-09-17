#!/usr/bin/env bash
# Disposable Unix-socket-only PostgreSQL cluster. Never touches an existing DB.
set -euo pipefail
cd "$(dirname "$0")/.."
for tool in python3 initdb pg_ctl createdb psql uv; do
  command -v "$tool" >/dev/null || { echo "Missing tool: $tool" >&2; exit 1; }
done
uv run python -m unittest discover -s tests -p 'test_*.py' -v
context_test_dir=$(mktemp -d /tmp/context-sql-check.XXXXXX)
cleanup() {
  pg_ctl -D "$context_test_dir/pg" -m immediate -w stop >/dev/null 2>&1 || true
  rm -rf "$context_test_dir"
}
trap cleanup EXIT
initdb -D "$context_test_dir/pg" -A trust --no-locale -E UTF8 >"$context_test_dir/init.log"
pg_ctl -D "$context_test_dir/pg" -l "$context_test_dir/server.log" \
  -o "-k $context_test_dir -p 55439 -h ''" -w start >/dev/null
export PGHOST="$context_test_dir" PGPORT=55439 PGDATABASE=context_sql_test
unset PGUSER PGPASSWORD PGSERVICE PGSERVICEFILE PGOPTIONS PGHOSTADDR
createdb "$PGDATABASE"
psql -X -v ON_ERROR_STOP=1 -f sql/001_schema.sql >/dev/null
psql -X -v ON_ERROR_STOP=1 -f sql/002_roles.sql >/dev/null
psql -X -v ON_ERROR_STOP=1 -f sql/003_acquisition.sql >/dev/null
psql -X -v ON_ERROR_STOP=1 -f sql/004_context_runtime.sql >/dev/null
psql -X -v ON_ERROR_STOP=1 -f sql/005_portable_projects.sql >/dev/null
psql -X -v ON_ERROR_STOP=1 -f sql/006_ingestion.sql >/dev/null
psql -X -v ON_ERROR_STOP=1 -f data/skills.sql >/dev/null
# Re-import must succeed without mutating historical rows.
psql -X -v ON_ERROR_STOP=1 -f data/skills.sql >/dev/null
psql -X -v ON_ERROR_STOP=1 -f tests/integrity.sql
psql -X -v ON_ERROR_STOP=1 -f tests/access.sql
python3 tests/database_roundtrip.py
python3 tests/portable_upgrade.py
uv run --with 'psycopg[binary]==3.2.10' python tests/context_runtime.py
# Independent connections exercise session_user without inheriting the owner session.
context_visible=$(psql -X -U context_test_bob -Atc 'SELECT count(*) FROM working.item')
[[ "$context_visible" == 0 ]] || { echo 'Cross-login read leaked rows' >&2; exit 1; }
context_owner=$(psql -X -U context_test_alice -Atc 'SELECT count(*) FROM working.item')
[[ "$context_owner" == 1 ]] || { echo 'Owner cannot read its note' >&2; exit 1; }
context_acquisitions=$(psql -X -U context_test_reader -Atc 'SELECT count(*) FROM catalog.acquisition a JOIN catalog.skill_acquisition s USING (acquisition_id)')
[[ "$context_acquisitions" == 14 ]] || { echo 'Reader cannot inspect acquisition provenance' >&2; exit 1; }
if psql -X -U context_test_reader -v ON_ERROR_STOP=1 -c "UPDATE catalog.skill SET kind='generic'" >"$context_test_dir/denied.txt" 2>&1; then
  echo 'Reader unexpectedly modified catalog' >&2; exit 1
fi
if psql -X -U context_test_reader -v ON_ERROR_STOP=1 -c 'SET ROLE context_test_alice' >"$context_test_dir/escalation.txt" 2>&1; then
  echo 'Reader unexpectedly assumed another login' >&2; exit 1
fi
psql -X -v ON_ERROR_STOP=1 -v symptom='NOAUTH' -v run_id='11111111-1111-1111-1111-111111111111' -f sql/queries.sql >"$context_test_dir/examples.txt"
printf 'PostgreSQL schema, repeat import, integrity, access, and example-query checks passed.\n'
