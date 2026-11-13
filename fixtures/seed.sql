CREATE SCHEMA "legacy sales";
CREATE SEQUENCE "legacy sales".order_seq START WITH 100;
CREATE TABLE "legacy sales"."Order Header" (
  id bigint PRIMARY KEY DEFAULT nextval('"legacy sales".order_seq'::regclass),
  customer text NOT NULL,
  status text DEFAULT 'legacy sales.pending'
);
ALTER SEQUENCE "legacy sales".order_seq OWNED BY "legacy sales"."Order Header".id;
CREATE TABLE "legacy sales"."Order Line" (
  id bigint PRIMARY KEY,
  order_id bigint REFERENCES "legacy sales"."Order Header"(id),
  amount numeric(12,2) NOT NULL
);
CREATE VIEW "legacy sales"."v Order" AS
SELECT h.id, h.customer, l.amount, 'legacy sales.Order Header'::text AS source_note
FROM "legacy sales"."Order Header" h JOIN "legacy sales"."Order Line" l ON l.order_id=h.id;
CREATE TABLE "legacy sales".unrelated(id integer PRIMARY KEY, note text);
INSERT INTO "legacy sales"."Order Header"(customer) VALUES ('fixture customer');
INSERT INTO "legacy sales"."Order Line" VALUES (1,100,12.50);
