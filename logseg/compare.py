"""两版切分结果的对比。

先按原始字节覆盖对应事件：一对一、拆分、合并、无法唯一对应。
只有边界完全相同的组才逐字段比较模板、类型和值；绝不把旧版
事件 id 强行沿用到新版（id 只在各自版本内有意义，对应全靠字节
覆盖）。
"""

from __future__ import annotations


def _span(event: dict) -> tuple[int, int]:
    return (event["span"]["start"], event["span"]["end"])


def align_events(
    events_a: list[dict], events_b: list[dict]
) -> tuple[list[dict], list[dict], list[dict]]:
    """按字节覆盖把两版确认事件分组对应。

    返回 (groups, unmatched_a, unmatched_b)。group 形如
    {"a": [...], "b": [...], "aligned": bool}；aligned=False 表示
    两边从同一起点出发却无法拼到共同终点（无法唯一对应）。
    """
    groups: list[dict] = []
    unmatched_a: list[dict] = []
    unmatched_b: list[dict] = []
    i = j = 0
    while i < len(events_a) and j < len(events_b):
        a, b = events_a[i], events_b[j]
        sa, sb = _span(a), _span(b)
        if sa[0] == sb[0]:
            ga, gb = [a], [b]
            end_a, end_b = sa[1], sb[1]
            i += 1
            j += 1
            aligned = True
            while end_a != end_b:
                if end_a < end_b:
                    if i < len(events_a) and _span(events_a[i])[0] == end_a:
                        ga.append(events_a[i])
                        end_a = _span(events_a[i])[1]
                        i += 1
                    else:
                        aligned = False
                        break
                else:
                    if j < len(events_b) and _span(events_b[j])[0] == end_b:
                        gb.append(events_b[j])
                        end_b = _span(events_b[j])[1]
                        j += 1
                    else:
                        aligned = False
                        break
            groups.append({"a": ga, "b": gb, "aligned": aligned})
        elif sa[0] < sb[0]:
            unmatched_a.append(a)
            i += 1
        else:
            unmatched_b.append(b)
            j += 1
    unmatched_a.extend(events_a[i:])
    unmatched_b.extend(events_b[j:])
    return groups, unmatched_a, unmatched_b


def compare_fields(event_a: dict, event_b: dict) -> list[dict]:
    """边界相同的两事件逐字段比较模板、类型与值。"""
    diffs: list[dict] = []
    if event_a["rule"] != event_b["rule"]:
        diffs.append(
            {"kind": "rule_changed",
             "a": {"rule": event_a["rule"]}, "b": {"rule": event_b["rule"]}}
        )
    fields_a, fields_b = event_a["fields"], event_b["fields"]
    for name in sorted(set(fields_a) | set(fields_b)):
        fa, fb = fields_a.get(name), fields_b.get(name)
        if fa is None:
            diffs.append({"field": name, "kind": "field_added_in_b",
                          "b": _field_view(fb)})
        elif fb is None:
            diffs.append({"field": name, "kind": "field_removed_in_b",
                          "a": _field_view(fa)})
        elif fa["type"] != fb["type"]:
            diffs.append({"field": name, "kind": "type_changed",
                          "a": _field_view(fa), "b": _field_view(fb)})
        elif fa["ok"] != fb["ok"] or fa["value"] != fb["value"]:
            diffs.append({"field": name, "kind": "value_changed",
                          "a": _field_view(fa), "b": _field_view(fb)})
    return diffs


def _field_view(field: dict) -> dict:
    view = {"type": field["type"], "ok": field["ok"], "value": field["value"]}
    if not field["ok"]:
        view["error"] = field["error"]
    return view


def _event_ref(event: dict, revision: str) -> str:
    return f"{revision}:{event['id']}"


def _classify_group(group: dict) -> dict:
    ga, gb = group["a"], group["b"]
    out = {
        "a": {"events": [_event_ref(e, "A") for e in ga],
              "spans": [_span(e) for e in ga]},
        "b": {"events": [_event_ref(e, "B") for e in gb],
              "spans": [_span(e) for e in gb]},
    }
    if not group["aligned"]:
        out["kind"] = "unresolvable"
        out["detail"] = "byte coverage cannot be aligned to a common end"
        return out
    out["span"] = {"start": _span(ga[0])[0], "end": _span(ga[-1])[1]}
    if len(ga) == 1 and len(gb) == 1:
        out["kind"] = "same_boundary"
        out["a"]["rule"] = ga[0]["rule"]
        out["b"]["rule"] = gb[0]["rule"]
        diffs = compare_fields(ga[0], gb[0])
        out["field_diffs"] = diffs
        out["verdict"] = "unchanged" if not diffs else "changed"
    elif len(ga) == 1:
        out["kind"] = "split"
        out["detail"] = f"1 event in A -> {len(gb)} events in B"
    elif len(gb) == 1:
        out["kind"] = "merge"
        out["detail"] = f"{len(ga)} events in A -> 1 event in B"
    else:
        out["kind"] = "aligned_many_to_many"
        out["detail"] = "coverage aligns but is not a clean split/merge"
    return out


def _unit_statuses(source: dict) -> list[dict]:
    """取一个源的块/对象状态序列，用于标注对侧上下文。"""
    units = source.get("blocks") or source.get("objects") or []
    return [
        {"index": u.get("index"), "status": u.get("status"),
         "span": u.get("span"), "lines": u.get("lines",
                                               [u.get("line"), u.get("line")])}
        for u in units
    ]


def _opposite_context(event: dict, other_source: dict) -> dict:
    """说明对侧版本在事件字节范围内的状态（歧义/未知/隔离等）。"""
    start, _ = _span(event)
    for unit in _unit_statuses(other_source):
        span = unit["span"]
        if span and span["start"] <= start < span["end"]:
            return {"unit_index": unit["index"], "status": unit["status"],
                    "lines": unit["lines"]}
    return {"status": "no_covering_unit"}


def _overlaps(event: dict, others: list[dict]) -> bool:
    s1, e1 = _span(event)
    return any(s1 < _span(o)[1] and _span(o)[0] < e1 for o in others)


def compare_source(source_a: dict, source_b: dict) -> dict:
    """对比同一文件在两版下的确认事件覆盖与字段。"""
    events_a = source_a.get("events", [])
    events_b = source_b.get("events", [])
    groups, unmatched_a, unmatched_b = align_events(events_a, events_b)
    result = {
        "path": source_a["path"],
        "groups": [_classify_group(g) for g in groups],
        "only_in_a": [],
        "only_in_b": [],
        "unresolvable": [],
    }
    for event in unmatched_a:
        if _overlaps(event, events_b):
            result["unresolvable"].append(
                {"side": "a", "event": _event_ref(event, "A"),
                 "span": _span(event),
                 "detail": "overlaps B events without unique alignment"})
        else:
            result["only_in_a"].append(
                {"event": _event_ref(event, "A"), "rule": event["rule"],
                 "span": _span(event),
                 "opposite": _opposite_context(event, source_b)})
    for event in unmatched_b:
        if _overlaps(event, events_a):
            result["unresolvable"].append(
                {"side": "b", "event": _event_ref(event, "B"),
                 "span": _span(event),
                 "detail": "overlaps A events without unique alignment"})
        else:
            result["only_in_b"].append(
                {"event": _event_ref(event, "B"), "rule": event["rule"],
                 "span": _span(event),
                 "opposite": _opposite_context(event, source_a)})
    return result


def compare_revisions(seg_a: dict, seg_b: dict) -> dict:
    """对比两个完整切分结果（按路径对齐源）。"""
    files = []
    sources_b = {s["path"]: s for s in seg_b["sources"]}
    for source_a in seg_a["sources"]:
        source_b = sources_b.get(source_a["path"])
        if source_b is None or source_a.get("status") != "ok" \
                or source_b.get("status") != "ok":
            files.append({"path": source_a["path"], "skipped": True})
            continue
        files.append(compare_source(source_a, source_b))
    return {"revisions": [seg_a["revision"], seg_b["revision"]], "files": files}
