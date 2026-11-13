#!/usr/bin/env bash
# demo.sh — full local demonstration of the consistent snapshot export tool.
# All data is synthetic. Safe to re-run: wipes exports/ and rebuilds the DB.
set -euo pipefail
cd "$(dirname "$0")"

PY=.venv/bin/python
SNAPX="$PY -m snapx"
export SNAPX_PGHOST=127.0.0.1 SNAPX_PGPORT=54329 SNAPX_PGDB=snapx SNAPX_PGUSER=postgres

step() { printf '\n\033[1m== %s ==\033[0m\n' "$*"; }

step "0. reset: stop old server, fresh DB cluster, fresh exports"
$SNAPX dbstop >/dev/null 2>&1 || true
rm -rf pgsql-data exports
$SNAPX dbstart

step "1. generate: synthetic dataset (240 orders, edge cases: zero-amount-no-payment, 5-row payments)"
$SNAPX generate --orders 240 --seed 42
psql -h 127.0.0.1 -p 54329 -U postgres -d snapx -tAc \
  "SELECT 'invariant violations: '||count(*) FROM orders o WHERE o.amount_cents <> COALESCE((SELECT sum(amount_cents) FROM payments p WHERE p.order_id=o.order_id),0)"

step "2. barrier demo: some workers read BEFORE the writer commits, others AFTER — same snapshot"
$SNAPX export --export-id demo-barrier --shards 4 --barrier-demo
$SNAPX verify --export-id demo-barrier

step "3. worker crash mid-export -> shard retried on the SAME snapshot"
SNAPX_CRASH_SHARDS=2 $SNAPX export --export-id demo-retry --shards 4
$SNAPX verify --export-id demo-retry | tail -2

step "4. idempotent retry returns the original publication; changed shard params are refused"
$SNAPX export --export-id demo-retry --shards 4
if $SNAPX export --export-id demo-retry --shards 8; then
  echo "ERROR: param change was accepted"; exit 1
else
  echo "refused as expected (rc=$?)"
fi

step "5. coordinator killed mid-export -> attempt abandoned, fresh attempt with NEW snapshot"
$SNAPX export --export-id demo-kill --shards 4 --pause-after-acquire 30 &
EXPID=$!
for i in $(seq 1 200); do
  n=$(ls exports/_attempts/demo-kill/attempt-1/control/barrier/*.acquired 2>/dev/null | wc -l) || true
  [ "$n" -ge 4 ] && break; sleep 0.1
done
kill -9 $EXPID || true
wait $EXPID 2>/dev/null || true
echo "coordinator killed; attempt 1 can never be published"
OLD_SID=$($PY -c "import json;print(json.load(open('exports/_attempts/demo-kill/attempt-1/ATTEMPT.json'))['snapshot_id'])")
echo "stale snapshot $OLD_SID — a late worker trying to import it:"
if $SNAPX _worker --snapshot "$OLD_SID" --shard 0 --shards 4 --out /tmp/stale-out --control /tmp/stale-ctl --group A; then
  echo "ERROR: stale snapshot import succeeded"; exit 1
else
  echo "stale snapshot rejected as expected (rc=$?)"
fi
$SNAPX writer --once --seed 99   # epoch moves on; old snapshot is dead
$SNAPX export --export-id demo-kill --shards 4   # abandons attempt 1, publishes attempt 2
$SNAPX inspect --export-id demo-kill
$SNAPX verify --export-id demo-kill | tail -2

step "6. failed attempt keeps identity+reason and cannot masquerade as complete"
if SNAPX_CRASH_SHARDS=1 $SNAPX export --export-id demo-fail --shards 4 --max-shard-retries 1; then
  echo "ERROR: expected export to fail"; exit 1
fi
$SNAPX inspect --export-id demo-fail
test ! -e exports/demo-fail && echo "no published dir for demo-fail: OK"
test ! -e exports/_attempts/demo-fail/attempt-1/payload/manifest.json && echo "no manifest in failed attempt: OK"

step "7. empty shards + zero-amount orders without payments + multi-row payments"
$SNAPX generate --orders 6 --seed 7
$SNAPX export --export-id demo-empty --shards 8
$SNAPX verify --export-id demo-empty
echo "shard 0 orders file lines: $(wc -l < exports/demo-empty/shards/orders-0.jsonl) (empty shard)"

step "8. writer loop commits 40 txns while an export is running (no locks taken)"
$SNAPX generate --orders 240 --seed 42 >/dev/null
$SNAPX writer --loop --interval 0.05 --count 40 --seed 555 &
WPID=$!
sleep 0.3
$SNAPX export --export-id demo-concurrent --shards 4
wait $WPID
psql -h 127.0.0.1 -p 54329 -U postgres -d snapx -tAc \
  "SELECT 'live invariant violations after 40 writer txns: '||count(*) FROM orders o WHERE o.amount_cents <> COALESCE((SELECT sum(amount_cents) FROM payments p WHERE p.order_id=o.order_id),0)"
$SNAPX verify --export-id demo-concurrent | tail -2

step "9. inspect everything"
$SNAPX inspect

step "DEMO COMPLETE — all scenarios passed"
