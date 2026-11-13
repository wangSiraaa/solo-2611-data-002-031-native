"""家庭分层随机优惠券实验的资格检查与随机化推断工具（仅标准库）。

模块划分：
- models:      输入记录与诊断的数据结构
- validation:  按原分配设计检查输入资格，汇总家庭指标
- design:      分层完全随机化的分配空间（合法名额枚举/采样）
- inference:   分层估计量、随机化检验、常数加性效应反演区间
- analysis:    端到端分析流水线
- cli:         命令行入口
"""

from .analysis import run_analysis
from .models import HouseholdMetric

__all__ = ["run_analysis", "HouseholdMetric"]
