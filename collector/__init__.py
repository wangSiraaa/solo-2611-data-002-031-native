"""单路 SSE 采集客户端。

模块划分：
- sse: 字节级 SSE 解析（UTF-8 增量解码、CR/LF/CRLF、分块边界）
- storage: SQLite 持久化（业务记录与可信断点同事务提交）
- client: HTTP 连接、Last-Event-ID 续接、退避重连与停止条件
- cli: 启动/停止/状态查询/人工恢复入口
"""

__all__ = ["sse", "storage", "client", "cli"]
