# Restricted catalog export source
Prepare PostgreSQL and a driver. Load seed.sql in a fresh database with psql ON_ERROR_STOP.
request.json chooses a root view and target schema; target must not already exist.
The source includes quoted identifiers, a regclass default, sequence ownership, FK and ordinary literals.
An unrelated table and row data are present to detect accidental over-export. Root dependencies are the task.
Only the documented object subset is required; report unsupported dependencies instead of dropping them.
No target DDL, dependency traversal, schema-remapping code or expected export is supplied.
