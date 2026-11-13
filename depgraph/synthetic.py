"""合成数据生成器（只使用合成数据，不接触任何真实生产资料）。

所有生成器保证输出为 DAG、节点编号连续、边去重，并保留显式孤立节点。
演示规模上限 2000 节点，超过即拒绝。
"""

from __future__ import annotations

import random

MAX_DEMO_NODES = 2000


def _validate(n: int, isolated: int) -> None:
    if n <= 0:
        raise ValueError("节点数必须为正整数")
    if n > MAX_DEMO_NODES:
        raise ValueError(f"演示规模不得超过 {MAX_DEMO_NODES} 个节点，收到 {n}")
    if isolated < 0 or isolated >= n:
        raise ValueError("孤立节点数需满足 0 <= isolated < n")


def _active_nodes(n: int, isolated: int) -> list[int]:
    # 编号最大的若干节点作为孤立节点，便于人工核对。
    return list(range(1, n - isolated + 1))


def chain(n: int, isolated: int = 0) -> tuple[list[int], list[tuple[int, int]]]:
    """链 1 -> 2 -> ... -> (n-isolated)，其余为孤立节点。"""
    _validate(n, isolated)
    nodes = list(range(1, n + 1))
    active = _active_nodes(n, isolated)
    edges = [(a, b) for a, b in zip(active, active[1:])]
    return nodes, edges


def diamond(
    n: int, isolated: int = 0
) -> tuple[list[int], list[tuple[int, int]]]:
    """串联菱形：每 4 个活动节点形成 top -> {mid1, mid2} -> bottom。"""
    _validate(n, isolated)
    active = _active_nodes(n, isolated)
    if len(active) < 4:
        raise ValueError("菱形至少需要 4 个活动节点")
    edges: list[tuple[int, int]] = []
    blocks = len(active) // 4
    for b in range(blocks):
        top, m1, m2, bottom = active[4 * b: 4 * b + 4]
        edges.extend([(top, m1), (top, m2), (m1, bottom), (m2, bottom)])
    # 相邻菱形之间串一条边，避免退化为多个互不相连的组件
    for b in range(blocks - 1):
        edges.append((active[4 * b + 3], active[4 * (b + 1)]))
    # 不能整除 4 时，剩余节点顺链接在最后一个菱形之后
    leftovers = active[4 * blocks:]
    tail = [active[4 * blocks - 1]] + leftovers if blocks else []
    edges.extend((a, b) for a, b in zip(tail, tail[1:]))
    return list(range(1, n + 1)), sorted(set(edges))


def layered_dag(
    n: int,
    isolated: int = 0,
    extra_edges: int = 0,
    seed: int = 7,
) -> tuple[list[int], list[tuple[int, int]]]:
    """分层随机 DAG：边只允许从小编号指向大编号，天然无环。

    先保证活动节点连成一棵“最小编号可达”骨架，再随机追加前向边；
    extra_edges 不会落到孤立节点上，确保 isolated 个节点真正孤立。
    """
    _validate(n, isolated)
    rng = random.Random(seed)
    active = _active_nodes(n, isolated)
    edges: set[tuple[int, int]] = set()

    # 骨架：每个活动节点（除第一个）连向某个更早的节点
    for idx in range(1, len(active)):
        src = rng.choice(active[:idx])
        edges.add((src, active[idx]))

    # 候选前向边只在活动节点内部，避免破坏孤立节点
    candidates = [
        (a, b)
        for i, a in enumerate(active)
        for b in active[i + 1:]
    ]
    rng.shuffle(candidates)
    for pair in candidates:
        if len(edges) - (len(active) - 1) >= extra_edges:
            break
        edges.add(pair)

    return list(range(1, n + 1)), sorted(edges)


def build(style: str, n: int, isolated: int = 0, seed: int = 7,
          extra_edges: int = 0) -> tuple[list[int], list[tuple[int, int]]]:
    if style == "chain":
        return chain(n, isolated)
    if style == "diamond":
        return diamond(n, isolated)
    if style == "layered":
        return layered_dag(n, isolated, extra_edges=extra_edges, seed=seed)
    raise ValueError(f"未知图样式: {style}（可选 chain/diamond/layered）")
