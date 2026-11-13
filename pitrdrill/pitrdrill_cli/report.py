"""演练报告: 汇总状态、证据、核验结果与磁盘占用, 输出 Markdown。"""
from __future__ import annotations

import json
from pathlib import Path

from .archive import list_archived_segments
from .state import (RESULT_FAILED, RESULT_PROMOTED,
                    RESULT_TARGET_REACHED, RESULT_WAITING_ARCHIVE, Store)
from .util import dir_size, human_bytes, now_iso

RESULT_CN = {
    RESULT_TARGET_REACHED: "目标已到达",
    RESULT_WAITING_ARCHIVE: "归档仍缺失而等待",
    RESULT_FAILED: "恢复失败",
    RESULT_PROMOTED: "已提升为新时间线",
    "": "未开始/进行中",
}


def _table(headers: list[str], rows: list[list]) -> str:
    out = ["| " + " | ".join(headers) + " |",
           "| " + " | ".join("---" for _ in headers) + " |"]
    for row in rows:
        out.append("| " + " | ".join(str(c).replace("\n", " ") for c in row) + " |")
    return "\n".join(out)


def build_report(store: Store) -> Path:
    cfg = store.cfg
    st = store.state
    lines: list[str] = []
    lines.append("# PostgreSQL 16 PITR 演练报告")
    lines.append("")
    lines.append(f"- 生成时间: {now_iso()}")
    lines.append(f"- 任务标识: `{st.task_tag}`")
    lines.append(f"- 演练根目录(清理边界): `{cfg.root}`")
    lines.append(f"- 工具版本: pitrdrill {store.state.config.get('pgbin', '')}")
    lines.append("")

    # 源实例
    src = st.source
    lines.append("## 1. 源实例身份")
    lines.append(_table(["项", "值"], [
        ["system identifier", src.get("system_identifier", "")],
        ["数据目录", src.get("data_dir", "")],
        ["socket 目录", src.get("socket_dir", "")],
        ["端口", src.get("port", "")],
        ["WAL 段大小(字节)", cfg.wal_seg_size_bytes],
        ["当前时间线", next((r["timeline"] for r in reversed(st.restore_points)), 1)],
        ["日志路径", src.get("log_path", "")],
        ["PID", src.get("pid", "")],
    ]))
    lines.append("")

    # 基础备份
    lines.append("## 2. 物理基础备份")
    rows = []
    for name, b in st.backups.items():
        rows.append([
            name, b.get("note", ""), b.get("start_lsn", ""),
            b.get("checkpoint_lsn", ""), b.get("backup_timeline", ""),
            b.get("system_identifier", ""),
            "corrupt" if b.get("corrupt") else f"verify_rc={b.get('verifybackup_rc')}",
            len(b.get("backup_wal_segments", [])),
        ])
    lines.append(_table(
        ["名称", "说明", "起始LSN", "checkpoint LSN", "时间线",
         "system id", "完整性", "自带WAL段数"], rows))
    lines.append("")

    # restore points
    lines.append("## 3. 命名恢复点(恢复目标以名称指定, 非墙钟)")
    lines.append(_table(
        ["名称", "LSN", "时间线", "说明", "创建时间"],
        [[r["name"], r["lsn"], r["timeline"], r["note"], r["created_at"]]
         for r in st.restore_points]))
    lines.append("")

    # 归档
    lines.append("## 4. WAL 归档清单")
    segs = list_archived_segments(cfg)
    lines.append(f"- 归档目录: `{cfg.archive_dir}`")
    lines.append(f"- WAL 段总数: {len(segs)}")
    lines.append(f"- 首段: `{segs[0] if segs else '-'}`, "
                 f"末段: `{segs[-1] if segs else '-'}`")
    lines.append(f"- 归档索引: `{cfg.archive_index}`")
    lines.append("")
    lines.append("### 归档集合")
    lines.append(_table(
        ["集合", "路径", "段数", "排除/备注"],
        [[n, d.get("path", ""), len(d.get("files", [])),
          ",".join(d.get("excluded", [])) or ""]
         for n, d in st.archive_sets.items()]))
    lines.append("")

    # 事件/见证
    lines.append("## 5. 事务见证与关键事件")
    witnesses = [e for e in st.events if e.get("kind") == "witness"]
    lines.append(_table(
        ["标记", "commit LSN", "event_seq", "时间"],
        [[w.get("marker"), w.get("commit_lsn"), w.get("event_seq"), w.get("ts")]
         for w in witnesses]))
    for e in st.events:
        if e.get("kind") == "mishap":
            lines.append("")
            lines.append(f"- **误操作事件**: 删除 parent {e.get('deleted_parents')}, "
                         f"污染 parent {e.get('corrupted_parent')}, "
                         f"新增 parent {e.get('inserted_parent')}, "
                         f"LSN 区间 {e.get('before_lsn')} -> {e.get('after_lsn')}")
    lines.append("")

    # 恢复结果
    lines.append("## 6. 隔离恢复结果(四类状态严格区分)")
    rows = []
    for name, rs in st.restores.items():
        rows.append([
            name, rs.backup, rs.target, rs.archive_set,
            RESULT_CN.get(rs.result, rs.result or rs.phase),
            rs.promoted_timeline or "-",
            "; ".join(rs.failure_reasons)[:160] or "-",
        ])
    lines.append(_table(
        ["恢复实例", "基础备份", "目标", "归档集合", "结果",
         "提升后时间线", "失败原因/诊断"], rows))
    lines.append("")

    for name, rs in st.restores.items():
        lines.append(f"### 恢复实例 `{name}` — {RESULT_CN.get(rs.result, rs.result)}")
        lines.append(f"- 数据目录: `{rs.data_dir}`")
        lines.append(f"- socket: `{rs.socket_dir}`  端口: {rs.port}  PID: {rs.pid}")
        lines.append(f"- 日志: `{rs.log_path}`")
        if rs.target_evidence:
            lines.append("- 目标到达证据:")
            lines.append("```json")
            lines.append(json.dumps(rs.target_evidence, ensure_ascii=False, indent=2))
            lines.append("```")
        if rs.preflight:
            pf = rs.preflight
            lines.append(f"- 预检通过: **{pf.get('passed')}**")
            if pf.get("hard_errors"):
                lines.append("  - 硬错误: " + "; ".join(pf["hard_errors"]))
            gap = pf.get("checks", {}).get("gap", {})
            if gap:
                lines.append(
                    f"  - 缺口: 需 {len(gap.get('required', []))} 段, "
                    f"备份自带覆盖 {len(gap.get('covered_by_backup', []))} 段, "
                    f"缺失 {gap.get('missing', [])}")
        if rs.verification:
            v = rs.verification
            lines.append(f"- 独立 SQL 核验: {v['passed']}/{v['total']} 通过 "
                         f"(时间线 {v.get('timeline')})")
            for c in v["checks"]:
                lines.append(f"  - [{'PASS' if c['passed'] else 'FAIL'}] "
                             f"{c['check']}: {c['detail']}")
        lines.append("")

    # 失败演练
    if st.failure_demos:
        lines.append("## 7. 故障注入演练")
        for name, d in st.failure_demos.items():
            lines.append(f"- `{name}` ({d['demo']}): "
                         f"{json.dumps({k: v for k, v in d.items() if k != 'created_at'}, ensure_ascii=True)[:300]}")
        lines.append("")

    # 磁盘占用
    lines.append("## 8. 磁盘占用与清理边界")
    occ = []
    for label, p in [("源实例", cfg.source_dir), ("WAL 归档", cfg.archive_dir.parent),
                     ("基础备份", cfg.backups_dir), ("归档集合", cfg.sets_dir),
                     ("恢复实例", cfg.restores_dir)]:
        if p.exists():
            occ.append([label, str(p), human_bytes(dir_size(p))])
    lines.append(_table(["对象", "路径", "占用"], occ))
    lines.append("")
    lines.append(f"清理命令(仅清理本演练, 不触碰其他目录): "
                 f"`python3 pitrdrill.py --root {cfg.root} cleanup --yes`")
    lines.append("")

    out = cfg.reports_dir / "drill-report.md"
    out.write_text("\n".join(lines), encoding="utf-8")
    (cfg.reports_dir / "drill-report.json").write_text(
        json.dumps(st.to_json(), ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8")
    return out
