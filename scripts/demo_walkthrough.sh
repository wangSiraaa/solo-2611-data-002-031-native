#!/usr/bin/env bash
# Full local walkthrough: schema -> synthetic data -> plan -> apply -> verify
# -> fault injection & retry -> export receipt.  Prints JSON artifacts.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="${PYTHON:-$ROOT/.venv/bin/python}"
DSN="${SUPPLIER_DB_DSN:-dbname=supplier_migrate host=/tmp/supplier-pg-sock}"
CLI=("$PY" -m supplier_migrate.cli --dsn "$DSN")
OUT="${OUT:-/tmp/supplier-migrate-demo}"

mkdir -p "$OUT"

echo "== init-db =="
"${CLI[@]}" init-db >/dev/null

echo "== seed-demo (reset) =="
"${CLI[@]}" seed-demo --reset >"$OUT/seed.json"
cat "$OUT/seed.json"

echo "== plan: two-code swap =="
"${CLI[@]}" plan --mapping "$ROOT/examples/swap.csv" >"$OUT/plan-swap.json"
"$PY" -c "import json;d=json.load(open('$OUT/plan-swap.json'));print(json.dumps({k:d[k] for k in ('mapping_hash','affected_rows','entry_counts')},indent=2))"

echo "== apply: two-code swap =="
"${CLI[@]}" apply --mapping "$ROOT/examples/swap.csv" --operation-id demo-swap >"$OUT/receipt-swap.json"

echo "== replay same operation-id =="
"${CLI[@]}" apply --mapping "$ROOT/examples/swap.csv" --operation-id demo-swap \
  >"$OUT/receipt-swap-replay.json"
"$PY" -c "import json;d=json.load(open('$OUT/receipt-swap-replay.json'));print('replayed=',d['replayed'],'hash=',d['mapping_hash'])"

echo "== verify =="
"${CLI[@]}" verify --operation-id demo-swap >"$OUT/verify-swap.json"
"$PY" -c "import json;d=json.load(open('$OUT/verify-swap.json'));print('status=',d['status'],'entities=',d['entities_checked'],'mismatches=',d['mismatch_count'])"

echo "== export receipt + verification =="
"${CLI[@]}" export-receipt --operation-id demo-swap --output "$OUT/bundle-swap.json" >/dev/null
echo "wrote $OUT/bundle-swap.json"

echo "== fault injection: after_suppliers (rollback) then retry =="
"${CLI[@]}" seed-demo --reset >/dev/null
"${CLI[@]}" apply --mapping "$ROOT/examples/cycle3.csv" --operation-id demo-cycle \
  --fault-at after_suppliers >"$OUT/fault-after-suppliers.json" || true
cat "$OUT/fault-after-suppliers.json"
"${CLI[@]}" apply --mapping "$ROOT/examples/cycle3.csv" --operation-id demo-cycle \
  >"$OUT/receipt-cycle.json"
"${CLI[@]}" verify --operation-id demo-cycle >"$OUT/verify-cycle.json"

echo "== three-node cycle result =="
psql "$DSN" -c \
  "SELECT entity_id, code, name FROM suppliers ORDER BY entity_id;"

echo "== exports in $OUT =="
ls -1 "$OUT"
