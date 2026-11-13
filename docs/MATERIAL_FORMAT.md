# Material format

A recording directory written by `python -m replaytool record` contains:

| file | purpose |
| --- | --- |
| `material.json` | the recorded, redacted, replayable flow |
| `replay.log` | append-only event log (metadata only, see below) |
| `session-<id>.json` | one file per live replay session (created on first use) |

Nothing else is written.  Temporary files from `atomic_write` are renamed in
place and removed on failure; they never contain the final material.

## `material.json` top level

```jsonc
{
  "format": "sequential-http-replay",
  "version": 1,
  "material_id": "mat_<shape>_<random>",   // new per recording, no business data
  "created_at": "...",
  "generated_symbols": ["session", "report"],
  "symbols": [
    {
      "name": "session",
      "produced_by_step": "login",
      "json_pointer": "/session/id",
      "response_marker": "<symbol:session>",
      "request_reference": "<ref:session:req>"
    }
  ],
  "redaction_policy": { ... },
  "steps": [ ... ]                          // execution order is authoritative
}
```

`material_id` combines a shape hash with a random suffix.  The random part
means a *new* recording is a different material, so replay sessions recorded
against an older material are refused instead of silently replayed.

## Step

```jsonc
{
  "index": 0,
  "name": "login",
  "method": "POST",
  "produces": ["session"],
  "request":  { "method": "POST", "path": "/sessions", "headers": [...], "body": {...} },
  "response": { "status": 201, "headers": [...], "body": {...} }
}
```

Steps are replayed strictly in array order.  There is no lookup by path or
method at replay time; an out-of-order request is rejected and the session
position does not move.

## Request templates

Plain strings are literals and are compared byte-for-byte (after normalising
URLs to their path).  JSON values keep their JSON type; an object, array,
string, number, boolean or null in the material must be the same type in the
live request.

Three placeholder forms appear inside strings:

| form | meaning | replay semantics |
| --- | --- | --- |
| `<symbol:NAME>` | this response value is **produced here** | expanded to the current session's generated value.  Generated symbols always live in a response (`produces`). |
| `<ref:NAME:req>` | the value produced by symbol `NAME` is **consumed** in this request (path, header, body string) | must equal the current session's value for `NAME`; a different session's value is rejected. |
| `<ref:NAME:resp>` | a previously produced value echoed in a later response | expanded from the current session |
| `<redacted:LABEL>` | a static secret was here (password, Authorization token, Cookie) | opaque wildcard: any non-empty string is accepted.  The original value is unrecoverable by design. |

A symbol placeholder is not a "random constant": it records *where a value
comes from* and *where it is used*.  Replay re-generates a fresh value for
every new replay session, so two sessions have different session/report IDs
while each session's create/read requests still use the matching IDs.

### Why `<redacted:LABEL>` carries no digest

An earlier draft used `<redacted:LABEL:sha256:hex16>` with a keyed digest.
That is still a value-derived byte string, and a low-entropy password can be
confirmed offline by hashing guesses.  The final format therefore stores only
the *kind* of secret.  It cannot be reverted to the original and cannot be
dictionary-checked.  The trade-off is explicit: an offline replay cannot verify
the password, only that the field is present and non-empty.

## Response body kinds

* `{"kind": "json", "value": ...}` -- replayed by expanding placeholders.  The
  materialised body is re-checked against the recorded structural shape before
  it is served.
* `{"kind": "opaque", "length": N}` -- recorded for a non-JSON response.  Such
  a step fails explicitly at replay; the tool refuses to invent content.
* `{"kind": "empty"}` -- request with no body.

## `session-<id>.json`

```jsonc
{
  "format": "sequential-http-replay/session",
  "version": 1,
  "session_id": "0123456789abcdef",
  "material_id": "mat_...",
  "next_step": 1,
  "completed": false,
  "symbols": { "session": "s_<fresh uuid>" },
  "dependencies": { "session": "login" }
}
```

The `symbols` map contains **replay-generated values**, not recorded ones.
These are produced by `uuid4` at replay time and are intentionally persisted so
a session can be resumed after a request.  They are not secrets: they are
single-use synthetic IDs for the offline simulator.

## `replay.log`

One JSON object per line: timestamp, replay-session id, event
(`session_created`, `step_completed`, `rejected`, `error`, `session_cleaned`),
the step name, and produced symbol *names*.  No request/response bodies,
headers, secret values, or generated values are logged.

## Security boundary

* Before any material file is written, `write_files` scans the exact bytes for
  every observed secret and dynamic value (raw and JSON-escaped).  A hit aborts
  the write; only labels are reported.
* After recording, `audit_directory` rescans the whole recording directory.
* Secrets covered structurally: workflow-declared JSON pointers, secret-named
  JSON fields, `Authorization`/`Cookie`, and any header whose name contains
  token/secret/auth hints.
* Dynamic response values are covered either by a declared symbol (kept as
  `<symbol:...>`/`<ref:...>` with the value discarded) or, if unbound, they are
  rejected -- the recorder never silently imprints a random value.
* The check does **not** cover the live service's own memory/state, the
  operating system page cache, or values the recorder never observed (for
  example, a token generated by a user agent and never sent back).  The
  fixture and replay processes are shut down before the final scan.
