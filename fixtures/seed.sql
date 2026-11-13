CREATE SCHEMA gql_input;
CREATE TABLE gql_input.customers(id bigint PRIMARY KEY, name text NOT NULL);
CREATE TABLE gql_input.orders(id bigint PRIMARY KEY, customer_id bigint REFERENCES gql_input.customers, created_at timestamptz NOT NULL, amount numeric(12,2) NOT NULL);
INSERT INTO gql_input.customers VALUES (1,'甲'),(2,'乙'),(3,'丙'),(4,'丁');
INSERT INTO gql_input.orders VALUES
(11,1,'2026-11-01T09:00:00Z',11.00),(12,1,'2026-11-01T09:00:00Z',12.00),(13,1,'2026-10-31T09:00:00Z',13.00),
(21,2,'2026-11-02T09:00:00Z',21.00),(22,2,'2026-10-30T09:00:00Z',22.00),
(31,3,'2026-11-03T09:00:00Z',31.00);
