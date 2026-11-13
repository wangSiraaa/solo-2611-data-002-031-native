# 分层家庭随机优惠券实验：资格检查与随机化推断工具

针对按家庭分层随机、两层处理比例不同的优惠券实验。工具先判断输入是否仍符合
原分配设计，再在合格时估计家庭层面的 ITT 效应；不合格时停止主推断、只保留
明确标注的描述统计。**仅用 Python 3.11 标准库，无第三方依赖。**

## 目录结构

```
coupon_experiment/
  models.py       # 数据结构与诊断级别
  validation.py   # 七类资格检查 + 成员/事件到家庭的汇总（左闭右开窗口）
  design.py       # 分层完全随机化分配空间：合法名额枚举/固定种子采样
  inference.py    # 分层估计量、随机化检验、常数加性效应反演区间
  analysis.py     # 端到端流水线
  cli.py          # 命令行入口
tests/
  fixture_builders.py          # 测试夹具（含 12 分配与 4900 分配两套设计）
  test_validation.py           # 资格检查与汇总单元测试
  test_design_inference.py     # 分配空间、估计量、检验、区间、MC 路径测试
  tiny_design_check.py         # 独立极小设计核对（不调用包内推断函数）
run_all_scenarios.py           # 基础 + 三异常场景一键复现
METHODS.md                     # 方法说明与设计依据
results/                       # 结构化 JSON 结果（随脚本生成）
```

## 运行

```sh
# 单场景
python3 -m coupon_experiment.cli \
  --plan fixtures/plan.json \
  --assignments fixtures/assignments.csv \
  --members fixtures/members.csv \
  --events fixtures/events.csv \
  --coverage fixtures/coverage.csv \
  --out results/base.json

# 四个场景（基础 + cross_arm / incomplete_coverage / ambiguous_members）
python3 run_all_scenarios.py

# 测试与独立核对
python3 -m unittest discover -s tests
python3 tests/tiny_design_check.py
```

一次只处理一个实验；金额只接受整数分。

## 结果解读（随 results/scenario_summary.json 生成）

| 场景 | 资格 | 阻断错误 | 主推断 |
|---|---|---|---|
| base | 合格 | 无（仅 H06/H10 未领券 warning） | 总体效应 −637.5 分；精确 p=0.3467；95% 区间 [−2033, 771] 分 |
| cross_arm | 不合格 | ASSIGNMENT_CROSS_ARM, SLOT_VIOLATION | 阻止，仅描述统计 |
| incomplete_coverage | 不合格 | COVERAGE_INCOMPLETE（H05 不记零） | 阻止，仅描述统计 |
| ambiguous_members | 不合格 | MEMBER_MULTI_HOUSEHOLD, EVENT_AMBIGUOUS_PERSON | 阻止，仅描述统计 |

要点：

- 未领券家庭（H06、H10）保留在处理组参与 ITT；`exposed` 从不选择队列。
- H05 覆盖完整且无事件才记零；窗口外事件 E18（恰在 window_end）被排除并列出。
- 朴素合并两组的均值差（−416.67 分）与设计有效估计（−637.5 分）不同，
  前者只作带警告的描述对照。
- 反演区间基于常数加性效应假设；搜索边界、步长、开放端点与假设均在 JSON 中
  显式声明，详见 [METHODS.md](METHODS.md)。
