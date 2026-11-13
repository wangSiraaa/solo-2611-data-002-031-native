"""JSON task configuration helpers and the crawl CLI.

Subcommands:
    serve-sites        run the synthetic local website group
    init               create a fresh frontier database from the seeds
    run                crawl until every processable URL is terminal (or budget)
    status             print state/disposition counters and completeness
    resume             continue a paused/stopped task
    pause              ask a running task to pause at the next safe point
    export             write details / link graph / per-URL ledger as CSV+JSON
    reset              drop the frontier database
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from pathlib import Path

from .config import ConfigError, TaskConfig, load_config
from .engine import Crawler, RunResult
from .store import Store


def _cmd_init(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    store = Store(config.database, reset=args.reset)
    crawler = Crawler(config, store)
    crawler.seed()
    store.meta_set("started", "1")
    print(f"frontier initialised at {config.database}")
    counts = store.state_counts()
    print(f"  seeds: {len(config.all_seeds())}  pending: {counts['pending']}")
    store.close()
    return 0


def _guard_origins(store: Store, config: TaskConfig) -> bool:
    stored = store.meta_get("origins")
    expected = json.dumps(config.origin_values)
    if stored is not None and stored != expected:
        print(
            "refusing to continue: the frontier was created with different origins\n"
            f"  stored:   {stored}\n"
            f"  config:   {expected}\n"
            "use `init --reset` or resume with the original configuration",
            file=sys.stderr,
        )
        return False
    return True


def _run(config: TaskConfig, *, reset: bool) -> int:
    store = Store(config.database, reset=reset)
    crawler = Crawler(config, store)
    if reset or store.count("urls") == 0:
        crawler.seed()
        store.meta_set("started", "1")
    else:
        if not _guard_origins(store, config):
            store.close()
            return 2
    result = crawler.run()
    _print_run_result(store, result)
    store.close()
    return 0 if result.complete else 1


def _print_run_result(store: Store, result: RunResult) -> None:
    counts = store.state_counts()
    print(f"stop reason: {result.stopped_reason}")
    if result.budget_stopped:
        print(f"budget stop: {result.unprocessed} URLs left unprocessed (marked over_budget)")
    print(
        "states: pending={pending} in_flight={in_flight} retry_wait={retry_wait} done={done}".format(
            **counts
        )
    )
    print("dispositions:")
    for key, value in store.disposition_counts().items():
        print(f"  {key:<20} {value}")
    print(
        f"details={store.count('details')} links={store.count('links')} "
        f"attachments={store.count('attachments')} requests={store.count('requests')} "
        f"page_requests={store.budget_used()}"
    )
    if result.details.get("requeued_interrupted"):
        print(f"requeued interrupted rows on start: {result.details['requeued_interrupted']}")
    print("complete" if result.complete else "incomplete — resume to continue")


def _cmd_run(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    return _run(config, reset=args.reset)


def _cmd_resume(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    print(f"resuming frontier at {config.database}")
    return _run(config, reset=False)


def _cmd_status(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    store = Store(config.database)
    counts = store.state_counts()
    total = store.count("urls")
    active = counts["pending"] + counts["in_flight"] + counts["retry_wait"]
    budget_stopped = store.meta_get("budget_stopped") == "1"
    dispositions = store.disposition_counts()
    payload = {
        "database": str(config.database),
        "states": counts,
        "total_urls": total,
        "terminal": counts["done"],
        "active": active,
        "complete": active == 0 and not budget_stopped,
        "budget_used": store.budget_used(),
        "budget_stopped": budget_stopped,
        "last_stop_reason": store.meta_get("last_stop_reason"),
        "details": store.count("details"),
        "links": store.count("links"),
        "attachments": store.count("attachments"),
        "page_requests": store.count("requests", "kind='page'"),
        "robots_requests": store.count("requests", "kind='robots'"),
        "duplicate_discoveries": store.meta_int("duplicate_discoveries"),
        "new_discoveries": store.meta_int("new_discoveries"),
        "retries_scheduled": store.meta_int("retries_scheduled"),
        "redirect_hops": store.meta_int("redirect_hops"),
        "requeued_interrupted": store.meta_int("requeued_interrupted"),
        "over_budget": dispositions.get("over_budget", 0),
        "dispositions": dispositions,
    }
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(f"database: {payload['database']}")
        print(f"URLs: {total}  done: {counts['done']}  active: {active}")
        print(
            "  pending={pending} in_flight={in_flight} retry_wait={retry_wait}".format(**counts)
        )
        print(f"page budget used: {payload['budget_used']}")
        if payload["budget_stopped"]:
            print(f"still waiting: no — stopped by budget, "
                  f"{payload['over_budget']} URLs did not get a page request")
        else:
            print(f"still waiting: {'no' if payload['complete'] else 'yes'}")
        if payload["budget_stopped"]:
            print(f"budget stop: {payload['last_stop_reason']}")
        print(f"details={payload['details']} links={payload['links']} "
              f"attachments={payload['attachments']}")
        print(f"page requests={payload['page_requests']} robots requests={payload['robots_requests']}")
        print(f"duplicate discoveries={payload['duplicate_discoveries']} "
              f"new discoveries={payload['new_discoveries']} "
              f"retries scheduled={payload['retries_scheduled']} "
              f"redirect hops={payload['redirect_hops']}")
        if payload["over_budget"]:
            print(f"over budget={payload['over_budget']}")
        if payload["requeued_interrupted"]:
            print(f"requeued interrupted rows: {payload['requeued_interrupted']}")
        print("dispositions:")
        for key, value in dispositions.items():
            print(f"  {key:<20} {value}")
    store.close()
    return 0


def _cmd_pause(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    store = Store(config.database)
    store.meta_set("pause_requested", "1")
    print("pause requested; the running task stops at the next safe point")
    store.close()
    return 0


def _cmd_reset(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    store = Store(config.database, reset=True)
    store.close()
    print(f"removed frontier database {config.database}")
    return 0


def _cmd_export(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    store = Store(config.database)
    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)

    def write_csv(name: str, columns: list[str], rows) -> int:
        path = outdir / name
        with open(path, "w", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh)
            writer.writerow(columns)
            n = 0
            for row in rows:
                writer.writerow([row[c] for c in columns])
                n += 1
        return n

    def rows_to_dicts(rows):
        return [dict(r) for r in rows]

    details = rows_to_dicts(store.conn.execute(
        "SELECT canonical,item_id,title,fetch_url,http_status,source_canonical,"
        "source_url,entry_url,redirect_chain,first_seen FROM details ORDER BY id"
    ))
    n_details = write_csv(
        "details.csv",
        ["canonical", "item_id", "title", "fetch_url", "http_status", "source_canonical",
         "source_url", "entry_url", "redirect_chain", "first_seen"],
        details,
    )
    edges = rows_to_dicts(store.conn.execute(
        "SELECT source_canonical,source_url,target_canonical,target_request_url,"
        "raw_href,text,base_url,kind,occurrences FROM links ORDER BY id"
    ))
    n_edges = write_csv(
        "links.csv",
        ["source_canonical", "source_url", "target_canonical", "target_request_url",
         "raw_href", "text", "base_url", "kind", "occurrences"],
        edges,
    )
    ledger = rows_to_dicts(store.conn.execute(
        "SELECT canonical,request_url,origin,depth,state,disposition,detail,http_status,"
        "final_url,attempts,retries,discovered_count,source_canonical,source_url,"
        "source_text,entry_url,redirect_chain,updated_at FROM urls ORDER BY id"
    ))
    n_urls = write_csv(
        "urls.csv",
        ["canonical", "request_url", "origin", "depth", "state", "disposition", "detail",
         "http_status", "final_url", "attempts", "retries", "discovered_count",
         "source_canonical", "source_url", "source_text", "entry_url", "redirect_chain",
         "updated_at"],
        ledger,
    )
    attachments = rows_to_dicts(store.conn.execute(
        "SELECT detail_canonical,canonical,request_url,text,kind,registered_at "
        "FROM attachments ORDER BY id"
    ))
    write_csv(
        "attachments.csv",
        ["detail_canonical", "canonical", "request_url", "text", "kind", "registered_at"],
        attachments,
    )
    requests = rows_to_dicts(store.conn.execute(
        "SELECT ts,origin,url,kind,status,outcome,redirect_from,elapsed_ms,bytes_read,"
        "content_type,worker FROM requests ORDER BY id"
    ))
    n_requests = write_csv(
        "requests.csv",
        ["ts", "origin", "url", "kind", "status", "outcome", "redirect_from",
         "elapsed_ms", "bytes_read", "content_type", "worker"],
        requests,
    )
    dispositions = rows_to_dicts(store.conn.execute(
        "SELECT canonical,request_url,disposition,detail,http_status,final_url,ts "
        "FROM dispositions ORDER BY id"
    ))
    write_csv(
        "dispositions.csv",
        ["canonical", "request_url", "disposition", "detail", "http_status", "final_url", "ts"],
        dispositions,
    )

    summary = {
        "database": str(config.database),
        "states": store.state_counts(),
        "dispositions": store.disposition_counts(),
        "details": n_details,
        "links": n_edges,
        "urls": n_urls,
        "requests": n_requests,
        "page_requests": store.budget_used(),
        "budget_stopped": store.meta_get("budget_stopped") == "1",
        "last_stop_reason": store.meta_get("last_stop_reason"),
        "counters": {
            "duplicate_discoveries": store.meta_int("duplicate_discoveries"),
            "retries_scheduled": store.meta_int("retries_scheduled"),
            "redirect_hops": store.meta_int("redirect_hops"),
            "new_discoveries": store.meta_int("new_discoveries"),
        },
        "robots": rows_to_dicts(store.conn.execute(
            "SELECT origin,state,source,status,matched_group,attempts FROM robots ORDER BY origin"
        )),
        "metrics": store.metrics_summary(),
    }
    (outdir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (outdir / "details.json").write_text(
        json.dumps(details, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (outdir / "links.json").write_text(
        json.dumps(edges, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"exported to {outdir}: details={n_details} links={n_edges} urls={n_urls} requests={n_requests}")
    store.close()
    return 0


def _cmd_serve_sites(args: argparse.Namespace) -> int:
    from .site import RoundConfig, default_sites, start_group

    servers, _world = start_group(
        log_path=Path(args.log),
        sites=default_sites(args.nav_port),
        config=RoundConfig(
            slow_seconds=args.slow_seconds,
            slow_release_after=args.slow_release_after,
            rate_limit_every=args.rate_limit_every,
        ),
        ready_file=Path(args.ready_file) if args.ready_file else None,
    )
    print("synthetic site group ready:", flush=True)
    for srv in servers:
        print(f"  {srv.spec.name:<10} {srv.spec.origin}", flush=True)
    print(f"access log: {args.log}", flush=True)
    try:
        while True:
            time.sleep(1.0)
    except KeyboardInterrupt:
        for srv in servers:
            srv.shutdown()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="webfrontier",
        description="Config-scoped multi-site directory crawler (local synthetic sites only)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def add_common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--config", required=True, help="path to the task JSON config")

    p_init = sub.add_parser("init", help="create/reset the frontier database from seeds")
    add_common(p_init)
    p_init.add_argument("--reset", action="store_true", default=True,
                        help="drop existing state (the default for init)")
    p_init.set_defaults(func=_cmd_init)

    p_run = sub.add_parser("run", help="run the crawl to completion")
    add_common(p_run)
    p_run.add_argument("--reset", action="store_true", help="drop existing state before running")
    p_run.set_defaults(func=_cmd_run)

    p_resume = sub.add_parser("resume", help="continue a paused/stopped crawl")
    add_common(p_resume)
    p_resume.set_defaults(func=_cmd_resume)

    p_status = sub.add_parser("status", help="show progress and completeness")
    add_common(p_status)
    p_status.add_argument("--json", action="store_true", help="machine-readable output")
    p_status.set_defaults(func=_cmd_status)

    p_pause = sub.add_parser("pause", help="request a safe pause of a running crawl")
    add_common(p_pause)
    p_pause.set_defaults(func=_cmd_pause)

    p_reset = sub.add_parser("reset", help="delete the frontier database")
    add_common(p_reset)
    p_reset.set_defaults(func=_cmd_reset)

    p_export = sub.add_parser("export", help="export details, link graph and per-URL ledger")
    add_common(p_export)
    p_export.add_argument("--outdir", default=".runtime/export")
    p_export.set_defaults(func=_cmd_export)

    p_sites = sub.add_parser("serve-sites", help="run the local synthetic site group")
    p_sites.add_argument("--log", default=".runtime/site_access.log")
    p_sites.add_argument("--ready-file", default=".runtime/sites-ready.json")
    p_sites.add_argument("--slow-seconds", type=float, default=2.5)
    p_sites.add_argument("--slow-release-after", type=float, default=1.2)
    p_sites.add_argument("--rate-limit-every", type=int, default=3)
    p_sites.add_argument("--nav-port", type=int, default=8811)
    p_sites.set_defaults(func=_cmd_serve_sites)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130
