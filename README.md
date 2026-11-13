# webfrontier — 配置限域的多站点目录采集器

一个可实际运行的 Python 3.11 采集器，附带**完全本地**的合成网站群。所有访问只
限于任务配置列出的本机 origin；不同端口视为不同 origin；不访问真实站点、不需要
账号。

```
src/webfrontier/
  urls.py      URL 身份规范化、链接解析、<base>、HTML 详情/链接抽取
  robots.py    RFC 9309 的 User-Agent 分组、Allow/Disallow、通配符与 $ 锚点
  http.py      流式读取并硬限制响应字节、按 origin 的请求启动间隔闸门
  store.py     SQLite frontier / 详情 / 链接图 / 逐 URL 处置 / 请求日志
  engine.py    每 origin 独立工作池、robots 门禁、逐跳重定向校验、有限重试
  site.py      合成站点群（目录、详情、重复导航、重定向、错误页、慢站、429…）
  cli.py       命令：serve-sites / init / run / status / resume / pause / export
config/demo.json   5+1 个站点的任务配置（含 robots 不可达站点）
config/nav.json    10,000 页导航图配置
scripts/demo.sh          一条命令跑完整演示 + 自动核验
scripts/verify_demo.py   以服务器访问日志核查（不依赖采集器自报）
scripts/scale_nav.py     万页规模测试：队列规模、RSS、请求数
scripts/restart_check.py SIGKILL 后恢复测试
tests/                   单元 + 集成测试
docs/DECISIONS.md        题目中需要判断处的取舍说明
```

## 快速开始（一条命令）

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
bash scripts/demo.sh
```

`scripts/demo.sh` 会：启动合成站群 → 全量采集 → 打印状态 → 导出 → 用**服务器
访问日志**做核验。演示约 20 秒。

依赖版本（`requirements.txt`）：`httpx==0.28.1`、`selectolax==0.3.27`、
`pytest==8.3.5`。也可 `pip install -e .` 后直接使用 `webfrontier` 命令。

## 命令

```sh
P=.venv/bin/python

# 1) 启动本地合成网站群（前台运行；演示脚本会自动启动/关闭）
$P -m webfrontier serve-sites --log .runtime/site_access.log --ready-file .runtime/sites-ready.json

# 2) 初始化（清空并写入种子）与开始采集
$P -m webfrontier init   --config config/demo.json
$P -m webfrontier run    --config config/demo.json          # 未完成时退出码为 1

# 3) 状态 / 暂停 / 继续
$P -m webfrontier status --config config/demo.json [--json]
$P -m webfrontier pause  --config config/demo.json
$P -m webfrontier resume --config config/demo.json

# 4) 导出：details / links / urls / attachments / requests / dispositions / summary
$P -m webfrontier export --config config/demo.json --outdir .runtime/export
```

`run --reset` 会丢弃旧 frontier；`init` 默认 `--reset`。任务只有在所有可处理地址
都进入终态时才显示 `complete`；预算停止会明确给出未处理数量并保持 `incomplete`。

## 配置

```jsonc
{
  "user_agent": "CoopDirectoryBot/1.0 (+local synthetic directory crawler)",
  "database": "../.runtime/demo-crawl.db",
  "origins": [
    {"origin": "http://127.0.0.1:8801", "path_prefixes": ["/"], "seeds": ["/"],
     "concurrency": 2, "min_request_interval": 0.12}
  ],
  "limits": {
    "max_depth": 3, "page_budget": 420, "max_redirects": 4,
    "per_origin_concurrency": 2, "min_request_interval": 0.12,
    "max_body_bytes": 262144, "timeout_seconds": 6.0,
    "max_attempts": 3, "retry_backoff_seconds": [0.4, 1.0, 2.0]
  }
}
```

* origin 必须写成规范形式（协议/主机小写、去默认端口）；种子可以是 `origin + 路径`。
* 每个 origin 可单独设置并发和最小请求间隔；`limits` 中为默认值。
* `HttpClient` 在每次请求（包括每个重定向跳）发出前再校验一次 origin 是否在配置
  内，两道防线。

## 关键语义

**URL 身份**（`urls.canonical`，与 `urls.request_url` 分列保存）
移除 fragment；协议/主机小写；去默认端口；解析点段；空路径补 `/`；查询串**逐
字节保留**（顺序、重复参数、有无 `?` 都是身份的一部分）；只解码 unreserved 的
百分号转义，`%2F` 永远不会变成 `/`。

**详情**
声明了稳定 `item_id`（`<meta name="item_id">` 或 `data-item-id`）的成功 HTML 页面
才写入 `details`；`item_id` 唯一索引 + 最终响应身份共同保证：多个入口（fragment、
重复查询、跳转别名、跨站链接）只产生一条详情，并保留真实响应地址和来源页。

**robots.txt**
每个 origin 第一次需要时抓取并**固定**在 SQLite；重启/继续时复用。产品 token 分组
（大小写不敏感前缀匹配）、多个匹配组合并、无匹配用 `*`、再没有则全允许；最长匹配
优先，等长 Allow 胜出；`*` 通配、行末 `$` 锚定，中间的 `*`/`$` 按字面量；200 解析、
4xx 视为无规则、429/5xx/连接失败重试后**整站暂缓且绝不放行**。robots.txt 本身隐式
允许。禁抓路径在领取任务时即终止，**不产生任何 HTTP 请求**。

**重定向**
不交给 httpx 自动跟随。每一跳都先检查范围（origin + 路径前缀）和目标站 robots，
通过后才发请求；跨站跳转按两个 origin 分别记账并分别受间隔约束；跳转环、超过
`max_redirects`、目标已终态都直接终止。被拒目标写入 `out_of_scope` /
`robots_denied` 处置记录，但从未收到页面请求。

**预算、重试、暂停、恢复**
页面预算按页面 GET 计数（含跳转后的新目标页）；耗尽时剩余 URL 标记 `over_budget`
并报告未处理数量，任务不显示完成。连接失败、超时、429、500/502/503/504 进入
`retry_wait`（带 `not_before` 和 `Retry-After`，上限 30s），不会被标成 `done`。
暂停/重启后 frontier 中残留的 `in_flight` 一律改回 `pending`（记录原因），绝不
视为成功。

**内存与慢站**
待处理全集只存在 SQLite；每个工作线程内存里最多一条已领取记录。每个 origin 有
独立工作池和独立间隔闸门，慢站只会占用它自己的槽位。响应体按实际读取字节硬截断
（同时参考 Content-Length 提前判定）。

## 合成站群

默认端口：A=8801 目录/详情/跳转/禁抓路径，B=8802 跨站链接与跨站跳转（含跳到
禁抓路径和范围外 `:8899`），C=8803 可控制屏障的慢响应，D=8804
500/503/429/二进制/超大/坏编码/长跳转链，E=8805 跳转环，8806 robots.txt 恒 500，
8811 万页导航图，8899 故意在配置之外的陷阱站（同样记录访问日志）。A 站 robots
包含通配符组、专属组、`*` 与 `$` 规则，用来覆盖 RFC 9309 的分组与匹配边界。

所有服务器把每个请求（含 robots.txt）写入 JSONL 访问日志，核验脚本只信这份日志。

## 验证结果（本机实测）

`bash scripts/demo.sh` → **全部核验通过**：

* 禁抓路径 `/private/hidden`、`/quarantine/rack.html`、`/public/secret`、
  `/go/secret`、`/blocked/hidden` 服务器命中数均为 **0**；陷阱站 `:8899` 命中 0；
  robots 不可达站点只收到 3 次 `/robots.txt`，页面 0 次。
* 每个 origin 服务器观测到的相邻请求最小间隔 ≥ 配置值（实测 ≈0.167s vs 0.12s，
  含 0.05s 抖动余量）。
* 慢站停顿时其他 origin 仍在推进（窗口内 A/B/D/E/robotsfail 均有请求）。
* 独立图遍历的 16 条详情与库中 `item_id` 完全一致、无重复；每条详情都有真实响应
  URL 与来源页。
* frontier 全部终态；重试耗尽、robots 拒绝、跳转别名、范围外、超深度、非 HTML、
  超大、二进制、解析失败、跳转环、超跳转数均有记录。

万页导航图（`scripts/scale_nav.py`，4 并发、64 KiB 响应上限）：

| 指标 | 值 |
|---|---|
| 页面请求数 | 10,102（10,000 nav + 101 详情 + 1 根） |
| 链接边（去重后） | 216,119 |
| frontier 行数 | 10,102 |
| 运行中最大 RSS | ≈41 MiB（全程基本持平） |
| 完成耗时 | ≈100 s（≈100 页/s） |
| 最大 pending | 1,036（队列主要在 SQLite，不在内存） |

`scripts/restart_check.py`（SIGKILL → resume）：杀掉时约 1,016 页已完成并保留；
注入一条“请求中”残留记录后恢复，启动日志显示
`requeued interrupted rows on start: 1`，该行被重新请求而非当作成功；恢复后
10,102 行全部终态，已完成页面无一条被改变，也没有被重算成新页面。

## 测试

```sh
.venv/bin/pytest -q     # URL/robots 单元测试 + 真实站群集成测试
```

## 说明

完整的设计取舍（身份规则、robots 语义、预算、重试计数、恢复规则）见
[`docs/DECISIONS.md`](docs/DECISIONS.md)。所有运行数据在 `.runtime/`（已被
`.gitignore` 忽略）。
