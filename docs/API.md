# API contract

Base URL: `http://127.0.0.1:8765` (default). All authenticated endpoints take

```
Authorization: Bearer <token>
```

Tokens come from `fixtures/identities.json`: `fixture-a` (`ann_a`, annotator),
`fixture-b` (`ann_b`, annotator), `fixture-arbiter` (`arbiter`). The acting
identity is **always** derived from the token. A body field such as
`author_id`, `user_id`, `annotator_id` or `arbiter_id` is rejected with
`400 identity_in_body`.

Request/response bodies are JSON (UTF-8). Errors always look like:

```json
{"error": {"code": "...", "message": "..."}}
```

Error payloads contain no item text, labels, peer status or submission ids, so
error responses cannot be used as a side channel.

## Visibility rules (enforced identically on every read path)

| Viewer | Item state | What is returned |
| --- | --- | --- |
| annotator, has not submitted | any | item id/text + `own_submission: null` + `status: "not_submitted"`. No peer field, no final. |
| annotator, has submitted | peer has not submitted | + `own_submission`, `peer_submitted: false`, no peer labels, no final |
| annotator, has submitted | both submitted | + `peer_submission`, `final` (agreement or arbitration if resolved) |
| arbiter | fewer than 2 submissions | item is omitted from list/queue; detail/arbitrate → `409 not_ready` |
| arbiter | 2 submissions | item text + both submissions (+ `final` if resolved) |

`GET /api/export` returns only items with a final result (`agreement` or
`arbitration`). Annotators additionally only see exported rows for items where
their own submission exists (this is always true for a final, but stated
explicitly). Disputed, single-submission and not-started items never appear.

The rule is also echoed in every relevant response as `visibility_rule`.

## Endpoints

### `GET /healthz`
Public. `{"ok": true}`.

### `GET /api/meta`
Public. Task description, item/label/annotator counts, and the visibility rule.

### `GET /api/labels`
Public. The fixed vocabulary:
```json
{"labels": [{"id": "product", "name": "商品问题"}, ...],
 "empty_set_allowed": true, "multiple_labels_allowed": true}
```

### `GET /api/items`
Auth required.

* annotator → own assigned items, visibility-filtered as in the table, with
  `status` in `not_submitted | waiting_for_peer | agreed | disputed | arbitrated`.
* arbiter → only items with 2 submissions; each entry has `item`, `status`,
  `submissions` (ordered by submission id), `final` (or `null`).

### `GET /api/items/{item_id}`
Auth required.

* annotator: own assigned item. `404 not_found` for unknown item,
  `403 not_assigned` if not assigned.
* arbiter: any known item **only if** both submissions exist; otherwise
  `409 not_ready`.

### `POST /api/items/{item_id}/submission`
Annotator only. An arbiter calling it gets `403 forbidden`.

```json
{"labels": ["product", "delivery"]}
```

* Label ids must be in the vocabulary (`400 unknown_label` otherwise — the
  error names only the offending ids).
* Order and duplicates do not matter: the set is normalized to a sorted
  unique list. `[]` is valid.
* Exactly one immutable submission per (item, annotator):
  * first time → `201`, body is the annotator view with `submitted: true`,
    `submission_id`;
  * identical repeat (after normalization) → `200`, same view plus
    `replayed: true`, no new row;
  * different labels → `409 submission_immutable`;
  * no longer assigned / unknown item → `403 not_assigned` / `404 not_found`;
  * body tries to set the actor → `400 identity_in_body`.
* When this completes the pair: identical sets create an `agreement` final;
  different sets leave the item `disputed` with no final.

### `GET /api/arbitration/queue`
Arbiter only (`403 forbidden` otherwise). Items with exactly two submissions,
differing label sets, and no final yet.

### `POST /api/items/{item_id}/arbitration`
Arbiter only.

```json
{"labels": ["product", "service"],
 "expected_submissions": {"ann_a": 3, "ann_b": 4}}   // optional
```

* Item must have both submissions (`409 not_ready` otherwise — this is also how
  the service refuses to help an annotator peek early).
* Refused if the two submissions already agree (`409 already_agreed`).
* `expected_submissions`, when supplied, must equal the current
  `{author_id: submission_id}` map (`409 stale_binding` otherwise). It is a
  compare-and-swap guard binding the decision to the pair the arbiter saw.
* First arbitration → `201` with `final` whose `binding` lists both
  submission ids/authors. Identical repeat → `200` + `replayed: true`.
  Any different attempt → `409 arbitration_immutable`. The row records the
  arbiter id, so a final can never be rewritten by item id alone.
* `labels` may equal either submission or be a new set; unknown labels are
  rejected as above.

### `GET /api/export`
Auth required (annotator or arbiter). `only_final: true`. Each row:

```json
{"item_id": "T01", "text": "...",
 "final": {"final_id": "agreement:1|arbitration:N",
           "status": "agreed|arbitrated",
           "source": "agreement|arbitration",
           "labels": ["delivery", "product"],
           "arbiter_id": "arbiter|null",
           "binding": {"item_id": "T01",
                       "submissions": [{"submission_id": 1, "author_id": "ann_a"},
                                       {"submission_id": 2, "author_id": "ann_b"}]}},
 "submissions": [{"submission_id": 1, "author_id": "ann_a",
                  "labels": [...], "created_at": "..."}, ...]}
```

## Status codes

`200` OK / replay · `201` created · `400` bad body or label · `401` missing/invalid
token · `403` wrong role or not assigned · `404` unknown item · `409` state
conflict (immutable / not ready / agreed / stale binding) · `411` missing
`Content-Length` · `413` oversized body.
