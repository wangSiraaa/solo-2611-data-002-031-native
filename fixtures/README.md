# Deterministic origins
Two terminals (Python standard library):
`python3 fixtures/upstream.py --port 8111 --mode blocked --gate release.gate --audit slow.jsonl`
`python3 fixtures/upstream.py --port 8112 --mode fast --audit fast.jsonl`
The blocked origin waits for release.gate, with a 30-second maximum. Create that file to release it.
Use a fresh nonexistent gate path per run. Every actual upstream request has a begin/end log.
The mock may keep its handler alive after a client closes; its log is evidence of upstream dispatch,
not a direct measure of the proxy's live socket ownership. Candidate diagnostics must explain that distinction.
Send a slow burst larger than its queue, interleave healthy requests, then release the gate.
No proxy, request scheduler or client-side assertions are supplied.
