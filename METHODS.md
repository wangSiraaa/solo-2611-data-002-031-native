# 方法说明：分层家庭随机优惠券实验的资格检查与随机化推断

## 1. 实验设计与目标量

plan 固定了实验开始前的入组名单：

| 层 | 入组家庭数 n_s | 处理名额 k_s | 分配机制 |
|---|---|---|---|
| S1 | 6（H01–H06） | 2 | 层内等概率抽取恰好 2 户 |
| S2 | 6（H07–H12） | 4 | 层内等概率抽取恰好 4 户 |

两层独立完成完全随机化（complete randomization），合法分配总数
C(6,2)·C(6,4) = 15·15 = **225**，每个合法分配概率 1/225。

目标 estimand（plan 原文）：**全部 12 个入组家庭的平均处理效应**，
层权重为预定家庭数 n_s/N（两层各 1/2），与实际抽到多少处理户无关。

因此两层处理比例不同（1/3 vs 2/3）时，**不能把两组订单直接合并求均值差**：
朴素合并隐含按各层实际处理户数加权，权重由随机实现决定，估计的不是目标量。
工具在 `descriptive_statistics.naive_pooled_mean_difference_cents` 中仍给出该
对照数，但带明确警告，仅用于说明偏差，不出现在推断结论中。

## 2. 数据汇总与 ITT 队列

1. **固定成员关系**：person→household 只由 members.csv 决定，窗口内不变。
2. **事件归集**：事件先映射到成员再到家庭；窗口为左闭右开
   `[window_start, window_end)`。E18 时间戳恰为 2026-11-08T00:00:00Z（=window_end），
   按约定排除。
3. **金额**：只接受整数分；非整数金额报错且不计入。退款可为负，求和自然处理。
4. **记零规则**：只有覆盖完整（coverage.complete=true）且窗口内确无事件的家庭
   才记零消费；complete=false 或覆盖行缺失时指标置空（`metric_cents=null`），
   不允许记零。
5. **ITT**：全部入组家庭按**原分配 arm** 分组。未领券（exposed=false，如 H06、H10）
   仍是处理组并保留；`exposed` 只描述是否实际收到处理，从不用于选择分析队列。
   曝光与 arm 不一致只产生 warning，不阻断。

## 3. 资格检查（七类必检 + 附加质量检查）

任一 **error** 级诊断都会使 `eligible_for_causal_inference=false`：停止主效应
结论，不删除任何家庭，仅保留明确标注 `DESCRIPTIVE ONLY` 的描述统计。受影响
单位（家庭/成员/事件 ID）逐条列入诊断输出。

| code | 级别 | 含义 |
|---|---|---|
| ASSIGNMENT_MISSING / COVERAGE_MISSING | error | 名单缺失：入组家庭无分配/覆盖记录 |
| ASSIGNMENT_CROSS_ARM | error | 同家庭多条分配且跨组或跨层冲突 |
| ASSIGNMENT_STRATUM_MISMATCH / PLAN_HOUSEHOLD_CROSS_STRATUM | error | 跨层 |
| SLOT_VIOLATION | error | 层内处理家庭数 ≠ 固定名额（附观测数与要求数） |
| MEMBER_MULTI_HOUSEHOLD | error | 成员多重归属 |
| EVENT_UNKNOWN_PERSON | error | 未知成员事件 |
| EVENT_DUPLICATE_ID | error | 重复事件（同 ID 事件全部不计金额） |
| COVERAGE_INCOMPLETE / COVERAGE_UNPARSEABLE | error | 覆盖不完整/无法解析 |
| EVENT_AMBIGUOUS_PERSON | error | 多重归属成员的事件无法唯一归集，候选家庭同时标记 |
| EVENT_NON_INTEGER_AMOUNT | error | 金额非整数分 |
| PLAN_BAD_SLOTS / PLAN_BAD_WINDOW | error | plan 自身不支持完全随机化/窗口非法 |
| EXPOSED_INCONSISTENT | warning | 曝光标记与 arm 不一致（不改变 ITT 分组） |
| MEMBER_DUPLICATE_PAIR / *_UNKNOWN_HOUSEHOLD / EVENT_BAD_TIMESTAMP | warning | 不破坏设计的质量问题 |

为什么这些是阻断性的：跨组冲突、名额违反直接破坏随机化分布；成员多重归属、
未知成员、重复事件与覆盖缺测破坏结果测量的完整性。删掉这些家庭再做推断会
改变被随机化的总体，无法再声称对原入组总体有效，因此工具选择**停止主推断并
保留诊断与描述统计**，而不是静默剔除。

## 4. 估计量

层 s 的 ITT 效应估计为层内处理均值减对照均值：

    τ̂_s = mean(Y_i, i∈T_s) − mean(Y_i, i∈C_s)

总体估计按预定家庭数加权：

    τ̂ = Σ_s (n_s / N) · τ̂_s

在完全随机化下，层内均值差是该层平均效应的无偏估计，加权后即目标 estimand
（Neyman 型分层估计）。

## 5. 随机化检验

- **零假设**（plan.inference.null）：尖锐零假设——处理对任何家庭都没有效应。
- **重抽空间**：仅在原设计的 225 个合法分配上重抽，每层处理数恒为固定名额、
  层间独立；不跨层重排家庭，不对订单做普通 bootstrap（订单不是随机化单位，
  普通 bootstrap 也不遵守名额约束）。
- **统计量**：与点估计同一统计量（分层加权均值差），另对每层分别报告层统计量。
- **p 值**：双侧，p = #{|T*| ≥ |T_obs|} / #{合法分配}，观测分配自身计入，
  因此最小 p 为 1/225 而非 0。
- **精确 vs 蒙特卡洛**：合法分配数 ≤ 200,000 时精确枚举；否则用 plan.seed
  按层派生独立随机流、每层 `sample(ns, k_s)` 固定名额采样 10,000 次，
  可复现且每次抽样都通过名额合法性核对。

## 6. 95% 效应区间（常数加性效应反演）

**假设（需随结果一起声明）**：处理效应是同一常数 τ（分），对每个家庭都相同，
即 Y_i(1) = Y_i(0) + τ（尖锐、无异质性）。这是可反演的工作假设；若效应异质，
区间是该假设下的摘要，不是无假设的有限总体置信界。

**反演**：对候选 τ，把观测处理户结果减去 τ 得到零假设潜在结果 Y_i(0)，
在合法分配空间上重做双侧随机化检验；p(τ) ≥ 0.05 的 τ 构成 95% 接受集。

**搜索控制（来自 plan，非数据限制）**：范围 [-5000, 5000] 分，网格步长 50 分。
先在网格上扫描，再从网格接受集两端向外逐**整数分**检验到首个被拒点，得到
整数分辨率的精确端点（结果同时保留网格区间与整数细化区间）。

**开放边界**：若接受集在搜索边界处仍未被拒绝，对应端点按开放端点报告
（`lower_endpoint_open/upper_endpoint_open=true`、端点数为 null），并在 note
中注明“不是有限置信界，需扩大 search_bounds 重算”，绝不把未封闭边界冒充为
有限置信界。接受集不连续时也会显式标记。

## 7. 每个家庭的可追溯内容

`household_metrics` 逐户给出：所属层/arm/曝光、成员 person_id 列表、窗口内
事件 ID 与金额合计、窗口外事件 ID 与金额、重复事件 ID、归属不明事件 ID、
覆盖状态、是否记零、最终指标及状态（computed / missing_coverage /
ambiguous_attribution）。汇总区另列：未曝光家庭、零消费家庭、窗口外事件、
无可用指标家庭（区分覆盖缺失与归属不明）。观测差异（描述统计）与可支持的
实验结论（inference 区）在结构上严格分开。

## 8. 复现方式

```sh
python3 run_all_scenarios.py           # 四场景结构化结果 -> results/
python3 -m unittest discover -s tests  # 23 个单元测试
python3 tests/tiny_design_check.py     # 独立极小设计核对（自含枚举）
```

仅使用 Python 3.11 标准库。
