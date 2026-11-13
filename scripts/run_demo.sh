#!/usr/bin/env bash
# Full reproduction of the migration under concurrent writes:
#   reset -> plan -> start supplied writer -> apply mid-stream -> verify.
# Artifacts (plan/apply reports, writer log) land in .runtime/.
set -euo pipefail
cd "$(dirname "$0")/.."
: "${PG_DSN:?set PG_DSN first, e.g. export PG_DSN=\"host=/tmp/pgsock_$(id -un) dbname=taskdb\"}"
PY=.venv/bin/python
mkdir -p .runtime

scripts/reset_db.sh

echo "=== 1. plan (read-only; catalog-derived bounds, estimate, DDL) ==="
$PY -m partition_mover plan --config fixtures/target.json --dsn "$PG_DSN" | tee .runtime/plan.json

echo "=== 2. start the supplied concurrent writer (40 rows, Jan+Feb) ==="
$PY fixtures/writer.py --dsn "$PG_DSN" --count 40 --interval 0.05 > .runtime/writer.jsonl &
WRITER_PID=$!
sleep 0.4

echo "=== 3. apply the migration while the writer is running ==="
$PY -m partition_mover apply --config fixtures/target.json --dsn "$PG_DSN" | tee .runtime/apply.json
wait $WRITER_PID

echo "=== 4. writer outcomes (all committed; nothing silently lost) ==="
$PY - <<'EOF'
import json, collections
rows = [json.loads(l) for l in open(".runtime/writer.jsonl")]
print(dict(collections.Counter(r["status"] for r in rows)))
EOF

echo "=== 5. final physical placement (tableoid) ==="
psql "$PG_DSN" -c "SET TIME ZONE 'UTC'; SELECT tableoid::regclass AS partition, count(*), min(occurred_at), max(occurred_at) FROM partition_input.events GROUP BY 1 ORDER BY 1;"
psql "$PG_DSN" -c "SET TIME ZONE 'UTC'; SELECT id, occurred_at, tableoid::regclass AS partition FROM partition_input.events WHERE id IN (1,2,4,5) ORDER BY id;"

echo "=== 6. rerun apply: must be a no-op ==="
$PY -m partition_mover apply --config fixtures/target.json --dsn "$PG_DSN" 2>/dev/null | $PY -c "import json,sys; d=json.load(sys.stdin); print('status:', d['status'], '| verification ok:', d['verification']['ok'])"
