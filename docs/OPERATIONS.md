# 旧系统 PTY 录入助手

在 Linux + Python 3.11 下，通过**真实 PTY** 操作给定旧终端程序
（`fixtures/legacy_app.py`），只更新 CSV 中已存在编码的**名称、单位、价格**。
助手**只观察 80×24 屏幕并发送按键**，不读数据库、不导入应用函数、不加隐藏接口。

## 安装

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt   # pexpect、pyte、pytest
```

## 快速开始

```sh
cp fixtures/faults.json .runtime-faults.json        # 一次性故障控制文件
mkdir -p .runtime
.venv/bin/python -m terminal_assistant \
  --csv fixtures/changes.csv \
  --db .runtime/legacy.sqlite \
  --faults .runtime-faults.json \
  --report .runtime/report.json \
  --raw-dir .runtime/raw \
  --yes
```

- `--yes`：非交互确认计划（演示/自动化用）。没有它时，程序先在真实 TTY 上打印
  计划并等待输入 `yes`，确认后才进入编辑。
- `--plan-only`：只校验 CSV、查询现值、生成计划，不进入编辑。
- `--resume <上一份 report.json>`：只处理上次报告中**未确认**的编码。
- `--retry-policy {safe-old-values,manual}`：断线后回读仍为旧值且修订号未变时，
  是否允许按策略重试（默认 `safe-old-values`）。
- `--settle` / `--read-timeout`：屏幕输出静止去抖与单步读取超时。

报告默认写到 `--report`，每一步动作、保存反馈、回读值都在其中；`--raw-dir`
保存原始 ANSI 字节，**仅作审计，不参与任何判断**。

## CSV 格式

```
catalog_code,name,unit,price,expected_old_name
A001,中华铅笔,支,1.50,铅笔
```

校验规则（连接终端前完成，失败则**根本不启动终端**）：

- 表头必须是上述五列，未知列/缺列报错；编码不得重复。
- 名称/编码/单位/预期旧名称非空，首尾无空白，不含控制字符。
- 价格必须是**非负十进制且最多两位小数**（拒绝 `-1`、`1.505`、`1.5e3`、`abc` 等）。
- 单位必须能从**编辑页可见选项**中选择，否则该条放弃、不写入。

## 处理流程

1. **校验 CSV**（无终端接触）。
2. **预查询**：经 PTY 逐条查询现值，生成逐字段差异（旧值→新值）与
   **预期旧名称不符清单**；编码查不到、查询不可用均明确列为阻塞项。
3. **用户确认计划**后才进入编辑（`--yes` 或交互输入 `yes`）。
4. **逐条编辑**：进入编辑页 → 按字段填写 → `S` 保存。
   - 编辑页新值仍标 `[尚未保存]`，**只有“保存成功”反馈才算提交**。
   - **未知弹窗**（如 `异常弹窗: E97`）立即停止：不发送任何按键、不回车、
     不发送 B、不 SIGTERM，保留现场并停止后续条目。
   - **保存冲突**：回车退出编辑 → **重新查询**并报告现值，**不覆盖**。
5. **正常保存后**：回车退出编辑 → 重新查询；现值等于计划且修订号 `+1`
   才标记 `confirmed`。
6. **成功提示前断线**：重启终端按编码回读（见下节）。
7. 报告保留：计划、动作、保存反馈、回读值、最终处置；续办只处理未确认项。

## 断线/结果未知的处置

| 重启后回读 | 处置 | 状态 |
|---|---|---|
| 现值全等于计划值 | 当前值已确认（**声称 only 当前值正确**） | `confirmed_current` |
| 仍为旧值且修订号未变 | 按 `--retry-policy` 决定是否重试；重试前会再查一次前置条件 | `confirmed_after_retry` / `retry_declined` |
| 其他值/修订号不一致 | 冲突，不覆盖，交人工 | `conflict_after_disconnect` |
| 查询不到可靠结果 | 未知 | `unknown_state` |

回读只能证明**当前值/修订号**，**不能证明恰好一次写入**；报告明确声明这一点。

## 独立校验（不属于助手）

`tools/verify_db.py` 是**独立程序**，助手从不调用它。它在运行结束后直接只读
检查 SQLite `items`/`history`：

```sh
.venv/bin/python tools/verify_db.py \
  --db .runtime/legacy.sqlite --csv fixtures/changes.csv --expect applied
```

`--expect applied|unchanged|any`，输出 JSON；`plan_valued_write_count` 是**观察到的**
已提交写入条数，同样**不等于**恰好一次写入的证明。

## 演示

```sh
.venv/bin/python demo/run_demo.py            # 运行全部真实 PTY 场景
.venv/bin/python demo/run_demo.py --only 09  # 只跑断线场景
```

每个场景在 `demo/records/<场景>/` 下产生：`assistant.log`、`report.json`、
`raw/*.ansi`、`verifier.log`、`verify.json`；`demo/records/index.json` 汇总。
演示记录与场景说明见 [docs/DEMO.md](docs/DEMO.md)。

## 测试

```sh
.venv/bin/python -m pytest tests/ -q
```

包含纯逻辑/分类器测试、屏幕解析测试、真实 PTY 端到端测试，以及
“助手不得导入 `sqlite3`/应用”的守护测试。

## 设计约束

- 只用真实 PTY（`pexpect`），屏幕用 `pyte` 重建为 80×24 后解析；
  **不拼接 ANSI 历史搜索“保存成功”**，**不用固定 sleep 脚本**。
- 每次发送后要求屏幕确实变化并静止（去抖），在识别到目标提示后才继续。
- 仅适配这一种 80×24 布局，不做多布局/跨平台扩展。
