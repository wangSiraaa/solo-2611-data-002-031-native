-- Synthetic ledger schema, version 001.
-- The migration runner wraps each file in a transaction and records it in
-- schema_migrations (created by the runner, not by this file).

CREATE TABLE accounts (
    account_id   text        PRIMARY KEY,
    currency     text        NOT NULL,
    created_at   timestamptz NOT NULL DEFAULT clock_timestamp(),
    CONSTRAINT accounts_id_nonempty CHECK (length(account_id) BETWEEN 1 AND 200),
    CONSTRAINT accounts_id_printable CHECK (account_id !~ '[[:cntrl:]]'),
    CONSTRAINT accounts_currency_valid CHECK (currency ~ '^[A-Z][A-Z0-9._-]{0,31}$')
);
CREATE UNIQUE INDEX accounts_account_currency_uidx
    ON accounts (account_id, currency);

-- Zero-initialized cumulative balance, maintained in the same transaction
-- as every posting.  It is redundant with postings by design: audit compares
-- both representations.
CREATE TABLE account_balances (
    account_id   text        NOT NULL,
    currency     text        NOT NULL,
    debit_cents  bigint      NOT NULL DEFAULT 0,
    credit_cents bigint      NOT NULL DEFAULT 0,
    PRIMARY KEY (account_id, currency),
    CONSTRAINT account_balances_debit_nonnegative CHECK (debit_cents >= 0),
    CONSTRAINT account_balances_credit_nonnegative CHECK (credit_cents >= 0),
    CONSTRAINT account_balances_account_fk
        FOREIGN KEY (account_id, currency)
        REFERENCES accounts (account_id, currency)
);

-- A period is created exactly once, by close, and is append-only.
CREATE TABLE periods (
    period             text        PRIMARY KEY,
    snapshot_id        bigint      NOT NULL UNIQUE,
    snapshot_hash      text        NOT NULL,
    entry_count        integer     NOT NULL,
    posting_count      integer     NOT NULL,
    snapshot           jsonb       NOT NULL,
    request_sha256     text        NOT NULL UNIQUE,
    request_json       jsonb       NOT NULL,
    result_json        jsonb       NOT NULL,
    closed_at          timestamptz NOT NULL,
    CONSTRAINT periods_period_valid CHECK (period ~ '^[0-9]{4}-(0[1-9]|1[0-2])$'),
    CONSTRAINT periods_counts_nonnegative
        CHECK (entry_count >= 0 AND posting_count >= 0)
);

CREATE SEQUENCE period_snapshot_id_seq AS bigint START WITH 1;

-- Per-account, per-currency closed-period snapshot.  Rows are derived only
-- from postings whose entry_date is in the period.
CREATE TABLE period_snapshots (
    period       text   NOT NULL REFERENCES periods (period),
    account_id   text   NOT NULL,
    currency     text   NOT NULL,
    debit_cents  bigint NOT NULL,
    credit_cents bigint NOT NULL,
    net_cents    bigint NOT NULL,
    PRIMARY KEY (period, account_id, currency),
    CONSTRAINT period_snapshots_debit_nonnegative CHECK (debit_cents >= 0),
    CONSTRAINT period_snapshots_credit_nonnegative CHECK (credit_cents >= 0),
    CONSTRAINT period_snapshots_net_matches CHECK (net_cents = debit_cents - credit_cents),
    CONSTRAINT period_snapshots_account_fk
        FOREIGN KEY (account_id, currency)
        REFERENCES accounts (account_id, currency)
);

CREATE TABLE entries (
    entry_seq          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    entry_id           text        NOT NULL UNIQUE,
    entry_type         text        NOT NULL,
    entry_date         date        NOT NULL,
    period             text        NOT NULL,
    canonical_content  text        NOT NULL,
    content_hash       text        NOT NULL,
    request_sha256     text        NOT NULL UNIQUE,
    request_json       jsonb       NOT NULL,
    result_json        jsonb       NOT NULL,
    reverses_entry_id  text,
    created_at         timestamptz NOT NULL DEFAULT clock_timestamp(),
    CONSTRAINT entries_id_valid
        CHECK (length(entry_id) BETWEEN 1 AND 200
               AND entry_id !~ '[[:cntrl:]]'),
    CONSTRAINT entries_type_valid CHECK (entry_type IN ('journal', 'reversal')),
    CONSTRAINT entries_period_matches_date
        CHECK (period = to_char(entry_date, 'YYYY-MM')),
    CONSTRAINT entries_period_valid
        CHECK (period ~ '^[0-9]{4}-(0[1-9]|1[0-2])$'),
    CONSTRAINT entries_kind_relation CHECK (
        (entry_type = 'journal'  AND reverses_entry_id IS NULL) OR
        (entry_type = 'reversal' AND reverses_entry_id IS NOT NULL)
    ),
    CONSTRAINT entries_not_self_reversal
        CHECK (reverses_entry_id IS NULL OR reverses_entry_id <> entry_id),
    CONSTRAINT entries_reverses_fk
        FOREIGN KEY (reverses_entry_id)
        REFERENCES entries (entry_id)
        DEFERRABLE INITIALLY IMMEDIATE
);
CREATE UNIQUE INDEX entries_one_reversal_per_original_uidx
    ON entries (reverses_entry_id)
    WHERE reverses_entry_id IS NOT NULL;
CREATE INDEX entries_period_idx ON entries (period);

CREATE TABLE postings (
    entry_seq    bigint NOT NULL REFERENCES entries (entry_seq),
    line_no      integer NOT NULL,
    account_id   text   NOT NULL,
    currency     text   NOT NULL,
    debit_cents  bigint NOT NULL,
    credit_cents bigint NOT NULL,
    PRIMARY KEY (entry_seq, line_no),
    CONSTRAINT postings_line_no_positive CHECK (line_no > 0),
    CONSTRAINT postings_debit_nonnegative CHECK (debit_cents >= 0),
    CONSTRAINT postings_credit_nonnegative CHECK (credit_cents >= 0),
    CONSTRAINT postings_exactly_one_side_positive CHECK (
        debit_cents > 0 AND credit_cents = 0 OR
        credit_cents > 0 AND debit_cents = 0
    ),
    -- This composite foreign key both proves the account exists and proves
    -- that the declared line currency is the account's fixed currency.
    CONSTRAINT postings_account_currency_fk
        FOREIGN KEY (account_id, currency)
        REFERENCES accounts (account_id, currency)
);
CREATE INDEX postings_account_idx ON postings (account_id, currency);
CREATE INDEX postings_entry_idx ON postings (entry_seq);

-- Every successful request is recorded here in the same transaction as its
-- effects.  This table is the idempotency and request-audit source of truth.
CREATE TABLE requests (
    request_sha256 text        PRIMARY KEY,
    operation      text        NOT NULL,
    target_id      text        NOT NULL,
    request_json   jsonb       NOT NULL,
    response_json  jsonb       NOT NULL,
    entry_seq      bigint,
    closed_period  text,
    created_at     timestamptz NOT NULL DEFAULT clock_timestamp(),
    CONSTRAINT requests_sha_format CHECK (request_sha256 ~ '^[0-9a-f]{64}$'),
    CONSTRAINT requests_operation_valid
        CHECK (operation IN ('post', 'reverse', 'close')),
    CONSTRAINT requests_entry_fk
        FOREIGN KEY (entry_seq) REFERENCES entries (entry_seq),
    CONSTRAINT requests_period_fk
        FOREIGN KEY (closed_period) REFERENCES periods (period),
    CONSTRAINT requests_effect_shape CHECK (
        (operation IN ('post', 'reverse')
            AND entry_seq IS NOT NULL AND closed_period IS NULL) OR
        (operation = 'close'
            AND entry_seq IS NULL AND closed_period IS NOT NULL)
    )
);

-- ---------------------------------------------------------------------------
-- Append-only enforcement.  These triggers are a database-level backstop;
-- the application also never issues UPDATE/DELETE against committed ledger
-- facts.
-- ---------------------------------------------------------------------------
CREATE FUNCTION ledger_forbid_mutation() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'ledger row is append-only: % %', TG_TABLE_NAME, TG_OP
        USING ERRCODE = 'restrict_violation';
END $$;

CREATE TRIGGER accounts_immutable
    BEFORE UPDATE OR DELETE ON accounts
    FOR EACH ROW EXECUTE FUNCTION ledger_forbid_mutation();
CREATE TRIGGER account_balances_no_delete
    BEFORE DELETE ON account_balances
    FOR EACH ROW EXECUTE FUNCTION ledger_forbid_mutation();
CREATE TRIGGER entries_immutable
    BEFORE UPDATE OR DELETE ON entries
    FOR EACH ROW EXECUTE FUNCTION ledger_forbid_mutation();
CREATE TRIGGER postings_immutable
    BEFORE UPDATE OR DELETE ON postings
    FOR EACH ROW EXECUTE FUNCTION ledger_forbid_mutation();
CREATE TRIGGER periods_immutable
    BEFORE UPDATE OR DELETE ON periods
    FOR EACH ROW EXECUTE FUNCTION ledger_forbid_mutation();
CREATE TRIGGER period_snapshots_immutable
    BEFORE UPDATE OR DELETE ON period_snapshots
    FOR EACH ROW EXECUTE FUNCTION ledger_forbid_mutation();
CREATE TRIGGER requests_immutable
    BEFORE UPDATE OR DELETE ON requests
    FOR EACH ROW EXECUTE FUNCTION ledger_forbid_mutation();

-- A new posting may not fall into an already closed period.  The application
-- additionally holds the global writer lock while checking periods, so the
-- check and insert are indivisible for all CLI writes.
CREATE FUNCTION ledger_assert_period_open() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF EXISTS (SELECT 1 FROM periods WHERE period = NEW.period) THEN
        RAISE EXCEPTION 'period % is closed', NEW.period
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END $$;

CREATE TRIGGER entries_period_open
    BEFORE INSERT ON entries
    FOR EACH ROW EXECUTE FUNCTION ledger_assert_period_open();

-- Deferred, database-level balance backstop.  A journal entry with multiple
-- currencies must balance separately in every currency.
CREATE FUNCTION ledger_assert_entry_balanced() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    bad_currency text;
BEGIN
    SELECT p.currency INTO bad_currency
      FROM postings p
     WHERE p.entry_seq = NEW.entry_seq
     GROUP BY p.currency
    HAVING sum(p.debit_cents) <> sum(p.credit_cents)
     LIMIT 1;

    IF bad_currency IS NOT NULL THEN
        RAISE EXCEPTION 'entry % is unbalanced in currency %',
            NEW.entry_seq, bad_currency
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NULL;
END $$;

CREATE CONSTRAINT TRIGGER postings_currency_balance
    AFTER INSERT ON postings
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION ledger_assert_entry_balanced();

-- An entry must have at least one posting.
CREATE FUNCTION ledger_assert_entry_nonempty() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM postings WHERE entry_seq = NEW.entry_seq
    ) THEN
        RAISE EXCEPTION 'entry % has no postings', NEW.entry_id
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NULL;
END $$;

CREATE CONSTRAINT TRIGGER entries_nonempty
    AFTER INSERT ON entries
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION ledger_assert_entry_nonempty();

COMMENT ON TABLE entries IS
    'Immutable journal and reversal headers. entry_id is globally unique.';
COMMENT ON TABLE postings IS
    'Immutable entry lines; amount is integer minor units (cents).';
COMMENT ON TABLE requests IS
    'One committed request/response record per successful write operation.';
COMMENT ON TABLE periods IS
    'Immutable accounting-period close records; one snapshot per period.';
