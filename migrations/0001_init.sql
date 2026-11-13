-- 0001_init.sql — schema for the consistent-snapshot export demo.
-- All money is integer cents. All data is synthetic.

CREATE TABLE orders (
    order_id     integer PRIMARY KEY,
    customer_id  integer NOT NULL,
    amount_cents bigint NOT NULL CHECK (amount_cents >= 0)
);

CREATE TABLE payments (
    payment_id   integer PRIMARY KEY,
    order_id     integer NOT NULL REFERENCES orders(order_id) ON DELETE CASCADE,
    amount_cents bigint NOT NULL
);

CREATE INDEX payments_order_id_idx ON payments(order_id);

-- Single-row epoch table: enforced by the id = 1 primary key + CHECK.
-- The writer bumps `epoch` inside every business transaction.
CREATE TABLE dataset_epoch (
    id         smallint PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    epoch      bigint NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
);
