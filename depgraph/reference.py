"""独立参考图算法（纯 Python，不使用任何数据库递归实现）。

校验时用它从直接边重新生成参考可达集合，再与物化闭包逐对比较，
避免“用同一种递归思路既维护又验证”的盲区。
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping


class ValidationError(ValueError):
    """图输入本身不合法（未知节点、自环、节点编号非法等）。"""


def build_adjacency(
    edges: Iterable[tuple[int, int]],
    known_nodes: Iterable[int] | None = None,
) -> dict[int, list[int]]:
    """把 (prerequisite, dependent) 边集合折叠成去重、排序的邻接表。

    known_nodes 给定时校验每条边的端点都是已登记节点，并拒绝自环。
    孤立节点也会出现在返回的邻接表中（邻接列表为空）。
    """
    known = set(known_nodes) if known_nodes is not None else None
    adj: dict[int, set[int]] = defaultdict(set)
    if known is not None:
        for node in known:
            adj[node]  # 登记孤立节点
    for pair in edges:
        u, v = pair
        if u == v:
            raise ValidationError(f"自环边不允许: {u} -> {u}")
        if known is not None and (u not in known or v not in known):
            raise ValidationError(f"边引用未知节点: {u} -> {v}")
        adj[u].add(v)
        adj.setdefault(v, set())
    return {node: sorted(nbrs) for node, nbrs in adj.items()}


def find_cycle(
    adj: Mapping[int, Iterable[int]],
) -> list[int] | None:
    """三色深度优先搜索找环。

    返回一条可核验的有向环路径，首尾节点相同，例如 [3, 5, 2, 3]；
    无环时返回 None。迭代实现，避免大图递归深度问题。
    """
    WHITE, GRAY, BLACK = 0, 1, 2
    color: dict[int, int] = {node: WHITE for node in adj}
    # parent[v] = 进入 v 时所在的节点，用于回溯环路径
    parent: dict[int, int] = {}

    for root in sorted(adj):
        if color[root] != WHITE:
            continue
        color[root] = GRAY
        stack: list[tuple[int, int]] = [(root, 0)]
        while stack:
            node, idx = stack[-1]
            neighbors = adj[node]
            if idx < len(neighbors):
                nxt = neighbors[idx]
                stack[-1] = (node, idx + 1)
                if color.get(nxt, WHITE) == WHITE:
                    color[nxt] = GRAY
                    parent[nxt] = node
                    stack.append((nxt, 0))
                elif color[nxt] == GRAY:
                    # 从 nxt 沿 parent 回溯到 node，再补上 nxt 形成闭环
                    path = [node]
                    while path[-1] != nxt:
                        path.append(parent[path[-1]])
                    path.reverse()
                    path.append(nxt)
                    return path
            else:
                color[node] = BLACK
                stack.pop()
    return None


def reachable_pairs(
    edges: Iterable[tuple[int, int]],
    known_nodes: Iterable[int] | None = None,
) -> set[tuple[int, int]]:
    """从直接边独立计算全部可达对（不含自身对）。

    以每个节点为起点做迭代 DFS，记录严格可达的后代。
    调用方需保证图无环；有环时行为定义为“传递闭包”，不会死循环，
    但正式流程中应先用 find_cycle 拒绝有环图。
    """
    adj = build_adjacency(edges, known_nodes)
    pairs: set[tuple[int, int]] = set()
    for start in adj:
        seen: set[int] = set()
        stack = list(adj[start])
        while stack:
            node = stack.pop()
            if node in seen or node == start:
                continue
            seen.add(node)
            pairs.add((start, node))
            stack.extend(adj.get(node, ()))
    return pairs


def ancestors_of(
    adj: Mapping[int, Iterable[int]], node: int
) -> list[int]:
    """求单节点全部祖先（反向上的严格可达），按节点编号排序。"""
    reverse: dict[int, list[int]] = defaultdict(list)
    for src, nbrs in adj.items():
        for dst in nbrs:
            reverse[dst].append(src)
    seen: set[int] = set()
    stack = list(reverse.get(node, ()))
    while stack:
        cur = stack.pop()
        if cur in seen:
            continue
        seen.add(cur)
        stack.extend(reverse.get(cur, ()))
    return sorted(seen)


def descendants_of(
    adj: Mapping[int, Iterable[int]], node: int
) -> list[int]:
    """求单节点全部后代（正向上的严格可达），按节点编号排序。"""
    seen: set[int] = set()
    stack = list(adj.get(node, ()))
    while stack:
        cur = stack.pop()
        if cur in seen:
            continue
        seen.add(cur)
        stack.extend(adj.get(cur, ()))
    return sorted(seen)
