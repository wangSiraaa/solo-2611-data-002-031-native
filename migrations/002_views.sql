-- Read-only views used by audit and by independent raw-SQL verification.

CREATE VIEW v_ledger_lines AS
SELECT e.entry_seq,
       e.entry_id,
       e.entry_type,
       e.entry_date,
       e.period,
       e.reverses_entry_id,
       p.line_no,
       p.account_id,
       p.currency,
       p.debit_cents,
       p.credit_cents
  FROM entries e
  JOIN postings p USING (entry_seq);

CREATE VIEW v_entry_currency_totals AS
SELECT e.entry_seq,
       e.entry_id,
       e.entry_type,
       e.entry_date,
       e.period,
       p.currency,
       sum(p.debit_cents)  AS debit_cents,
       sum(p.credit_cents) AS credit_cents,
       sum(p.debit_cents) - sum(p.credit_cents) AS diff_cents
  FROM entries e
  JOIN postings p USING (entry_seq)
 GROUP BY e.entry_seq, e.entry_id, e.entry_type, e.entry_date,
          e.period, p.currency;

CREATE VIEW v_account_raw_totals AS
SELECT p.account_id,
       p.currency,
       sum(p.debit_cents)  AS debit_cents,
       sum(p.credit_cents) AS credit_cents,
       sum(p.debit_cents) - sum(p.credit_cents) AS net_cents
  FROM postings p
 GROUP BY p.account_id, p.currency;

CREATE VIEW v_period_account_raw_totals AS
SELECT e.period,
       p.account_id,
       p.currency,
       sum(p.debit_cents)  AS debit_cents,
       sum(p.credit_cents) AS credit_cents,
       sum(p.debit_cents) - sum(p.credit_cents) AS net_cents
  FROM entries e
  JOIN postings p USING (entry_seq)
 GROUP BY e.period, p.account_id, p.currency;

CREATE VIEW v_reversal_effects AS
SELECT orig.entry_id AS original_entry_id,
       rev.entry_id  AS reversal_entry_id,
       p.account_id,
       p.currency,
       sum(p.debit_cents)  AS debit_cents,
       sum(p.credit_cents) AS credit_cents,
       sum(p.debit_cents) - sum(p.credit_cents) AS net_cents
  FROM entries rev
  JOIN entries orig ON orig.entry_id = rev.reverses_entry_id
  JOIN postings p ON p.entry_seq IN (orig.entry_seq, rev.entry_seq)
 GROUP BY orig.entry_id, rev.entry_id, p.account_id, p.currency;

-- Combined original + reversal lines, ordered so a checker can compare each
-- line position: reversal line i must be the exact debit/credit mirror of
-- original line i.
CREATE VIEW v_reversal_line_pairs AS
SELECT orig.entry_id AS original_entry_id,
       rev.entry_id  AS reversal_entry_id,
       o.line_no,
       o.account_id AS original_account_id,
       o.currency AS original_currency,
       o.debit_cents AS original_debit_cents,
       o.credit_cents AS original_credit_cents,
       r.account_id AS reversal_account_id,
       r.currency AS reversal_currency,
       r.debit_cents AS reversal_debit_cents,
       r.credit_cents AS reversal_credit_cents
  FROM entries rev
  JOIN entries orig ON orig.entry_id = rev.reverses_entry_id
  JOIN postings o ON o.entry_seq = orig.entry_seq
  LEFT JOIN postings r
         ON r.entry_seq = rev.entry_seq AND r.line_no = o.line_no;

CREATE VIEW v_closed_period_raw_counts AS
SELECT pe.period,
       pe.closed_at,
       pe.entry_count,
       pe.posting_count,
       (SELECT count(*) FROM entries e
         WHERE e.period = pe.period) AS raw_entry_count,
       (SELECT count(*) FROM entries e
          JOIN postings p USING (entry_seq)
         WHERE e.period = pe.period) AS raw_posting_count
  FROM periods pe;
