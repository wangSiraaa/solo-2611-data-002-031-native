# Limited GraphQL inputs
Prepare PostgreSQL and install any chosen GraphQL/database packages yourself.
`psql "$PG_DSN" -v ON_ERROR_STOP=1 -f fixtures/seed.sql`
schema.graphql is the requested fixed schema; queries.graphql includes aliases and different first values.
Run between_requests.sql between two completed requests, not concurrently inside a request.
No resolvers, DataLoader, batching SQL or expected result are provided.
