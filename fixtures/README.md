# Blind annotation inputs
items.jsonl contains immutable text and stable item IDs. No labels/answers are preassigned.
labels.json defines a fixed multi-label vocabulary; empty label sets are valid submissions.
identities.json supplies three synthetic local identities and demo tokens; no account platform is needed.
The candidate chooses its local HTTP API and persistent storage. Token role checks must run on the server.
No server, arbitration implementation or independent annotation oracle is supplied.
