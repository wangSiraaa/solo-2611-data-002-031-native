# Single-month partition input
Prepare PostgreSQL and install `psycopg[binary]` for the optional supplied writer.
Load seed into an empty test database: `psql "$PG_DSN" -v ON_ERROR_STOP=1 -f fixtures/seed.sql`.
`python3 fixtures/writer.py --dsn "$PG_DSN" > writer.jsonl` sends bounded real INSERTs.
For a repeated writer run choose a fresh --first-id. A failed write is logged, not silently retried.
Existing partition names deliberately do not encode their actual bounds.
No relocation, migration transaction or expected row locations are supplied.
