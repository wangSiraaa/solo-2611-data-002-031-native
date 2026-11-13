# Two-source inputs
Requires a local PostgreSQL server and psql; neither is supplied/preinstalled.
Load into an empty test database: `psql "$PG_DSN" -v ON_ERROR_STOP=1 -f fixtures/orders.sql`.
Run HTTP fixture: `python3 fixtures/products_api.py --port 8107 --audit products-requests.jsonl`.
GET /products supports `ids=10,20` (empty means no keys), `category=food`, `fields=id,name`.
All filters conjunct, projection happens last, no pagination. Duplicates are retained.
The log records actual request parameters and row counts. Missing ids means an unbounded scan.
queries.json declares the limited supported query language. category=null means no category predicate.
No expected joins or query engine are supplied.
