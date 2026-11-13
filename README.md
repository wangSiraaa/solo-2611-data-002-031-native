# 点-in-time 滚动销量预测工具

离线滚动原点（rolling-origin）预测与核验工具。目标是复现"门店在历史上**真正能够
做出**的销量预测"：每个发起时刻只能使用当时已可见的数据，迟到修订、迟公布日历、
缺测与同修订冲突都按发起时刻的资格处理，未来信息不能倒灌到过去。不提供在线预测服务。

## 环境

- Python 3.11，依赖见 `requirements.txt`（pandas 2.x、scikit-learn 1.x）

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

## 三个命令

```sh
# 1) 执行逐折预测 + 评价，产物写到 out/
.venv/bin/python -m rolling_forecast run \
  --sales fixtures/sales.csv --calendar fixtures/calendar.csv \
  --plan fixtures/plan.json --out out

# 2) 方法学说明（多步策略、回退规则、数据资格），可同时落盘
.venv/bin/python -m rolling_forecast explain --out out

# 3) 独立核验（九项检查，任一失败返回非零退出码）
.venv/bin/python -m rolling_forecast verify \
  --sales fixtures/sales.csv --calendar fixtures/calendar.csv --plan fixtures/plan.json
```

## 数据资格规则（核心）

1. **可见性**：只取 `available_at <= origin` 的 sales/calendar 行；
2. **最高修订**：同一 `record_id` 取当时可见的最大 `revision`；
3. **同修订冲突阻断**：同键同 revision 内容不一致 => 相关折整体 `blocked_conflict`，
   不任选一行、不猜测；
4. **缺测不是零**：无 sales 行的日期保持 NaN，任何阶段不补零；
5. **日历同样有可见时刻**：迟公布停业（如 B 店 2025-02-17 在 02-20 才公布）
   不会影响 02-17 12:00 发起的折。

## 模型

| 模型 | 标识 | 说明 |
| --- | --- | --- |
| 季节朴素 | `seasonal_naive_v1` | 目标日 t 取 t-7 当时可见销量；滞后缺测则该步不可预测 |
| 滞后 Ridge | `ridge_lag_v1` | 特征 lag_1、lag_7、planned_open；均值填充 + StandardScaler + Ridge(α=1.0)，**全部逐折 fit** |

- 多步采用**递推（recursive）**：同一模型逐期推进，滞后只引用历史观测或本折已生成的
  预测值，预测期真实销量零接触；选择理由见 `explain`。
- 训练序列覆盖"训练窗 + 最大滞后"扩展区间：训练窗早期样本的窗外滞后取真实观测
  （窗外日期只作滞后来源，不作监督目标）；历史不足（完整监督样本 < 7）、目标近似
  常数、特征全缺、拟合异常：分别输出明确的 `unpredictable_*` / `fit_failed` 状态，
  绝不输出零冒充结果。
- 无嵌套调参、无模型搜索、无预测区间；参数完全来自 `plan.json`。

## 输出产物（`out/`）

| 文件 | 内容 |
| --- | --- |
| `predictions.csv` | 逐折逐步预测、状态、实际值、误差、滞后取值与来源（record_id/revision/CSV 行号）、特征向量 |
| `fold_evidence.json` | 每折来源记录、日历来源、填充均值、scaler 参数、Ridge 系数/截距、模型标识、冲突详情 |
| `metrics_by_series_step.csv` | 门店 × 模型 × 步数的 MAE/RMSE/MAPE 与评价/未评价计数 |
| `not_evaluated_reasons.csv` | 每条未评价预测的具体原因（真值缺测/真值冲突/预测侧无数值） |
| `summary.json` | 计划快照、状态计数、汇总指标 |
| `manifest.json` | 输入/输出 SHA-256、Python 与平台版本，供可重复核验 |
| `report.html` | 自包含静态 HTML 报告（无外部资源） |
| `explain.md` | 方法学说明（运行 explain --out 后生成） |

评价真值只取 `plan.evaluation_as_of` 时刻可见的最高修订，与预测时可见范围严格分开；
缺测/冲突样本逐行登记，不允许悄悄剔除后只报平均分。

## 独立核验（verify）覆盖

1. 朴素预测手算复核（含 rev0→rev1 修订切换点）；
2. 滞后来源追溯到具体 CSV 行、修订与可见时刻；
3. **未来不变性**：把 origin 后才可见的销量改成 999999，旧预测逐值不变；
4. **全量预处理泄漏反例**：合成迟到暴增修订，证明逐折 fit 参数与错误的全量 fit 显著不同，
   且与独立可见快照重算完全一致；
5. 输入行重排不变性；
6. 缺测不补零（朴素不可预测、Ridge 无零预测、评价无零真值）；
7. 同修订内容冲突阻断整折且不波及其余门店；
8. 迟公布日历/迟到观测不倒灌；
9. 重复运行逐字节可复现。

## 代码结构

```
rolling_forecast/
  config.py       plan.json 解析与固定参数
  io_data.py      CSV 加载/校验（保留原始行号）
  visibility.py   点-in-time 快照、最高修订、冲突检测
  models.py       季节朴素 + 逐折 Ridge（真实 fit）与递推预测
  backtest.py     折编排与阻断逻辑
  evaluate.py     独立真值快照、误差与未评价原因
  reporting.py    CSV/JSON/HTML/manifest 输出
  verify.py       九项独立核验
  explain.py      方法学文本
  cli.py          run / explain / verify 入口
```
