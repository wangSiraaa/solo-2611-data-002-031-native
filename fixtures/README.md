# Fixed terminal input application
Python 3.11 standard library; Linux PTY with 80x24 screen is the task environment.
Copy `fixtures/faults.json` to `faults-run.json`; run
`python3 fixtures/legacy_app.py --db legacy.sqlite --faults faults-run.json` in a real PTY.
The app initializes three records once, accepts line-terminated visible menu actions, emits ANSI
clear/cursor/local-line redraw, and commits SQLite writes before success output.
Fault flags are one-shot and consumed in the supplied control file; restart with the same DB after a drop.
Use separate fresh DB/fault files for independent scenarios. Enable only needed faults per scenario.
Available faults: drop_after_save_once, reject_save_once, conflict_before_save_once,
unknown_prompt_once, query_unavailable_once. A conflict simulates another committed modification.
Only an independent verifier may inspect SQLite items/history. The automation must exclusively use PTY.
This is the application under automation; no screen parser, driver, reconciliation or expected output is supplied.
