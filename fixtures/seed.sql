PRAGMA foreign_keys=ON;
CREATE TABLE customers(subject_id TEXT PRIMARY KEY,name TEXT,email TEXT,phone TEXT,restricted INTEGER NOT NULL DEFAULT 0,revision INTEGER NOT NULL DEFAULT 1);
CREATE TABLE orders(order_id TEXT PRIMARY KEY,subject_id TEXT NOT NULL REFERENCES customers(subject_id),amount_cents INTEGER NOT NULL,note TEXT,restricted INTEGER NOT NULL DEFAULT 0,revision INTEGER NOT NULL DEFAULT 1);
INSERT INTO customers(subject_id,name,email,phone) VALUES
('U01','甲','a@example.invalid','10001'),('U02','乙','b@example.invalid','10002'),('U03','丙','c@example.invalid','10003');
INSERT INTO orders(order_id,subject_id,amount_cents,note) VALUES
('O11','U01',1200,'请电话联系'),('O12','U01',500,'已交付'),('O21','U02',2400,'争议处理中'),('O22','U02',800,'已完成'),('O31','U03',3500,'普通业务');
