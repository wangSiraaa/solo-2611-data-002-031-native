"""`snapx inspect` — show the state of exports (published and attempts)."""

import argparse
import json
import os

from . import exports_root, util


def _collect(export_id: str | None):
    root = exports_root()
    result = {}
    ids = set()
    if os.path.isdir(root):
        for name in os.listdir(root):
            if name != "_attempts" and os.path.isdir(os.path.join(root, name)):
                ids.add(name)
    att_root = os.path.join(root, "_attempts")
    if os.path.isdir(att_root):
        ids.update(os.listdir(att_root))
    for eid in sorted(ids):
        if export_id and eid != export_id:
            continue
        entry = {"export_id": eid, "published": None, "attempts": []}
        man_path = os.path.join(root, eid, "manifest.json")
        if os.path.exists(man_path):
            entry["published"] = util.read_json(man_path)
        att_dir = os.path.join(att_root, eid)
        if os.path.isdir(att_dir):
            for name in sorted(os.listdir(att_dir)):
                rec_path = os.path.join(att_dir, name, "ATTEMPT.json")
                if os.path.exists(rec_path):
                    rec = util.read_json(rec_path)
                    rec["attempt_dir"] = os.path.join(att_dir, name)
                    entry["attempts"].append(rec)
        result[eid] = entry
    return result


def cmd_inspect(args: argparse.Namespace) -> int:
    data = _collect(args.export_id)
    if args.json:
        print(json.dumps(data, indent=2, sort_keys=True))
        return 0
    if not data:
        print("no exports found")
        return 0
    for eid, entry in data.items():
        print(f"export '{eid}'")
        if entry["published"]:
            m = entry["published"]
            t = m["totals"]
            print(f"  PUBLISHED attempt={m['attempt']} snapshot={m['db']['snapshot_id']} "
                  f"epoch={m['epoch']} shards={m['params']['shards']}")
            print(f"    orders={t['orders']} payments={t['payments']} "
                  f"order_amount_cents={t['order_amount_cents']} "
                  f"payment_amount_cents={t['payment_amount_cents']}")
            print(f"    finished={m['finished_iso']} dir={os.path.join(exports_root(), eid)}")
            if m.get("writer"):
                w = m["writer"]
                print(f"    writer committed during export: epoch {w['epoch_before']} -> "
                      f"{w['epoch_after']} at {w['commit_iso']}")
        else:
            print("  not published")
        for rec in entry["attempts"]:
            line = f"  attempt {rec.get('attempt')}: status={rec.get('status')}"
            if rec.get("snapshot_id"):
                line += f" snapshot={rec['snapshot_id']} epoch={rec.get('epoch')}"
            print(line)
            if rec.get("reason"):
                print(f"    reason: {rec['reason']}")
            print(f"    dir: {rec['attempt_dir']}")
    return 0
