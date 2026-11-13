CREATE SCHEMA IF NOT EXISTS fed_input;
CREATE TABLE fed_input.orders(id bigint PRIMARY KEY, product_id bigint, region text NOT NULL, amount numeric(12,2));
INSERT INTO fed_input.orders VALUES
(1,10,'north',18.00),(2,20,'north',30.00),(3,30,'north',9.50),
(4,NULL,'north',12.00),(5,99,'south',6.00),(6,10,'north',2.00),(7,40,'north',0.00);
