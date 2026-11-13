# Sequential recording source
Run `python3 fixtures/service.py --port 8109`. Only Python standard library is required.
workflow.json specifies the three-step HTTP conversation and JSON Pointer symbol extraction.
The password is a synthetic fixture credential, not a production secret.
Each login/report creates random identifiers; the source has no request-body log.
The fixed flow is login → create report → read report. No parallel matching or request retry is needed.
The recorder/replay implementation, recorded corpus and expected symbol substitutions are not supplied.
