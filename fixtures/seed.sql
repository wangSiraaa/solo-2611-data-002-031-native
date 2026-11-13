CREATE SCHEMA partition_input;
CREATE TABLE partition_input.events(id bigint NOT NULL, occurred_at timestamptz NOT NULL, payload text, PRIMARY KEY(id,occurred_at)) PARTITION BY RANGE(occurred_at);
CREATE TABLE partition_input.bucket_misc PARTITION OF partition_input.events DEFAULT;
CREATE TABLE partition_input.not_named_by_month PARTITION OF partition_input.events FOR VALUES FROM ('2025-12-01T00:00:00Z') TO ('2026-01-01T00:00:00Z');
INSERT INTO partition_input.events VALUES
(1,'2025-12-31T23:59:59Z','before'),(2,'2026-01-01T00:00:00Z','start'),
(3,'2026-01-20T12:00:00Z','middle'),(4,'2026-01-31T23:59:59.999999Z','last'),
(5,'2026-02-01T00:00:00Z','next'),(6,'2026-02-15T12:00:00Z','outside');
