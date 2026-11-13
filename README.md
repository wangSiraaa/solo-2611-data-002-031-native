# 遗留报表站 · 本地表单采集助手（Python 3.11）

本项目包含两部分：

| 组件 | 路径 | 说明 |
| --- | --- | --- |
| 合成报表站 | `report_site/` | 有登录、选择机构、报表参数、预览、下载入口的遗留风格站点；SQLite 存储；两套结构不同的页面变体 |
| 采集助手 | `report_helper/` | 只通过站点公开网页请求取数；解析表单、解释选项、恢复会话、校验下载 |
| 配置样例 | `config/` | 站点地址/账号、表单与控件名称、报表任务清单；**不含** CSRF 值、Cookie、下载地址或机器选项值 |
| 验证工具 | `tools/` | 多场景端到端验证、与站点独立真值逐份核对 |
| 运行脚本 | `scripts/` | 启动站点、跑完整演示 |

**依赖：仅 Python 3.11 标准库**（`http.client`、`html.parser`、`sqlite3`、`csv`、
`json`），无需 `pip install`。详见 `requirements.txt`。不需要浏览器、账号或外部站点。

> HTML 解析使用标准库 `html.parser`（通用 DOM 语义），保持零依赖、便于审计。

---

## 1. 快速开始

```bash
cd /workspace

# 终端 1：启动合成站点（变体 a，端口 8765）
bash scripts/run_site.sh a 8765

# 终端 2：执行任务清单
python3 -m report_helper.cli run \
  --config config/site.json \
  --tasks  config/tasks.example.json \
  --state-dir .runtime/state \
  --trace-dir .runtime/trace

# 与站点独立真值逐份核对
python3 tools/verify_reports.py \
  --truth http://127.0.0.1:8765/__truth.csv \
  --state-dir .runtime/state

python3 -m report_helper.cli status --state-dir .runtime/state
```

一键演示（自动启动站点、跑任务、核真值、打印状态）：

```bash
bash scripts/run_demo.sh a
bash scripts/run_demo.sh b     # 换一套完全不同的页面结构，结果相同
```

演示账号：`ops` / `demo-pass`（合成站点固定演示账号）。

### 命令行入口

| 命令 | 作用 |
| --- | --- |
| `python3 -m report_helper.cli run --config ... --tasks ...` | 执行清单；已完成且核对通过的自动跳过，**只重做未完成任务** |
| `python3 -m report_helper.cli status` | 查看各任务当前阶段与已成功报表，显示 `save_state` |
| `python3 -m report_helper.cli trace [--task X] [--kind E]` | 打印轨迹（阶段/方法/地址/选项解释/重定向/恢复原因） |
| `python3 -m report_helper.cli events` | 按阶段/事件统计轨迹 |
| `python3 tools/verify_reports.py --truth URL --state-dir DIR` | 与站点真值逐份核对机构/月份/行数/金额 |
| `python3 tools/verify_scenarios.py` | 两变体 + 各类故障的端到端断言 |
| `python3 tests/test_units.py` | 解析、表单序列化、配置校验、轨迹脱敏等单元测试 |

### 站点启动

```bash
bash scripts/run_site.sh              # variant a, port 8765, unscoped
bash scripts/run_site.sh b 8800       # variant b, port 8800
bash scripts/run_site.sh b 8800 scoped
python3 -m report_site.server --all-faults   # 全部故障开启（调试用）
```

入口 `http://127.0.0.1:8765/`；真值导出 `http://127.0.0.1:8765/__truth.csv`
（**仅供事后核对**；助手运行时不访问）。

---

## 2. 站点结构（两套语义相同、结构不同的页面）

| 项目 | 变体 A | 变体 B |
| --- | --- | --- |
| 登录页 | `/login` | `/portal/index.cgi` |
| 登录 action | `login`（相对） | `auth`（相对 → `/portal/auth`） |
| 选择机构页/action | `/select-org` / `select-org` | `/portal/org` / `org` |
| 参数页/action | `/params` / `params` | `/portal/setup` / `setup` |
| 下载 action | `download/<handle>` | `/portal/dl/<handle>`（根绝对） |
| CSRF 字段名 | `_csrf` | `csrf_guard` |
| 提交按钮名 | `submitter` | `op` |
| 机构机器值 | `org-0..3` | `tenant/0..3` |
| 月份机器值 | `per-2023-01` | `2023/01` |
| 类型机器值 | `rt-900..903` | `kind/100..103` |
| 隐藏字段 | 较少、单处 | 拆两处，另有 `session_tag`/`scope`/`trail`/`org_echo` |
| 无关表单 | 无 | 每页多一个 `newsletter` 表单 |

两套页面中**表单名、控件名、选项显示文本完全相同**：
`login` / `select_org` / `report_params` / `download`；
`username`、`password`、`org`、`month`、`rtype`、`columns`。
助手只依赖这些语义名 + 显示文本，所以同一份配置在 A、B 上都得到相同业务报表
（`verify_scenarios.py` 的 `happy-a` / `happy-b` 逐份核对）。

### 控件规则（已实现并单测）

* 支持 `text` / `password` / `hidden` / `select` / `checkbox` / `submit`；
* 隐藏字段值**只取当前页面**；每次读取/成功提交业务表单都换新 CSRF；
* 同名 checkbox 可出现多个，只提交选中的值；`disabled` 控件一律不提交；
* 提交按钮 name/value 来自页面（各变体不同）；
* 选项按**显示文本**定位，再从当前页面取机器值；
* 表单 action 可为相对路径，按浏览器规则相对**请求 URL**解析；
* 缺失控件或多义定位 → 记录所在页面（URL + 结构摘要）并停止该报表。

## 3. 配置格式

`config/site.json`：

```json
{
  "base_url": "http://127.0.0.1:8765",
  "login_url": "/",
  "credentials": {"username": "ops", "password": "demo-pass"},
  "forms": {
    "login": "login", "org": "select_org",
    "params": "report_params", "download": "download",
    "username_control": "username", "password_control": "password",
    "org_control": "org", "month_control": "month", "type_control": "rtype",
    "section_control": "columns", "section_labels": ["显示摘要"]
  },
  "retry_limit": 2
}
```

配置只接受"表单名 + 控件名 + 选项显示值"。加载时**主动拒绝**
`csrf` / `cookie` / `download_url` / `machine_value` / `sid` 等键（抛 `ConfigError`），
从制度上避免把某次运行的令牌、地址或机器选项值写死。

`config/tasks.example.json`：每项含 `org_label`（显示名）、`month_label`、`report_type`，
可选 `name`（状态目录名）。任务只读，不修改业务数据。

## 4. 会话、阶段识别与恢复

* 服务端下发会话 Cookie；domain/path 生效范围与失效由真实 HTTP 交互决定
  （助手实现小型 RFC 6265 风格 jar，按 path 排序、按 Max-Age 删除）。
* 助手按**页面结构**识别阶段（login / org / params / preview / error），
  不看状态码：站点可能在任意阶段返回 **200 的登录 HTML**。
* 判定会话失效后：
  1. 记录 `session_expired_detected`（含页面 URL、解析出的阶段与状态码）；
  2. 丢弃旧会话（Cookie、旧 CSRF 字段、预览页、下载链接**全部不带入新会话**）；
  3. 从**任务清单里该任务自己的**机构/月份/类型重新进入；
  4. 受 `retry_limit` 限制；超限后该任务失败，其他任务继续。

每个任务独立逻辑上下文（独立 `Session` + 独立状态目录），
不会把上一个任务的机构或参数带入下一个任务。

## 5. 预览与下载核对

预览页展示：查询标识、机构、月份、报表类型、列名、行数、金额合计。

下载前/保存前助手核对：

1. 预览侧：机构/月份/类型与任务请求一致；声明行数 = 预览表实际行数；有列名；
2. 响应侧：HTTP 状态；`Content-Length` 与实际流式收到字节（截断检测）；
3. **Content-Type 必须是 CSV**（`text/csv` / `application/csv` / `application/octet-stream`）；
4. 非 HTML 登录页 / 非错误页；`X-Query-Id`（若有）与预览查询标识一致；
5. CSV 列名 = 预览列名；数据行数 = 预览行数；逐行金额可解析；
6. CSV 金额合计 = 预览合计。

任一项失败都不会保存成功报表。下载按**流**写入任务暂存区
`<state>/<task>/.partial/download.bin`，状态先记为 **`downloaded_file`**；
只有 1–6 全部通过才原子提升为 `<task>/report.csv` 并记为 **`completed`**。

## 6. 执行轨迹与页面摘要

每次运行在 `--trace-dir` 生成：

* `trace.jsonl`：逐事件，含 `stage`、`event`、`method`、`url`、状态码、
  `content_type`、`bytes`/`declared_bytes`/`truncated`、重定向、选项解释
  （显示文本 → 机器值）、表单字段（密码替换 `***`）、恢复原因/次数、失败页面阶段；
* `pages/<task>-<seq>-<stage>.json`：页面结构摘要（标题、标题层级、表单名/方法/action、
  控件名/类型/选项显示文本与机器值/是否禁用），**不含完整账号页内容**；
* `summary-<run>.json`：本次运行各任务最终状态。

轨迹**不写 Cookie、不写密码**（单测与场景后全量扫描确认）。

## 7. 状态与"只重做未完成任务"

`<state-dir>/<task>/state.json` 关键字段：

| 字段 | 含义 |
| --- | --- |
| `stage` | `pending/login/org/params/preview/download/done/failed` |
| `save_state` | `none` / `downloaded_file` / `completed` / `failed` |
| `qid` | 与预览/下载同源的查询标识 |
| `preview_*` / `csv_*` | 预览与 CSV 的列名/行数/合计，用于事后核对 |
| `final_file` / `sha256` | 已核对通过的报表与摘要 |
| `recoveries` / `last_error` / `diagnostics` | 恢复次数与可读诊断 |

再次运行 `cli run`：仅当历史为 `done` + `completed`、文件存在、且机构/月份/类型未变
时才跳过；其余（含失败）全部重做。

## 8. 故障场景与验证

`tools/verify_scenarios.py` 为每个场景启动全新站点、跑助手、取真值、逐项断言
（包括"失败任务不得留下可读取的 report.csv"）。

| 场景 | 注入 | 期望 |
| --- | --- | --- |
| `happy-a` / `happy-b` | 无 | 两套结构 → 相同报表，全部对真值通过 |
| `expire-*-a/b` | 各阶段会话失效，返回 **200 登录 HTML** | 判定失效 → 重建会话 → 从清单重进 → 完成 |
| `csrf-rotate-b` | 每次渲染/提交换 CSRF | 每次重读当前令牌，全部完成 |
| `mismatch-download-a` | 下载身份与预览不符 | 列/行/合计或 `X-Query-Id` 不符 → 不保存，重试后完成 |
| `bad-content-type-b` | CSV 内容但 `Content-Type: text/html` | 拒绝保存，重试后完成 |
| `truncated-download-a` | 声明长度但流被截断 | 截断检测 → 不保存，重试后完成 |
| `persistent-truncation-a` | 每次下载都截断 | 全部失败；**无 report.csv**；站点日志有记录 |
| `missing-option-b` | 任务请求不存在的机构显示名 | 仅该任务失败（含所在页面与可用选项），其余完成 |

```bash
python3 tools/verify_scenarios.py
python3 tools/verify_scenarios.py --only expire-org-a
```

产物在 `.runtime/verify/<scenario>/`（`helper_stdout.txt`、站点日志、状态、轨迹）。

### 请求日志与真值核对

* 站点把每次请求写入自身 SQLite 的 `request_log`
  （阶段、方法、路径、状态、是否带会话、备注、查询标识、重定向、内容类型、字节数）。
* 站点另有**独立真值表** `truth_export`（确定性业务生成器生成，与渲染路径分开），
  通过 `/__truth.csv` 导出。
* `tools/verify_reports.py` 把每份报表与真值逐份比对机构、月份、行数、金额，
  并要求记录查询标识。

查看站点请求日志：

```bash
sqlite3 .runtime/site_a.sqlite \
  "select ts,phase,method,path,status,note,qid,bytes from request_log order by id desc limit 20;"
```

失败验证：`request_log` 可见 `download-body` 虽为 200 但 `note` 标出
`truncated body` / `identity mismatch`，而对应任务目录**没有** `report.csv`。

## 9. 目录

```
report_site/      合成站点（server, pages, catalog, domain, store, htmlbuild）
report_helper/    采集助手（cli, engine, htmlforms, httpx, stages, state, trace, config）
config/           site.json（站点+表单语义），tasks.example.json（任务清单）
tools/            verify_scenarios.py（端到端故障矩阵），verify_reports.py（真值核对）
scripts/          run_site.sh / run_demo.sh
tests/            test_units.py
```

## 10. 设计取舍

* **零第三方依赖**：Cookie 作用域、重定向策略、流式截断检测都显式可审计，
  使用 `http.client` 自建会话而非 `requests`。
* **业务生成器与渲染分离**：`domain.py` 是唯一数据源，站点把结果物化进
  `truth_export` 供事后核对；助手从不读该表，只解析页面与下载响应。
* **不把 200 当成功**：所有成功判定基于页面语义与内容。
* **脱敏**：轨迹与页面摘要不写 Cookie/密码。
