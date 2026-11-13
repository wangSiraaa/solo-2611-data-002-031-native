"""业务错误类型。

所有可预期的业务诊断都继承 FixedIncomeError，CLI 捕获后输出结构化诊断，
而不是吐出堆栈或返回一个由任意初值算出的价格。
"""

from __future__ import annotations


class FixedIncomeError(Exception):
    """固定息估值工具的业务错误基类。"""

    diagnostic_code = "fixed_income_error"

    def __init__(self, message: str, **details: object) -> None:
        super().__init__(message)
        self.message = message
        self.details = details

    def to_diagnostic(self) -> dict:
        return {
            "diagnostic_code": self.diagnostic_code,
            "message": self.message,
            "details": self.details,
        }


class ScheduleError(FixedIncomeError):
    """计息日程无法按整期生成（典型：未声明的残缺期）。"""

    diagnostic_code = "schedule_error"


class ContractValidationError(FixedIncomeError):
    """合约或报价工具包含本工具不支持的条款 / 非法字段。"""

    diagnostic_code = "contract_validation_error"


class CalibrationError(FixedIncomeError):
    """折现曲线校准失败（支点不足、无解、重复支点、不支持的工具等）。"""

    diagnostic_code = "calibration_error"


class OutOfRangeError(FixedIncomeError):
    """合约现金流落在曲线支持的到期范围之外。"""

    diagnostic_code = "out_of_range"
