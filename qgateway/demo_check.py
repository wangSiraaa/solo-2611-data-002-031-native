"""独立核对：绕过网关，直连数据库重跑目录 SQL，逐行比对导出文件。

这是“独立直连数据库核对完整成功结果的类型、行数和值”的实现：
- 不经过 HTTP/NDJSON 协议，直接用 psycopg 驱动执行目录里的同一条 SQL；
- 比对行数、每列值与类型（int8 -> int、NUMERIC -> Decimal、timestamptz、date、NULL）；
- 导出文件缺少 success 终态时直接判失败，证明中断结果不会被标记完成。
"""
from __future__ import annotations

import datetime as _dt
import decimal
import json
from pathlib import Path
from typing import Any

from psycopg import AsyncConnection

from .catalog import bind_params, get
from .values import decode_value


async def check_export_against_database(dsn: str, export_file: Path) -> int:
    meta_frame, rows_frames, terminal = _read_export(export_file)
    if terminal is None:
        print(f"[FAIL] {export_file} 没有终态帧 -> 结果未完成")
        return 1
    if terminal.get("status") != "success":
        print(f"[FAIL] 终态不是 success：{terminal.get('status')}")
        return 1

    entry = get(meta_frame["query_id"])
    params = bind_params(entry, meta_frame.get("params", {}))
    export_rows = [row for frame in rows_frames for row in frame["rows"]]

    conn = await AsyncConnection.connect(dsn, autocommit=True)
    try:
        async with conn.cursor() as cur:
            await cur.execute(entry.sql, params)
            db_rows = await cur.fetchall()
            db_cols = [(d.name, d.type_code) for d in cur.description]
    finally:
        await conn.close()

    problems: list[str] = []
    if len(db_rows) != len(export_rows):
        problems.append(f"行数不一致：数据库 {len(db_rows)} vs 导出 {len(export_rows)}")
    if terminal["rows_sent"] != len(db_rows):
        problems.append(
            f"终态行数 {terminal['rows_sent']} 与数据库行数 {len(db_rows)} 不一致"
        )

    for idx, (db_row, raw_row) in enumerate(zip(db_rows, export_rows)):
        decoded = [decode_value(cell) for cell in raw_row]
        for col_idx, (db_val, got) in enumerate(zip(db_row, decoded)):
            if not _value_equal(db_val, got):
                problems.append(
                    f"第 {idx} 行列 {db_cols[col_idx][0]} 值不一致：db={db_val!r} export={got!r}"
                )
                break
        if len(problems) > 10:
            break

    # 类型可识别性抽查
    type_ok = _check_type_tags(rows_frames, db_rows, db_cols)
    if not type_ok:
        problems.append("类型标签抽查失败（NULL/int8/numeric/timestamptz/date）")

    print(f"查询            : {entry.query_id}")
    print(f"数据库行数      : {len(db_rows)}")
    print(f"导出行数        : {len(export_rows)}")
    print(f"终态            : {terminal['status']}（bytes_sent={terminal['bytes_sent']}）")
    print(f"列定义          : {[(c['name'], c['type_name']) for c in meta_frame['columns']]}")
    if problems:
        for problem in problems:
            print(f"[FAIL] {problem}")
        return 1
    print("[PASS] 行数、逐行值、NULL 与精确类型全部与直连数据库一致")
    return 0


def _read_export(path: Path):
    meta = None
    rows_frames = []
    terminal = None
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            frame = json.loads(line)
            if frame["type"] == "meta":
                meta = frame
            elif frame["type"] == "rows":
                rows_frames.append(frame)
            elif frame["type"] == "terminal":
                terminal = frame
    if meta is None:
        raise ValueError("导出文件缺少 meta 帧")
    return meta, rows_frames, terminal


def _value_equal(db_val: Any, got: Any) -> bool:
    if db_val is None:
        return got is None
    if isinstance(db_val, decimal.Decimal):
        return isinstance(got, decimal.Decimal) and db_val == got
    if isinstance(db_val, _dt.datetime):
        # 驱动返回 aware datetime；协议保留微秒与时区
        return isinstance(got, _dt.datetime) and db_val == got
    if isinstance(db_val, _dt.date):
        return isinstance(got, _dt.date) and not isinstance(got, _dt.datetime) and db_val == got
    if isinstance(db_val, (bytes, memoryview)):
        return bytes(db_val) == got
    if isinstance(db_val, int):
        return type(got) is int and db_val == got
    return db_val == got


def _check_type_tags(rows_frames: list[dict], db_rows: list, db_cols: list) -> bool:
    if not rows_frames or not db_rows:
        return True
    first_raw = rows_frames[0]["rows"][0]
    first_db = db_rows[0]
    for raw, db_val, (_, oid) in zip(first_raw, first_db, db_cols):
        if db_val is None:
            if raw is not None:
                return False
            continue
        if not isinstance(raw, dict) or "t" not in raw:
            return False
        # 关键类型标签与 pg 类型 OID 对应
        if oid == 20 and raw["t"] != "int8":
            return False
        if oid == 1700 and raw["t"] != "numeric":
            return False
        if oid == 1184 and raw["t"] != "timestamptz":
            return False
        if oid == 1082 and raw["t"] != "date":
            return False
    return True
