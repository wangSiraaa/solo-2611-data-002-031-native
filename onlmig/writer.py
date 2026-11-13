"""源表写入器：只在本地生成 INSERT / UPDATE / DELETE 事务。

- 单条事务 CLI（writer insert/update/delete）
- 并发写入器（writer run）：多线程随机产生三类事务，按比例制造回滚，
  回滚的写入不会出现在变更日志里，演示末尾会专门核验。
"""
from __future__ import annotations

import random
import threading
import time

from .db import connect

VALID_CODES = [" alpha-01 ", "beta_02", "  gamma.03  ", "delta04", "EPSILON-05",
               "mixedCase06", "zeta07 ", "  eta-08", "theta09", "iota10",
               "kappa-é-11", "lambda12", "mu/13", "nu#14", "xi15"]
# 去首尾空白后为空 -> 规范 code 为空，属于格式错误
# 只用空格：保证 SQL btrim 与 Python str.strip 的演示口径完全一致
INVALID_CODES = ["   ", "      ", "  "]


def random_code(rng: random.Random) -> str:
    # 约 8% 概率产生格式错误（空白 code）
    if rng.random() < 0.08:
        return rng.choice(INVALID_CODES)
    return rng.choice(VALID_CODES)


def do_insert(conn, rid: int, raw_code: str, amount_cents: int, active: bool) -> None:
    with conn.transaction():
        conn.execute(
            "INSERT INTO records (id, raw_code, amount_cents, active) VALUES (%s,%s,%s,%s)",
            [rid, raw_code, amount_cents, active],
        )


def do_update(conn, rid: int, raw_code: str, amount_cents: int, active: bool) -> None:
    with conn.transaction():
        conn.execute(
            "UPDATE records SET raw_code=%s, amount_cents=%s, active=%s WHERE id=%s",
            [raw_code, amount_cents, active, rid],
        )


def do_delete(conn, rid: int) -> None:
    with conn.transaction():
        conn.execute("DELETE FROM records WHERE id=%s", [rid])


def run_concurrent(seconds: float, threads: int, rate: float, rollback_pct: float,
                   id_start: int, seed: int) -> dict[str, int]:
    """并发写入器：每个线程独立连接，随机产生本地事务，部分主动回滚。"""
    id_lock = threading.Lock()
    next_id = id_start
    stats = {"insert": 0, "update": 0, "delete": 0, "rollback": 0, "errors": 0}
    stats_lock = threading.Lock()
    deadline = time.monotonic() + seconds

    def worker(tid: int) -> None:
        nonlocal next_id
        local = random.Random(seed * 100 + tid)
        conn = connect()
        try:
            while time.monotonic() < deadline:
                kind = local.choices(
                    ["insert", "update", "delete"], weights=[6, 3, 1], k=1
                )[0]
                rollback = local.random() * 100 < rollback_pct
                try:
                    with conn.transaction():
                        if kind == "insert":
                            with id_lock:
                                rid = next_id
                                next_id += 1
                            conn.execute(
                                "INSERT INTO records (id, raw_code, amount_cents, active)"
                                " VALUES (%s,%s,%s,%s)",
                                [rid, random_code(local),
                                 local.randint(-500000, 5_000_000),
                                 local.random() < 0.8],
                            )
                        elif kind == "update":
                            row = conn.execute(
                                "SELECT id FROM records ORDER BY random() LIMIT 1"
                            ).fetchone()
                            if row is None:
                                continue
                            conn.execute(
                                "UPDATE records SET raw_code=%s, amount_cents=%s, active=%s WHERE id=%s",
                                [random_code(local), local.randint(0, 9_999_999),
                                 local.random() < 0.7, row[0]],
                            )
                        else:
                            row = conn.execute(
                                "SELECT id FROM records ORDER BY random() LIMIT 1"
                            ).fetchone()
                            if row is None:
                                continue
                            conn.execute("DELETE FROM records WHERE id=%s", [row[0]])
                        if rollback:
                            # 演示用：显式回滚。触发器写的变更记录随之消失，
                            # 迁移不可能看到这次写入。
                            raise _Rollback
                except _Rollback:
                    with stats_lock:
                        stats["rollback"] += 1
                    continue
                except Exception:  # 竞争导致的偶发错误（如更新已被删的行）
                    with stats_lock:
                        stats["errors"] += 1
                    continue
                with stats_lock:
                    stats[kind] += 1
                time.sleep(local.uniform(0, 1.0 / max(rate, 0.01)))
        finally:
            conn.close()

    jobs = [threading.Thread(target=worker, args=(i,), name=f"writer-{i}")
            for i in range(threads)]
    for j in jobs:
        j.start()
    for j in jobs:
        j.join()
    stats["max_id_used"] = next_id - 1
    return stats


class _Rollback(Exception):
    pass
