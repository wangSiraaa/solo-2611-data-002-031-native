"""工具内使用的异常类型。"""


class ExportError(Exception):
    """导出阶段的可预期错误（含范围外依赖），消息可直接展示给用户。"""


class UnsupportedDependency(ExportError):
    """闭包中遇到支持范围之外的依赖：明确列出并停止，绝不静默丢弃。"""

    def __init__(self, items):
        self.items = list(items)
        detail = "\n".join(f"  - {line}" for line in self.items)
        super().__init__("遇到支持范围之外的依赖，已停止导出：\n" + detail)


class ApplyError(Exception):
    """DDL 应用失败，事务已整体回滚。携带失败对象与数据库原始报错。"""

    def __init__(self, failed_object, reason, sql):
        self.failed_object = failed_object
        self.reason = reason
        self.sql = sql
        super().__init__(
            f"对象 {failed_object} 的 DDL 执行失败，事务已回滚：{reason}"
        )
