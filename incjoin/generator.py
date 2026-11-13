"""合成数据与测试场景生成。

generate 命令产出的文件可直接喂给 load / apply；测试套件也复用这里的场景。
所有数据都是确定性的（固定随机种子），不依赖任何外部服务。
"""

from __future__ import annotations

import json
import random
from pathlib import Path
from typing import Any

from .errors import BatchValidationError

# 初始快照：含重复键 K2/K3、以及右侧单价为 0 的行
INITIAL_LEFT = [
    {"row_id": "L1", "join_key": "K1", "qty": 2},
    {"row_id": "L2", "join_key": "K2", "qty": 3},
    {"row_id": "L3", "join_key": "K2", "qty": 5},
    {"row_id": "L4", "join_key": "K3", "qty": 7},
    {"row_id": "L5", "join_key": "K4", "qty": 11},
]
INITIAL_RIGHT = [
    {"row_id": "R1", "join_key": "K1", "unit_cents": 100},
    {"row_id": "R2", "join_key": "K2", "unit_cents": 200},
    {"row_id": "R3", "join_key": "K2", "unit_cents": 0},
    {"row_id": "R4", "join_key": "K3", "unit_cents": 300},
]

# 命名场景批次。每个场景覆盖一类关键增量行为。
SCENARIOS: dict[str, dict[str, Any]] = {
    # 两侧同时对同一键增行：交叉新增 4 个配对，不能按单边增量相加
    "both_sides_add": {
        "batch_id": "B1-both-sides-add",
        "operations": [
            {"seq": 1, "action": "put", "side": "L", "row_id": "L6",
             "join_key": "K3", "qty": 4},
            {"seq": 2, "action": "put", "side": "R", "row_id": "R5",
             "join_key": "K3", "unit_cents": 50},
        ],
    },
    # 同批先删后加同一行（同键），最终状态只看批次结束时
    "delete_then_add": {
        "batch_id": "B2-delete-then-add",
        "operations": [
            {"seq": 1, "action": "delete", "side": "L", "row_id": "L1"},
            {"seq": 2, "action": "put", "side": "L", "row_id": "L1",
             "join_key": "K1", "qty": 8},
        ],
    },
    # 键迁移：L4 从 K3 迁到 K5，旧键配对删除、新键按需生成
    "key_migration": {
        "batch_id": "B3-key-migration",
        "operations": [
            {"seq": 1, "action": "put", "side": "L", "row_id": "L4",
             "join_key": "K5", "qty": 7},
            {"seq": 2, "action": "put", "side": "R", "row_id": "R6",
             "join_key": "K5", "unit_cents": 90},
        ],
    },
    # 右侧单价归零：配对保留、金额变 0
    "unit_price_zero": {
        "batch_id": "B4-unit-price-zero",
        "operations": [
            {"seq": 1, "action": "put", "side": "R", "row_id": "R1",
             "join_key": "K1", "unit_cents": 0},
        ],
    },
    # 删除最后一个配对来源：K4 在右表本无配对，这里删掉唯一右源使 K1 配对清零
    "last_pair_removed": {
        "batch_id": "B5-last-pair-removed",
        "operations": [
            {"seq": 1, "action": "delete", "side": "R", "row_id": "R1"},
        ],
    },
    # 多个重复键同时增删：K2 左右都是多行，删一条重复行不能误删其他配对
    "duplicate_keys": {
        "batch_id": "B6-duplicate-keys",
        "operations": [
            {"seq": 1, "action": "delete", "side": "L", "row_id": "L2"},
            {"seq": 2, "action": "put", "side": "L", "row_id": "L7",
             "join_key": "K2", "qty": 6},
            {"seq": 3, "action": "delete", "side": "R", "row_id": "R3"},
            {"seq": 4, "action": "put", "side": "R", "row_id": "R7",
             "join_key": "K2", "unit_cents": 250},
        ],
    },
    # 非法批次：delete 不存在的行，必须整批失败、不留痕迹
    "invalid_missing_delete": {
        "batch_id": "X1-invalid-missing-delete",
        "operations": [
            {"seq": 1, "action": "put", "side": "L", "row_id": "L9",
             "join_key": "K1", "qty": 1},
            {"seq": 2, "action": "delete", "side": "R", "row_id": "NO-SUCH"},
        ],
    },
    # 非法批次：静态校验即拒绝（qty 非正）
    "invalid_bad_qty": {
        "batch_id": "X2-invalid-bad-qty",
        "operations": [
            {"seq": 1, "action": "put", "side": "L", "row_id": "L9",
             "join_key": "K1", "qty": 0},
        ],
    },
}

# 并发测试专用：两个进程各自修改 K2 上不同的行
CONCURRENT_BATCH_A = {
    "batch_id": "C1-concurrent-a",
    "operations": [
        {"seq": 1, "action": "put", "side": "L", "row_id": "L3",
         "join_key": "K2", "qty": 100},
    ],
}
CONCURRENT_BATCH_B = {
    "batch_id": "C2-concurrent-b",
    "operations": [
        {"seq": 1, "action": "put", "side": "R", "row_id": "R2",
         "join_key": "K2", "unit_cents": 777},
    ],
}


def random_snapshot(rows_per_side: int = 40, key_pool: int = 8,
                    seed: int = 20261113) -> tuple[list[dict], list[dict]]:
    """确定性合成快照：重复键天然存在（键池远小于行数）。"""
    rng = random.Random(seed)
    left = [
        {
            "row_id": f"L{i:04d}",
            "join_key": f"K{rng.randrange(key_pool)}",
            "qty": rng.randint(1, 500),
        }
        for i in range(rows_per_side)
    ]
    right = [
        {
            "row_id": f"R{i:04d}",
            "join_key": f"K{rng.randrange(key_pool)}",
            "unit_cents": rng.randint(0, 2000),
        }
        for i in range(rows_per_side)
    ]
    return left, right


def random_batches(count: int = 20, *, prefix: str = "RND",
                   seed: int = 99) -> list[dict]:
    """确定性随机批次流，put/delete 混合、允许同批多次改行。

    维护两侧存活行集合：delete 只选当时存活的行，put 可复活旧行或创建新行，
    保证生成的批次流在按序应用时每一条 delete 都合法。
    """
    rng = random.Random(seed)
    batches: list[dict] = []
    live: dict[str, set[str]] = {
        "L": {f"L{i:04d}" for i in range(40)},
        "R": {f"R{i:04d}" for i in range(40)},
    }
    next_id = {"L": 40, "R": 40}
    for n in range(count):
        ops: list[dict] = []
        for seq in range(1, rng.randint(2, 5) + 1):
            side = rng.choice(["L", "R"])
            action = rng.choice(["put", "put", "delete"])
            op: dict[str, Any] = {"seq": seq, "action": action, "side": side}
            if action == "delete" and not live[side]:
                action = "put"
                op["action"] = "put"
            if action == "delete":
                op["row_id"] = rng.choice(sorted(live[side]))
            else:
                # 70% 改存活行，30% 引入新行（或复活已删行）
                if live[side] and rng.random() < 0.7:
                    op["row_id"] = rng.choice(sorted(live[side]))
                else:
                    new_id = f"{side}{next_id[side]:04d}"
                    next_id[side] += 1
                    op["row_id"] = new_id
                op["join_key"] = f"K{rng.randrange(10)}"
                if side == "L":
                    op["qty"] = rng.randint(1, 999)
                else:
                    op["unit_cents"] = rng.randint(0, 5000)
            ops.append(op)
        # 先在生成器侧模拟一遍，维护存活集合
        for op in ops:
            side = op["side"]
            if op["action"] == "delete":
                live[side].discard(op["row_id"])
            else:
                live[side].add(op["row_id"])
        batches.append({"batch_id": f"{prefix}-{n:03d}", "operations": ops})
    return batches


def write_json(path: Path, payload: Any) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return path


def generate_workspace(out_dir: str | Path) -> dict[str, str]:
    """把初始快照、命名场景与随机批次写到目录，返回文件路径清单。"""
    out = Path(out_dir)
    written: dict[str, str] = {}
    written["left"] = str(write_json(out / "snapshot_left.json", INITIAL_LEFT))
    written["right"] = str(write_json(out / "snapshot_right.json", INITIAL_RIGHT))
    for name, batch in SCENARIOS.items():
        written[name] = str(
            write_json(out / "scenarios" / f"{name}.json", batch)
        )
    written["concurrent_a"] = str(
        write_json(out / "scenarios" / "concurrent_a.json", CONCURRENT_BATCH_A)
    )
    written["concurrent_b"] = str(
        write_json(out / "scenarios" / "concurrent_b.json", CONCURRENT_BATCH_B)
    )

    left, right = random_snapshot()
    written["random_left"] = str(write_json(out / "random_left.json", left))
    written["random_right"] = str(write_json(out / "random_right.json", right))
    for i, batch in enumerate(random_batches()):
        written[f"random_batch_{i:03d}"] = str(
            write_json(out / "random_batches" / f"{batch['batch_id']}.json", batch)
        )
    return written


def validate_snapshot_rows(rows: Any, side: str) -> list[dict]:
    """load 输入的静态校验，规则与 DDL 约束保持一致。"""
    if not isinstance(rows, list) or not rows:
        raise BatchValidationError(f"{side} 侧快照必须是非空数组")
    value_field = "qty" if side == "L" else "unit_cents"
    minimum = 1 if side == "L" else 0
    seen: set[str] = set()
    cleaned: list[dict] = []
    for i, row in enumerate(rows):
        where = f"{side} 侧第 {i} 行"
        if not isinstance(row, dict):
            raise BatchValidationError(f"{where} 必须是对象")
        row_id = row.get("row_id")
        join_key = row.get("join_key")
        value = row.get(value_field)
        if not isinstance(row_id, str) or not row_id:
            raise BatchValidationError(f"{where} row_id 必须是非空字符串")
        if row_id in seen:
            raise BatchValidationError(f"{where} row_id {row_id!r} 重复")
        seen.add(row_id)
        if not isinstance(join_key, str) or not join_key:
            raise BatchValidationError(f"{where} join_key 必须是非空字符串")
        if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
            label = "正整数" if minimum == 1 else "非负整数"
            raise BatchValidationError(f"{where} {value_field} 必须是{label}")
        cleaned.append(
            {"row_id": row_id, "join_key": join_key, value_field: value}
        )
    return cleaned
