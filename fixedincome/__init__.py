"""本地固定息合约估值工具。

只支持单币种、固定本金、固定票息的合约：
- 计息边界按月生成（支持月底约定），付款日按假日表修正后续调整；
- 计息规则统一 ACT/365 Fixed；
- 基于简单计息存款与固定息基准债券净价自举校准折现曲线；
- 提供 price / scenario / inspect 三个命令。
"""

from .errors import (
    CalibrationError,
    ContractValidationError,
    FixedIncomeError,
    OutOfRangeError,
    ScheduleError,
)

__all__ = [
    "FixedIncomeError",
    "ScheduleError",
    "ContractValidationError",
    "CalibrationError",
    "OutOfRangeError",
]
