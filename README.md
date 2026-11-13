# 盲标双人标注与仲裁服务（Python 3.11）

面向客服文本的本地标注服务：两位指定标注员各自独立提交一次多标签集合，
一致则自动形成最终结果，不一致则进入待仲裁，由仲裁员显式裁决。
仅使用 Python 标准库（`http.server` + SQLite），无第三方依赖。

## 快速开始

```sh
python3.11 -m annotation_service --db .runtime/annotation.db --port 8765
```

另开终端：

```sh
# 盲标：两人对同一条目分别提交
curl -s -X POST localhost:8765/api/items/T01/submission \
  -H 'Authorization: Bearer fixture-a' -H 'Content-Type: application/json' \
  -d '{"labels":["product","delivery"]}'
curl -s -X POST localhost:8765/api/items/T01/submission \
  -H 'Authorization: Bearer fixture-b' -H 'Content-Type: application/json' \
  -d '{"labels":["delivery","product"]}'   # 集合相同、顺序无关 -> 一致通过
```

完整演示（逐个读取出口验证盲态、一致、分歧仲裁、并发、重启）：

```sh
python3.11 tools/demo.py            # 自行拉起并重启服务，64 项断言
python3.11 -m unittest discover -s tests -v   # 23 个 HTTP 端到端测试
```

接口契约见 [docs/API.md](docs/API.md)，演示数据流见 [docs/DEMO.md](docs/DEMO.md)。

## 核心设计

* **身份只来自认证信息**：`Authorization: Bearer <token>` 在服务端以 token 哈希
  查库确定操作者。请求正文若出现 `author_id`/`user_id`/`arbiter_id` 等字段，
  直接 `400 identity_in_body`，绝不接受正文指定操作者。
* **提交前彻底盲态**：标注员未提交时，列表、详情、导出和错误信息都只返回原文
  和自己的空状态；连“对方是否已提交”也不暴露。列表里该条目的 `status` 一律显示
  为 `not_submitted`。
* **提交后的开放规则明确且一致**：只有两位指定标注员都已提交后，才向标注员展示
  对方的提交与标签。最终结果只能在此之后存在，因此不会提前泄题。
* **仲裁员只看已完成条目**：仲裁列表、详情、队列只包含两份提交都齐全的条目；
  对未完成条目的查询/仲裁返回 `409 not_ready`，且错误体不含任何标签。
  仲裁入口不能用来替未提交者提前看答案。
* **提交不可变**：每个（条目, 标注员）只有一条提交。完全相同的重复提交返回原结果
  （`200` + `replayed: true`）；内容不同的第二次提交拒绝（`409 submission_immutable`）。
* **最终结果显式绑定**：标签集合相同（顺序无关、允许空集合）生成 `agreement`
  最终记录；不同则保持 `disputed`、无最终结果。只有仲裁员能生成 `arbitration`
  最终记录，并绑定当时的两个提交编号与作者；可用 `expected_submissions` 校验绑定，
  过期的绑定会被 `409 stale_binding` 拒绝。不能用条目 id 覆盖历史。
* **每条目至多一份最终记录**：`UNIQUE(item_id)` + SQLite `BEGIN IMMEDIATE`
  写事务 + 重放/冲突处理，并发提交与重复/冲突仲裁都收敛到唯一最终记录。
* **导出仅含最终结果**：只导出已一致或已仲裁的条目，带最终标签与完成来源
  （`agreement`/`arbitration`）以及两个提交的绑定；单人提交与待仲裁绝不混入。
* **重启可恢复**：状态持久化在 SQLite；启动时重新校验 fixtures 哈希并审计整条
  “提交 → 最终结果”链，发现篡改或词表漂移则拒绝启动（不做版本迁移）。

## 目录

```
annotation_service/   业务规则、HTTP 服务、SQLite 存储、fixture 加载
tests/                23 个 HTTP 端到端测试
tools/demo.py         三身份演示脚本（含并发与重启），64 项断言
docs/                 API 契约与演示数据流
fixtures/             题目提供的固定输入
.runtime/             默认数据库与日志（已 gitignore）
```
