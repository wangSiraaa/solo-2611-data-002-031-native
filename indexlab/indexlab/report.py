"""报告：从归档的原始计划与测量值生成可解释建议（report.md / report.json）。"""
from __future__ import annotations

import json
from pathlib import Path

from .config import Config
from .measure import median, summarize_times


def _load(cfg: Config, *parts) -> dict | None:
    p = cfg.archive.joinpath(*parts)
    return json.loads(p.read_text()) if p.exists() else None


def _fmt_times(times):
    s = summarize_times(times)
    return f"{s['median']:.1f} (min {s['min']:.1f} / max {s['max']:.1f}, n={s['n']})"


def generate(cfg: Config) -> str:
    meta = _load(cfg, "meta.json") or {}
    state = _load(cfg, "state.json") or {}
    cand_doc = _load(cfg, "candidates.json") or {}
    baseline = _load(cfg, "baseline", "measure.json")
    base_write = _load(cfg, "baseline", "writebench.json")
    combos = state.get("combos", {})
    if not baseline or not combos:
        raise SystemExit("归档不完整：需要先运行 init / run")

    L: list[str] = []
    w = L.append

    # ---- 1. 元信息
    w("# 索引实验报告\n")
    w("## 1. 实验元信息\n")
    w(f"- PostgreSQL: `{meta.get('version', '?')}`")
    st = meta.get("stats", {})
    w(f"- 统计信息收集: {st.get('method', '?')}（default_statistics_target="
      f"{st.get('default_statistics_target', '?')}，未使用扩展统计）")
    w(f"- 固定 seed: `{meta.get('seed')}`；数据规模: {meta.get('counts')}")
    fp = meta.get("fingerprints", {})
    w(f"- 原始数据指纹(count/bit_xor_hash): "
      + ", ".join(f"{t}={v['rows']}/{v['hash']}" for t, v in fp.items()))
    w(f"- 关键参数: {meta.get('settings')}")
    w(f"- 计时方法: 每(查询,参数组) warmup {cfg.measurement['warmup']} 次后交错计时 "
      f"{cfg.measurement['repeats']} 次，报告中位数与分布；写入测量每次从模板全新克隆，"
      f"重复 {cfg.writebench['repeats']} 次")
    w("")

    # ---- 2. 候选及来源
    w("## 2. 候选索引（由查询结构解析生成，附来源）\n")
    w("| 候选 | 来源(查询:规则) |")
    w("|---|---|")
    for c in cand_doc.get("kept", []):
        w(f"| `{c['label']}` | {', '.join(c['sources'])} |")
    dropped = cand_doc.get("dropped", [])
    if dropped:
        w(f"\n剔除与已有索引等价/冗余的候选 {len(dropped)} 条：")
        for d in dropped:
            w(f"- `{d['candidate']}` — {d['reason']}（来源 {', '.join(d['sources'])}）")
    w("")

    # ---- 3. 组合试验表
    w("## 3. 组合试验表（全部为实际执行过的组合）\n")
    w("| 组合 | 索引 | 构建耗时s | 空间MB | 写入倍率 | 结果一致 | 可行 | 加权几何平均加速 | 备注 |")
    w("|---|---|---|---|---|---|---|---|---|")
    for cid, c in sorted(combos.items()):
        idxs = "<br>".join(f"`{i['def']}`" for i in c["indexes"])
        build = sum(i["build_s"] for i in c["indexes"])
        note = "; ".join(c["violations"]) if c["violations"] else ""
        w(f"| {cid} | {idxs} | {build:.2f} | {c['space_mb']:.1f} | "
          f"{c['write_ratio']} | {c['correct']} | {c['feasible']} | "
          f"{c['weighted_geomean']} | {note} |")
    w("")
    w(f"写入基线: {base_write.get('times_s')} s（同一初始数据模板克隆，相同操作流 "
      f"{base_write.get('ops')} 条）")
    w("")

    # ---- 4. 每类查询详情
    w("## 4. 每类查询的加速/变慢与计划变化\n")
    feasible = {cid: c for cid, c in combos.items()
                if c["feasible"] and c["status"] == "done"}
    best_cid = max(feasible, key=lambda c: feasible[c]["weighted_geomean"] or 0,
                   default=None)
    qdesc = {q.qid: q.desc for q in cfg.queries}
    for qid in [q.qid for q in cfg.queries]:
        w(f"### {qid} — {qdesc.get(qid, '')}\n")
        groups = baseline[qid]
        w("| 参数组 | 基线中位数ms (分布) | 推荐组合中位数ms | 加速比 | 实际使用的新索引 | 访问路径变化 |")
        w("|---|---|---|---|---|---|")
        best = combos.get(best_cid) if best_cid else None
        for gname, g in groups.items():
            bmed = f"{median(g['times_ms']):.1f} ({min(g['times_ms']):.1f}~{max(g['times_ms']):.1f})"
            if best:
                pq = {r["group"]: r for r in best["per_query"].get(qid, [])}
                r = pq.get(gname)
                if r:
                    used = [i for i in r["indexes_used"] if i.startswith(cfg.index_prefix)]
                    used_s = ", ".join(f"`{u}`" for u in used) if used else "（未使用新索引）"
                    nb = "Seq" if "Seq Scan" in r["node_types_base"] else "Idx"
                    nc = "Seq" if "Seq Scan" in r["node_types_combo"] else "Idx"
                    path = f"{nb}→{nc}" if nb != nc else "不变"
                    w(f"| {gname} | {bmed} | {r['combo_median_ms']:.1f} | "
                      f"{r['speedup']:.2f}x | {used_s} | {path} |")
                    continue
            w(f"| {gname} | {bmed} | - | - | - | - |")
        w("")

    # ---- 5. 反直觉发现
    w("## 5. 反直觉发现（由实测数据支持）\n")
    findings = _findings(cfg, combos)
    for f in findings:
        w(f"- {f}")
    if not findings:
        w("- （本次未检出）")
    w("")

    # ---- 6. 推荐与局限
    w("## 6. 推荐与局限\n")
    if best_cid and (feasible[best_cid]["weighted_geomean"] or 0) > 1.0:
        c = feasible[best_cid]
        w(f"**推荐组合 {best_cid}**（在已测试组合与当前数据分布下最优）：")
        for i in c["indexes"]:
            w(f"- `{i['def']}` — 大小 {i['size_bytes']/1e6:.1f}MB，构建 {i['build_s']}s")
        w(f"\n加权几何平均加速 **{c['weighted_geomean']}x**，空间 {c['space_mb']:.1f}MB"
          f"（预算 {cfg.constraints['space_budget_mb']}MB），写入倍率 {c['write_ratio']}"
          f"（阈值 {cfg.constraints['write_slowdown_max']}）。")
    else:
        w("**无可接受方案**：所有已测试组合均不可行或无净收益，如实报告。")
    w("")
    w("### 局限\n")
    w("- 推荐仅适用于**本次实际测试过的组合**与当前合成数据分布；未测试的组合未参与比较，"
      "本报告不声称它们更差。")
    w("- 数据为固定 seed 的合成分布（热点客户/日期倾斜/字段相关性按构造生成），"
      "真实负载的选择性可能不同。")
    w("- 计时为本机单次实验的重复测量分布，存在缓存与噪声影响；报告给出 min~max 区间，"
      "不承诺固定加速倍数。")
    w("- 候选空间限定为普通 B-tree 单列/≤3 列复合/可选 INCLUDE；部分索引、表达式索引、"
      "扩展统计等未纳入。")
    w("- 写入代价以固定操作流在同一初始数据克隆上测得；真实写入混合比例不同结论可能变化。")
    w("")

    # ---- 7. 已测试组合清单
    w("## 7. 实际检查过的组合清单\n")
    for cid, c in sorted(combos.items()):
        w(f"- {cid}: {c['candidates']} -> {c['status']}"
          + (f"（{c['error']}）" if c.get("error") else ""))
    w("\n未列出的组合未被测试，其性能未知。")
    w("")

    # ---- 8. 失败与缺失测量
    failed = {cid: c for cid, c in combos.items() if c["status"] != "done"}
    w("## 8. 失败/缺失测量记录\n")
    if failed:
        for cid, c in sorted(failed.items()):
            w(f"- {cid}: {c['error']}")
    else:
        w("- 无")
    w("")

    text = "\n".join(L)
    (cfg.archive / "report.md").write_text(text)
    (cfg.archive / "report.json").write_text(json.dumps({
        "recommendation": best_cid if best_cid else None,
        "combos": combos,
    }, indent=1))
    return text


def _findings(cfg: Config, combos: dict) -> list[str]:
    """从实测组合中自动检出两类反直觉现象。"""
    out = []
    singles: dict[str, dict] = {}
    for cid, c in combos.items():
        if len(c["candidates"]) == 1 and c["status"] == "done":
            singles[c["candidates"][0]] = c

    def parse(label: str):
        # "orders(a, b) INCLUDE(c)" -> (table, (a,b), (c,))
        table = label.split("(")[0]
        key = label[label.index("(") + 1: label.index(")")].split(", ")
        inc = ()
        if "INCLUDE(" in label:
            inc = tuple(label.split("INCLUDE(")[1].rstrip(")").split(", "))
        return table, tuple(key), inc

    # A: 单列不如复合（工作负载级）；列顺序影响显著（查询级）
    by_table: dict[str, list[tuple[tuple, dict]]] = {}
    for label, c in singles.items():
        t, key, inc = parse(label)
        if not inc:
            by_table.setdefault(t, []).append((key, c))
    for t, lst in by_table.items():
        keys = {k: v for k, v in lst}
        seen_pairs: set[frozenset] = set()
        for key, c in keys.items():
            if len(key) != 2:
                continue
            rev = (key[1], key[0])
            if frozenset(key) in seen_pairs or rev not in keys:
                continue
            seen_pairs.add(frozenset(key))
            s1, s2 = keys.get((key[0],)), keys.get((key[1],))
            if not (s1 and s2):
                continue
            g_a = c["weighted_geomean"] or 0
            g_b = keys[rev]["weighted_geomean"] or 0
            g_s1 = s1["weighted_geomean"] or 0
            g_s2 = s2["weighted_geomean"] or 0
            if min(g_a, g_b) <= max(g_s1, g_s2) * 1.1:
                continue
            # 查询级列序差异：找两种列序差距最大的参数组
            worst = None
            for qid in c["per_query"]:
                ma = {r["group"]: r["combo_median_ms"]
                      for r in c["per_query"][qid]}
                mb = {r["group"]: r["combo_median_ms"]
                      for r in keys[rev]["per_query"].get(qid, [])}
                for g, va in ma.items():
                    vb = mb.get(g)
                    if vb and min(va, vb) > 0:
                        ratio = max(va, vb) / min(va, vb)
                        if worst is None or ratio > worst[0]:
                            worst = (ratio, qid, g, va, vb)
            order_note = ""
            if worst and worst[0] > 1.5:
                _, qid, grp, va, vb = worst
                order_note = (f"；列顺序在查询级影响显著：{qid}/{grp} 中位数 "
                              f"({key[0]},{key[1]}) 组 {va}ms vs ({rev[0]},{rev[1]}) 组 "
                              f"{vb}ms（差 {worst[0]:.1f} 倍）——左前缀列决定索引能为"
                              f"哪些查询做定位与有序扫描，两种列序服务不同的查询")
            out.append(
                f"**单列不如复合**：`{t}({key[0]})` 单索引 {g_s1}x、"
                f"`{t}({key[1]})` 单索引 {g_s2}x，而复合 `({key[0]}, {key[1]})` "
                f"{g_a}x、`({rev[0]}, {rev[1]})` {g_b}x{order_note}")
    # B: 读快但约束不允许（取分数最高的前 3 个，其余计数汇总）
    feas_scores = [c["weighted_geomean"] or 0 for c in combos.values() if c["feasible"]]
    best_feas = max(feas_scores, default=0)
    viol = [c for c in sorted(combos.values(),
                              key=lambda c: -(c["weighted_geomean"] or 0))
            if not c["feasible"] and (c["weighted_geomean"] or 0) > best_feas
            and (c["weighted_geomean"] or 0) > 1.0]
    for c in viol[:3]:
        out.append(
            f"**读快但约束不允许**：组合 {c['combo_id']} {c['candidates']} 读加速 "
            f"{c['weighted_geomean']}x（优于所有可行组合的最佳 {best_feas}x），但 "
            f"{'; '.join(c['violations'])}，依规则不得推荐。")
    if len(viol) > 3:
        out.append(f"（另有 {len(viol) - 3} 个读加速超过可行最优但违反约束的组合，"
                   f"详见组合试验表）")
    return out
