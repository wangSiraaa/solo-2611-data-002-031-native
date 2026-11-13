-- Supplier code migration schema.
--
-- Design notes:
--   * supplier `code` is compared byte-for-byte (COLLATE "C"), so case matters.
--   * `entity_id` is immutable; a trigger rejects any attempt to change it.
--   * Child FKs are DEFERRABLE INITIALLY IMMEDIATE.  The migration transaction
--     runs `SET CONSTRAINTS ALL DEFERRED`, which keeps every FK enforced (the
--     final COMMIT validates all of them) but permits the two-step code
--     rewrite.  Constraints are never dropped or disabled.
--   * The referenced UNIQUE(code) constraint cannot itself be deferrable in
--     PostgreSQL, so swaps/cycles are performed through transaction-local
--     placeholder codes; no placeholder is ever visible outside the transaction.

BEGIN;

CREATE TABLE IF NOT EXISTS suppliers (
    entity_id text PRIMARY KEY,
    code      text COLLATE "C" NOT NULL,
    name      text NOT NULL,
    CONSTRAINT suppliers_code_unique   UNIQUE (code),
    CONSTRAINT suppliers_code_nonempty CHECK (code <> '')
);

CREATE OR REPLACE FUNCTION suppliers_forbid_entity_id_change()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF NEW.entity_id IS DISTINCT FROM OLD.entity_id THEN
        RAISE EXCEPTION
            'suppliers.entity_id is immutable (entity_id=% is not entity_id=%)',
            OLD.entity_id, NEW.entity_id
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS suppliers_entity_id_immutable ON suppliers;
CREATE TRIGGER suppliers_entity_id_immutable
    BEFORE UPDATE ON suppliers
    FOR EACH ROW
    EXECUTE FUNCTION suppliers_forbid_entity_id_change();

CREATE TABLE IF NOT EXISTS purchase_orders (
    id            bigserial PRIMARY KEY,
    order_ref     text NOT NULL UNIQUE,
    supplier_code text COLLATE "C" NOT NULL,
    customer      text NOT NULL DEFAULT '',
    amount        numeric(14, 2) NOT NULL DEFAULT 0,
    created_at    timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT purchase_orders_supplier_fk
        FOREIGN KEY (supplier_code) REFERENCES suppliers (code)
        DEFERRABLE INITIALLY IMMEDIATE,
    CONSTRAINT purchase_orders_supplier_code_nonempty
        CHECK (supplier_code <> '')
);

CREATE INDEX IF NOT EXISTS purchase_orders_supplier_code_idx
    ON purchase_orders (supplier_code);

CREATE TABLE IF NOT EXISTS contacts (
    id            bigserial PRIMARY KEY,
    contact_ref   text NOT NULL UNIQUE,
    supplier_code text COLLATE "C" NOT NULL,
    full_name     text NOT NULL,
    email         text NOT NULL DEFAULT '',
    CONSTRAINT contacts_supplier_fk
        FOREIGN KEY (supplier_code) REFERENCES suppliers (code)
        DEFERRABLE INITIALLY IMMEDIATE,
    CONSTRAINT contacts_supplier_code_nonempty
        CHECK (supplier_code <> '')
);

CREATE INDEX IF NOT EXISTS contacts_supplier_code_idx
    ON contacts (supplier_code);

-- One committed row per successfully applied operation.  The row is written
-- in the same transaction as the data migration, so it either exists together
-- with the migrated state or not at all.
CREATE TABLE IF NOT EXISTS migration_operations (
    operation_id       text PRIMARY KEY,
    mapping_hash       text NOT NULL,
    normalized_mapping jsonb NOT NULL,
    receipt            jsonb NOT NULL,
    status             text NOT NULL CHECK (status IN ('committed')),
    created_at         timestamptz NOT NULL DEFAULT now()
);

COMMIT;
