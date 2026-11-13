# SSE upstream input fixture
Run `python3 fixtures/sse_upstream.py --port 8106` (Python 3.11 standard library).
GET `/events?case=partial&client=run-1`. Cases: normal, partial, conflict, expired, rotate.
Use a new client name per independent experiment; restart server to reset attempt counters.
IDs are `streamA:1` etc. Every event's JSON also has stream_id and sequence.
The server ends each response. A subsequent request may repeat the complete last event.
`partial` drops the first response inside event 4; `expired` returns 410 after a checkpoint;
`rotate` starts streamB on reconnect; `conflict` changes a repeated event on reconnect.
No collector, checkpoint storage, assertions, or expected result files are provided.
