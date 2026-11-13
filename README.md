# Sequential HTTP record / offline replay

A standard-library-only (Python 3.11) recorder and offline replayer for the
fixed three-step fixture flow:

```
login  ->  create report  ->  read report
```

The problem this solves: every live login generates a new `session` id and
every create generates a new `report` id, so replaying captured URLs verbatim
can never match.  The recorder replaces those concrete values with **symbols**
that say *where a value is produced* and *where it is consumed*; replay
generates a fresh value per replay session and checks dependencies against that
session's own bindings.

See [docs/MATERIAL_FORMAT.md](docs/MATERIAL_FORMAT.md) for the on-disk format
and the security boundary.

## Layout

```
replaytool/          implementation (core.py, cli.py)
fixtures/            given synthetic service + workflow.json (unchanged)
examples/demo.py     the end-to-end demonstration described below
tests/               unittest suite (records against the real fixture)
docs/                material format + security notes
```

## Quick start

```sh
# 1) start the synthetic service
python3 fixtures/service.py --port 8109

# 2) record the flow once (writes .runtime/recorded/material.json)
python3 -m replaytool record \
    --workflow fixtures/workflow.json \
    --base-url http://127.0.0.1:8109 \
    --out .runtime/recorded

# 3) stop the fixture (Ctrl-C). Offline replay never contacts it again.

# 4) serve offline replay
python3 -m replaytool serve --material-dir .runtime/recorded --port 8110

# 5) create a replay session (each new session gets fresh ids)
curl -s -XPOST localhost:8110/__replay/sessions
# {"session_id": "…", "next_step": 0, "next_step_name": "login", …}

# 6) run the flow for that session (replace <sid> with the value above)
curl -s -XPOST localhost:8110/sessions \
  -H "X-Replay-Session: <sid>" \
  -H 'Content-Type: application/json' \
  -d '{"user":"demo","password":"demo-secret"}'

curl -s -XPOST localhost:8110/sessions/<session-from-login>/reports \
  -H "X-Replay-Session: <sid>" \
  -H "Authorization: Bearer <session-from-login>" \
  -H 'Content-Type: application/json' \
  -d '{"period":"2026-10"}'

curl -s localhost:8110/reports/<report-from-create> \
  -H "X-Replay-Session: <sid>" \
  -H "Authorization: Bearer <session-from-login>"

# session state / cleanup
python3 -m replaytool session status --material-dir .runtime/recorded --id <sid>
python3 -m replaytool session delete --material-dir .runtime/recorded --id <sid>
python3 -m replaytool session list   --material-dir .runtime/recorded
```

The replay server also exposes:
* `GET /__replay/health`
* `POST /__replay/sessions` (create)
* `GET|DELETE /__replay/sessions/<id>` (status / clean one session)

## Example: one record, two rounds, failure cases, leak scan

```sh
python3 examples/demo.py
```

The demo records against the fixture, **terminates the fixture**, then runs two
complete replay rounds.  Example output:

```
[round 1] session=s_4ccdd7… report=r_0ea2eb… read_total=42.50
[round 2] session=s_594fed… report=r_8588eb… read_total=42.50
-> ids differ between rounds, yet intra-round dependencies match

out-of-order create before login -> HTTP 409: out of order: the request looks like step 'create', but the next step for this session is 'login' (POST /sessions)
cross-session foreign session id -> HTTP 409: path does not match ... (value must equal this session's binding for session; values from another replay session are rejected)

scanned 6 files ... clean=True
literal password occurrences in recording dir: 0
replay ids found inside material.json: 0
```

## Symbols and placeholders

Recording output (request for `create`):

```json
"path": "/sessions/<ref:session:req>/reports",
"headers": [
  {"name": "Authorization", "value": "Bearer <ref:session:req>", "secret": true}
]
```

Response for `login`:

```json
{"session": {"id": "<symbol:session>"}}
```

The password is `"<redacted:PASSWORD>"`.  See below for why there is no digest.

### Generated values (`<symbol:…>`, `<ref:…>`)

* `<symbol:NAME>` is emitted at the exact JSON pointer where the response
  produced the value (`/session/id`, `/report/id`).  It means *this is where
  the value comes from*.
* `<ref:NAME:req>` / `<ref:NAME:resp>` appear where the value is later used
  (path, header, body string).  They mean *this is where the value is used*.
* At replay, each new replay session generates its own value for every symbol
  (`uuid4`), so round 1 and round 2 get different session/report ids.  A request
  is accepted only if the placeholder's occurrence equals the current session's
  binding.  Values from another session (or a value that was never produced
  because a dependency step is missing) are hard failures.
* Material validation refuses a recording that declares a generated symbol but
  does not emit it as `<symbol:NAME>` in the producing response.  A random id
  can therefore never be silently hard-coded as a business literal.

### Static secrets (`<redacted:LABEL>`)

Passwords, `Authorization` tokens and cookies become
`<redacted:LABEL>` (e.g. `<redacted:PASSWORD>`, `<redacted:AUTHORIZATION>`).
The placeholder records *which kind of secret* was present, and nothing derived
from its bytes.  An earlier design used a truncated keyed digest; that was
dropped because a low-entropy password can be confirmed by dictionary attack
even against a keyed digest.  The consequence is explicit: offline replay
treats that slot as an opaque wildcard (any non-empty string), because the
secret is intentionally unrecoverable.  Replay material still shows that a
secret existed there, just not its value.

More generally, material keeps *type* (string vs number vs object vs array vs
null), and distinguishes a missing key from a key set to `null` (they produce
different failures).

## Strict replay semantics

* Steps execute in recorded array order.  The next step is held in session
  state; there is no path/method lookup to pick a response.
* Out-of-order, repeated, or post-completion requests return `409` and do not
  advance the session.
* Path, declared headers, and JSON body are compared against the step template.
* Extra business JSON fields are rejected; missing required fields, wrong JSON
  types, and a wrong current-session binding are rejected with distinct errors.
* Unknown or malformed `X-Replay-Session`, and a session recorded under a
  different `material_id`, are rejected.
* A failed request never moves the step pointer, so a repeat cannot skip ahead.

## Security: where secrets can and cannot appear

* Every write goes through `write_files`, which scans the final bytes for all
  observed secrets and dynamic values (raw and JSON-escaped) and aborts before
  creating the file on any hit.  Only labels are reported.
* After recording, `audit_directory` rescans the entire recording directory
  (`material.json`, all `session-*.json`, `replay.log`).
* Diagnostics/logging never include bodies or header values; `replay.log` has
  only timestamps, event names, step names, and produced symbol *names*.
* Check boundary: coverage is the recorder's registry of **observed** secrets
  and dynamic values.  It does not cover the live service's memory (the demo
  stops the service before replay), the OS page cache, or values that were
  never observed by the recorder.

## Tests

```sh
python3 -m unittest discover -s tests -v
```
