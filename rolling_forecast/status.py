"""状态常量：预测状态、评价状态、未评价原因。"""
from __future__ import annotations

# ---- 预测状态（prediction_status）----
PRED_OK = "predicted"                 # 成功产出数值预测
PRED_BLOCKED = "blocked_conflict"     # 同修订内容冲突，整折阻断
PRED_NAIVE_UNAVAILABLE = "unpredictable_naive_missing_lag"  # 朴素基线所需滞后缺测
PRED_MODEL_INSUFFICIENT = "unpredictable_model_insufficient_history"  # 可训练样本不足
PRED_MODEL_FIT_FAILED = "unpredictable_model_fit_failed"  # 拟合异常，明确失败而非零
PRED_MODEL_NO_FEATURES = "unpredictable_model_no_usable_features"  # 特征全部缺失等

# ---- 评价状态（evaluation_status）----
EVAL_DONE = "evaluated"
EVAL_SKIPPED_PREDICTION = "not_evaluated_no_prediction"
EVAL_SKIPPED_TRUTH_MISSING = "not_evaluated_truth_not_visible"
EVAL_SKIPPED_TRUTH_CONFLICT = "not_evaluated_truth_conflict"

# ---- 模型标识 ----
MODEL_NAIVE = "seasonal_naive_v1"
MODEL_RIDGE = "ridge_lag_v1"

# ---- 多步策略 ----
STRATEGY_NAIVE = "seasonal_naive：直接取同星期滞后值，逐步独立"
STRATEGY_RECURSIVE = "recursive：同一模型逐期递推，滞后输入只用历史/已生成预测，不接触真实未来"
