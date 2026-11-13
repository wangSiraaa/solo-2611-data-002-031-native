#!/usr/bin/env bash
# Build a deterministic synthetic scenario through the CLI, then verify it with
# raw SQL that reads entries/postings/period_snapshots directly.  Prints a
# report suitable for docs/VERIFICATION.md.
set -euo pipefail
cd "$(dirname "$0")/.."

export LEDGER_PGHOST="${LEDGER_PGHOST:-/tmp/ledger-pg-sock}"
export LEDGER_PGPORT="${LEDGER_PGPORT:-55432}"
export LEDGER_PGDATABASE="${LEDGER_PGDATABASE:-ledger_verify}"
PY=".venv/bin/python"
PSQL="psql -h $LEDGER_PGHOST -p $LEDGER_PGPORT -d $LEDGER_PGDATABASE -v ON_ERROR_STOP=1"

psql -h "$LEDGER_PGHOST" -p "$LEDGER_PGPORT" -d postgres -Atc \
  "SELECT 1 FROM pg_database WHERE datname='$LEDGER_PGDATABASE'" | grep -q 1 \
  || createdb -h "$LEDGER_PGHOST" -p "$LEDGER_PGPORT" "$LEDGER_PGDATABASE"

$PY -m ledger reset-db --yes >/dev/null

OUT=".runtime/verify"
rm -rf "$OUT"
$PY -m ledger demo-data --period 2026-03 --out-dir "$OUT" >/dev/null
$PY -m ledger load-accounts "$OUT/accounts.csv" >/dev/null
for f in "$OUT"/entries/demo-00[1-6].json; do
  $PY -m ledger post --entry-file "$f" >/dev/null
done
# Reject: cross-currency offset that still totals equal.
$PY -m ledger post --entry-json '{"entry_id":"bad-cross","entry_date":"2026-03-28","lines":[
 {"account_id":"cash-usd","currency":"USD","debit_cents":500,"credit_cents":0},
 {"account_id":"revenue-usd","currency":"USD","debit_cents":0,"credit_cents":100},
 {"account_id":"cash-eur","currency":"EUR","debit_cents":100,"credit_cents":0},
 {"account_id":"revenue-eur","currency":"EUR","debit_cents":0,"credit_cents":500}]}' >/dev/null || true
$PY -m ledger close --period 2026-03 >/dev/null
$PY -m ledger reverse --reversal-json \
  '{"entry_id":"rev-004","reverses_entry_id":"demo-004","entry_date":"2026-04-02"}' >/dev/null

echo "=== schema/ledger counts ==="
$PSQL -c "SELECT (SELECT count(*) FROM entries) AS entries,
                 (SELECT count(*) FROM postings) AS postings,
                 (SELECT count(*) FROM periods) AS periods,
                 (SELECT count(*) FROM requests) AS requests;"

echo "=== each currency: whole-ledger debit/credit diff (must be 0) ==="
$PSQL -c "SELECT currency,
                 sum(debit_cents) AS debit,
                 sum(credit_cents) AS credit,
                 (sum(debit_cents)-sum(credit_cents)) AS diff
            FROM postings GROUP BY currency ORDER BY currency;"

echo "=== per-entry per-currency balance check (should be empty) ==="
$PSQL -c "SELECT e.entry_id, p.currency,
                 sum(p.debit_cents) AS d, sum(p.credit_cents) AS c
            FROM entries e JOIN postings p USING (entry_seq)
           GROUP BY e.entry_id, p.currency
          HAVING sum(p.debit_cents) <> sum(p.credit_cents);"

echo "=== stored balance vs raw postings (mismatches should be empty) ==="
$PSQL -c "SELECT b.account_id, b.currency, b.debit_cents, b.credit_cents,
                 r.debit_cents AS raw_debit, r.credit_cents AS raw_credit
            FROM account_balances b
            JOIN (SELECT account_id, currency,
                         sum(debit_cents) AS debit_cents,
                         sum(credit_cents) AS credit_cents
                    FROM postings GROUP BY 1,2) r USING (account_id, currency)
           WHERE (b.debit_cents, b.credit_cents)
                 <> (r.debit_cents, r.credit_cents);"

echo "=== 2026-03 snapshot vs raw aggregation (mismatches should be empty) ==="
$PSQL -c "SELECT s.account_id, s.currency,
                 s.debit_cents, s.credit_cents, s.net_cents,
                 r.debit_cents AS raw_debit, r.credit_cents AS raw_credit
            FROM period_snapshots s
            JOIN (SELECT p.account_id, p.currency,
                         sum(p.debit_cents) AS debit_cents,
                         sum(p.credit_cents) AS credit_cents
                    FROM entries e JOIN postings p USING (entry_seq)
                   WHERE e.period = '2026-03'
                   GROUP BY 1,2) r USING (account_id, currency)
           WHERE (s.debit_cents, s.credit_cents)
                 <> (r.debit_cents, r.credit_cents);"

echo "=== original + reversal net effect per account (should all be 0) ==="
$PSQL -c "SELECT p.account_id, p.currency,
                 sum(p.debit_cents) AS debit,
                 sum(p.credit_cents) AS credit,
                 (sum(p.debit_cents)-sum(p.credit_cents)) AS net
            FROM entries e
            JOIN postings p USING (entry_seq)
           WHERE e.entry_id IN ('demo-004','rev-004')
           GROUP BY 1,2 ORDER BY 1,2;"

echo "=== reversal line mirror check (mismatches should be empty) ==="
$PSQL -c "SELECT lp.original_entry_id, lp.line_no
            FROM v_reversal_line_pairs lp
           WHERE lp.original_account_id <> lp.reversal_account_id
              OR lp.original_currency <> lp.reversal_currency
              OR lp.original_debit_cents <> lp.reversal_credit_cents
              OR lp.original_credit_cents <> lp.reversal_debit_cents;"

echo "=== closed period counts vs raw counts ==="
$PSQL -c "SELECT period, entry_count, raw_entry_count,
                 posting_count, raw_posting_count
            FROM v_closed_period_raw_counts;"

echo "=== CLI audit verdict ==="
$PY -m ledger audit
