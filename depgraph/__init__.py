"""depgraph —— 本地工序依赖维护工具。

模块划分：
- db:         连接与迁移
- reference:  独立于 SQL 的图遍历参考实现（三色 DFS 找环、迭代 DFS 求可达）
- repo:       建图、批次变更、增量闭包维护、查询与校验
- synthetic:  仅含合成数据的 DAG 生成器
- cli:        命令行入口
"""

__all__ = ["db", "reference", "repo", "synthetic", "cli"]
