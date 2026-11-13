"""领域错误类型。"""


class IncrementalJoinError(Exception):
    """所有可预期业务错误的基类，CLI 统一以非零码退出。"""


class BatchValidationError(IncrementalJoinError):
    """批次 JSON 结构或取值非法，整批不得生效。"""


class BatchConflictError(IncrementalJoinError):
    """同一 batch_id 已提交，但本次规范内容与历史内容不同。"""


class VerifyError(IncrementalJoinError):
    """完整重算比对发现物化结果与源表不一致。"""
