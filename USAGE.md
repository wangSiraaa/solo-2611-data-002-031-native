# 批注迁移工具使用说明

离线工具，仅依赖 Python 3.11 标准库。输入为章节/段落 JSON 与旧批注 JSON，
不处理 PDF、OCR 或办公软件，也不需要账号或数据库。

## 命令

```sh
# 1. 比较两个版本，自动迁移可信批注，导出未解决清单
python3 -m cmtool compare --old fixtures/old.json --new fixtures/new.json \
    --comments fixtures/comments.json --out out/

# 2. 人工填写 decisions.json 后应用，重新导出
python3 -m cmtool apply-decisions --old fixtures/old.json --new fixtures/new.json \
    --comments fixtures/comments.json --decisions decisions.json --out out/

# 3. 复核输出：每条旧批注有且仅有一个去向，已确认锚点逐条重新截取核对
python3 -m cmtool verify --old fixtures/old.json --new fixtures/new.json \
    --comments fixtures/comments.json --out out/
```

输出目录包含：

- `migrated.json` — 新批注（新段落 id + 左闭右开码点范围 + 引用原文 + 依据）
- `unresolved.json` — 待人工处理项（原因、候选段落、文本证据）
- `report.html` — 只读对照页面（无脚本、无表单）
- `summary.json` — 计数与两份源文件摘要
- `decisions_report.json` —（仅 apply-decisions）每条决定的受理/拒绝记录

退出码：compare 恒为 0；apply-decisions 有决定被拒绝或决定已过期时为 1；
verify 发现不一致时为 1。

## 迁移规则要点

- 先核对批注的范围、引用、前后上下文是否与旧文一致；不一致列入未解决
  （`source_mismatch`）。原文永不改动，也不用规范化文本覆盖。
- 段落对应只看正文与章节标题，不看段落编号。相似度在 NFC 匹配视图上
  计算，坐标一律回到原文码点。重复段落靠章节上下文消歧；仍不唯一时
  保持 `ambiguous`，绝不按文件顺序取第一个。
- 拆分/合并识别为 `split_suspected` / `merge_suspected`，不自动解决。
- 引用在新段落中完整保留且位置唯一才自动迁移；多次出现时用前后上下文
  消歧，仍不唯一则 `quote_repeated`。引用被改写/删除（`quote_deleted`）、
  疑似跨段（`quote_moved_across_paragraphs`）、仅规范化后相同
  （`normalization_changed`）都保持未解决。
- 插入点批注（start==end）只靠前后上下文定位：缺失
  （`insertion_context_missing`）、找不到（`insertion_context_not_found`）、
  不唯一（`insertion_context_ambiguous`）均保持未解决。
- 偏移由文字编辑对齐推导，不按总长度差平移。

## decisions.json 格式

```json
{
  "source_digests": {"old": "<summary.json 中的 old_digest>",
                     "new": "<summary.json 中的 new_digest>"},
  "decisions": [
    {"comment_id": "C3", "action": "choose_candidate", "paragraph_id": "b5"},
    {"comment_id": "C5", "action": "set_range", "paragraph_id": "b3",
     "start": 5, "end": 10, "quote": "负责人确认"}
  ]
}
```

- 决定绑定两份源文件摘要；源文变化后摘要即不匹配，整个决定文件被拒绝。
- `choose_candidate`：在选定段落内重新定位引用，定位失败则拒绝。
- `set_range`：范围必须落在指定新段落内；若带 `quote`，必须与新文实际
  切片完全一致，否则拒绝（错误引用不会被采纳）。
- 被拒绝的决定不影响其他决定；未解决项不会被删除或计为迁移成功。

## 小样例与测试

`samples/` 下有五类小样例（移动、重复句子、插入点、组合字符、局部删改），
每个目录含 old/new/comments 三份输入与 expect.json 预期。运行测试：

```sh
python3 -m unittest discover -s tests
```
