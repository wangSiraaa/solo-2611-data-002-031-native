# 数据工程独立任务初始工作区

本仓库用于独立的数据工程任务。本次需求以当前会话提供的完整题面为准；初始快照没有业务实现、测试答案或业务数据。

## 环境

- Linux、Python 3.11，提供 pip 和 venv。
- PostgreSQL 16 的 `initdb`、`pg_ctl`、`psql`、`createdb` 已加入 PATH。
- 当前为普通用户，可在自己拥有的目录初始化独立数据库。
- Python 标准库包含 SQLite；根据题目选择实际需要的技术。

原生环境初始化示例见 [environment/README.md](environment/README.md)。请将依赖、数据库、运行日志及临时验证数据放在忽略目录中，并为最终实现提供清楚的运行说明。

---

# logseg：两版规则下的全局一致日志切分与改版对比

纯 Python 3.11 标准库实现（无第三方依赖，无需数据库）。在冻结日志源上，按两版
有界事件语法（`fixtures/templates-a.json` / `templates-b.json`）寻找**覆盖整块
所有行**的事件切分，并对比两版差异。只做有限语法、小数据、两版配置。

## 语义要点

- **核验先行**：读取前核对清单声明的字节长度与 SHA256，不符则该源失败，绝不带伤解析。
- **块与候选**：空行划定独立块（仅完全空的行算空行）；块内时间戳行（`YYYY-MM-DDTHH:MM:SSZ `）
  可开启 1..`max_candidate_lines` 行的连续候选。候选逻辑消息只去掉**首个**时间戳前缀与
  末尾行终止符，内部时间戳行与换行原样保留；对每个候选用全部规则做 `re.fullmatch`（DOTALL）。
- **全局一致**：不做局部最长/最短/首条规则选择。恰好 1 个覆盖整块的解释才确认事件；
  0 个为 UNKNOWN（保留候选证据）；≥2 个为歧义（保留至少两套完备见证；
  同边界不同规则也算歧义）。
- **尾块隔离**：`last_event_complete` 为 false / 未声明时，尾块（或末个 JSONL 对象）
  分别隔离为 `incomplete` / `unknown_completeness`，不做候选分析；声明 true 不免除
  语法检查，与是否有末尾换行无关；已完整的前块仍可分析。
- **JSONL**：每个对象即独立边界，只对 `message` 字段做 fullmatch，绝不跨对象拼接。
- **转换在切分之后**：确认解释后才按声明类型转换；`string` 保留前导零，`decimal`
  为有限精确十进制（JSON 中以其精确文本表示），失败保留原值与原因，转换成败不暗选切分。
- **偏移即字节**：所有范围为半开字节偏移 `[start, end)`，由原始字节计算，
  UTF-8 字符位置不冒充字节位置。
- **两版对比**：先按原始字节覆盖对应（一对一 / 拆分 / 合并 / 无法唯一对应），
  只有边界相同才逐字段比较模板、类型和值；事件 id 不跨版本沿用。

## 运行命令

```sh
# 一键：核验 + 两版切分 + 对比，写出 outputs/{seg-a,seg-b,comparison}.json
python3 -m logseg run --fixtures fixtures --out outputs

# 分步
python3 -m logseg verify  --fixtures fixtures
python3 -m logseg segment --fixtures fixtures --rules fixtures/templates-a.json --out outputs/seg-a.json
python3 -m logseg segment --fixtures fixtures --rules fixtures/templates-b.json --out outputs/seg-b.json
python3 -m logseg compare --a outputs/seg-a.json --b outputs/seg-b.json --out outputs/comparison.json

# 测试（含独立枚举核对：不调用主算法的暴力枚举器逐块比对）
python3 -m unittest discover -s tests -v
```

## 样例（取自真实输出）

**1. 局部两候选、全局唯一**（`grammar.log` 块0，A 版）：行 1 处 `head`（1 行）与
`job`（3 行）两个候选都命中，但 `head` 之后 `part value=7` 无法开启任何解释，
只有 `[job]` 覆盖整块 → 唯一确认：

```json
{"lines": [1, 1], "matched_rules": ["head"], "message": "BEGIN task=A"}
{"lines": [1, 3], "matched_rules": ["job"],  "message": "BEGIN task=A\n2026-11-03T11:00:01Z part value=7\n..."}
→ confirmed: {"id": "grammar.log#e1", "rule": "job", "lines": [1, 3], "span": {"start": 0, "end": 100}}
```

**2. 全局确有多解**（`grammar.log` 块1，A、B 两版同样）：`[short_job]` 与
`[head, end]` 都覆盖整块 → 歧义，保留两套见证：

```json
"witnesses": [
  [{"rule": "head", "lines": [5, 5]}, {"rule": "end", "lines": [6, 6]}],
  [{"rule": "short_job", "lines": [5, 6]}]
]
```

**3. 规则拆合**（`grammar.log`，对比报告）：A 版 `pair` 一条事件在 B 版拆成两条
`pair_part`；A 版 `phase_begin`+`phase_done` 两条在 B 版并成一条 `phase`：

```json
{"kind": "split", "span": {"start": 168, "end": 244}, "detail": "1 event in A -> 2 events in B"}
{"kind": "merge", "span": {"start": 245, "end": 320}, "detail": "2 events in A -> 1 event in B"}
```

**4. 类型变化与转换失败可见**（`no_newline.log` / `orphan.log`，对比报告）：
`amount` 由 string 变为 decimal，`"0.00"` 精确保留；`amount=bad` 转换失败时
原值与原因都留在报告里，且切分与 A 版完全一致：

```json
{"field": "amount", "kind": "type_changed",
 "a": {"type": "string",  "ok": true,  "value": "0.00"},
 "b": {"type": "decimal", "ok": true,  "value": "0.00"}}
{"field": "amount", "kind": "type_changed",
 "a": {"type": "string",  "ok": true,  "value": "bad"},
 "b": {"type": "decimal", "ok": false, "value": "bad", "error": "cannot convert 'bad' to decimal"}}
```

## 代码结构

```
logseg/
  manifest.py   清单读取与长度/SHA256 核验
  rules.py      模板配置加载与自洽校验（fullmatch_dotall、字段=分组、类型白名单）
  sources.py    字节级物理行/空行块切分（偏移全部来自原始字节）
  segment.py    候选生成、完备解释回溯搜索、text/jsonl 切分、事件与证据输出
  convert.py    string/int/decimal 转换（失败保留原值与原因）
  compare.py    字节覆盖对齐（1:1/拆分/合并/无法唯一对应）与同边界字段对比
  report.py     编排：核验 -> 两版切分 -> 对比
  cli.py        verify / segment / compare / run 子命令
tests/
  test_logseg.py            单元测试（状态矩阵、见证、偏移、转换、对比、篡改失败）
  test_independent_enum.py  独立枚举核对（自含预言机，不调用主算法）
```
