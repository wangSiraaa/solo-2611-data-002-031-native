# Demo data flow

`tools/demo.py` runs the whole sequence against a fresh database, restarting
the server once, and asserts each visibility/immutability/consistency point.
Run it with `python3.11 tools/demo.py`.

The timing-sensitive parts below were also captured by hand with curl against
`python3.11 -m annotation_service --db .runtime/docsdb --port 8899`.

## 0. Start

```sh
python3.11 -m annotation_service --db .runtime/annotation.db --port 8765
```

Fixtures are loaded once. The DB is pinned to SHA-256 hashes of
`items.jsonl`, `labels.json`, `identities.json`; a restart with changed files
is refused (the service intentionally has no vocabulary migration).

## 1. Blind phase — ann_b submits, ann_a must not see it

`ann_b` submits T03 (`service`, `product`) while `ann_a` has submitted nothing.

Every read path used by `ann_a`:

```sh
A='Authorization: Bearer fixture-a'
curl -s -H "$A" localhost:8765/api/items          # list
curl -s -H "$A" localhost:8765/api/items/T03      # detail
curl -s -H "$A" localhost:8765/api/export         # export (empty)
curl -s -o /dev/null -w '%{http_code}\n' -X POST -H "$A" \
  -H 'Content-Type: application/json' \
  localhost:8765/api/items/T03/submission -d '{"labels":["bogus"]}'   # 400, no state
```

Actual detail response:

```json
{"item": {"id": "T03", "text": "用了两天还不错，希望电池能耐用。"},
 "own_submission": null,
 "status": "not_submitted",
 "viewer": {"id": "ann_a", "role": "annotator"},
 "visibility": "blind_until_own_submission"}
```

There is no `peer_submitted`, `peer_submission` or `final` key, and the list
shows `not_submitted` for every item even though a peer has submitted. The
demo asserts no label id string appears anywhere in the serialized payload.

Arbiter-side reads for the same item:

| call | result |
| --- | --- |
| `GET /api/items` | T03 absent |
| `GET /api/arbitration/queue` | empty |
| `GET /api/items/T03` | `409 not_ready`, error body only |
| `POST /api/items/T03/arbitration` | `409 not_ready` — cannot be used to peek |

## 2. Agreement — T01

```sh
curl -s -X POST localhost:8765/api/items/T01/submission -H 'Authorization: Bearer fixture-a' \
  -H 'Content-Type: application/json' -d '{"labels":["product","delivery"]}'
# -> 201, status "waiting_for_peer", final null

curl -s -X POST localhost:8765/api/items/T01/submission -H 'Authorization: Bearer fixture-b' \
  -H 'Content-Type: application/json' -d '{"labels":["delivery","product"]}'
# -> 201, status "agreed"
```

The second response is the first point at which any peer data is visible
(`peer_submission`, `final`). The final is:

```json
{"final_id": "agreement:1", "status": "agreed", "source": "agreement",
 "labels": ["delivery", "product"], "arbiter_id": null,
 "binding": {"item_id": "T01",
   "submissions": [{"submission_id": 1, "author_id": "ann_a"},
                   {"submission_id": 2, "author_id": "ann_b"}]}}
```

Note the label order differs between the two requests; equality is set-based.

Exact repeat of `ann_a`'s submission:

```sh
curl -s -X POST ... /api/items/T01/submission -d '{"labels":["delivery","product"]}'
# -> 200 {"replayed": true, "own_submission": {"submission_id": 1, ...}}
```

Different labels: `409 submission_immutable`, no state change.

## 3. Dispute and arbitration — T02

`ann_a` submits `["product"]`, `ann_b` submits `["service"]`.

* Both see `status: "disputed"`, `final: null`, and each other's labels (both
  have now committed).
* Export contains nothing for T02 (`only_final` guarantee).
* Arbiter list/queue now include T02 with both submissions.

```sh
curl -s -X POST localhost:8765/api/items/T02/arbitration \
  -H 'Authorization: Bearer fixture-arbiter' -H 'Content-Type: application/json' \
  -d '{"labels":["product","service"],
       "expected_submissions":{"ann_a":5,"ann_b":6}}'
```

* Wrong `expected_submissions` → `409 stale_binding` (no write).
* Matching → `201`; `final.source = "arbitration"`, `arbiter_id = "arbiter"`,
  and `binding` is the exact `(submission_id, author_id)` pair.
* Identical replay → `200 replayed: true`; different labels → `409
  arbitration_immutable`.
* While unresolved (disputed), the item is absent from export for all roles.

## 4. Concurrency

* Both annotators POST T04 at the same instant (thread barrier): both `201`,
  exactly one `submissions` row per author and exactly one `matches` row;
  the final is still a single agreement.
* Six concurrent identical submissions by `ann_a`: one `201`, five `200`
  replays, one stored row.
* Two conflicting same-author submissions: one `201`, one `409`, one row.
* Eight concurrent arbitrations for T06, half with one label set and half with
  another: exactly one `201`; the rest are replays (`200`, identical outcome)
  or `409 arbitration_immutable`. The DB has exactly one row for T06 and the
  export shows that single final.

These guarantees come from `BEGIN IMMEDIATE` on every write plus
`UNIQUE(item_id)` on `matches`/`arbitrations` and `UNIQUE(item_id, author_id)`
on `submissions`; losers re-read committed state and return replay/conflict.

## 5. Restart

The demo terminates the server process and starts a new one on the same DB.

On start the store re-checks the fixture hashes and audits the chain:

* each submission is canonical, uses known labels, by an assigned annotator;
* each agreement final binds two ordered submissions from the same item whose
  labels equal the final, authored by both assigned annotators;
* each arbitration binds two ordered, real, *disagreeing* submissions and was
  made by an arbiter after them;
* no item has more than one final record (SQL also enforces this).

After restart the exported items are byte-identical, identical resubmissions
still replay, and conflicting submissions/arbitrations are still refused.

## Expected final export (demo)

T01, T02, T03, T04, T05, T06 each end with exactly one final:

| item | source | labels |
| --- | --- | --- |
| T01 | agreement | delivery, product |
| T02 | arbitration | product, service |
| T03 | agreement | product, service |
| T04 | agreement | delivery |
| T05 | arbitration | product |
| T06 | arbitration | one of product/service (single winning concurrent write) |

Entries that were ever single-submitted or disputed appear only once they
become agreed or arbitrated.
