"""只读对照 HTML 报告：静态页面，无脚本、无表单。"""
from __future__ import annotations

import html


def _mark(text: str, s: int, e: int) -> str:
    if not (0 <= s <= e <= len(text)):
        return html.escape(text)
    return (
        html.escape(text[:s])
        + "<mark>"
        + html.escape(text[s:e])
        + "</mark>"
        + html.escape(text[e:])
    )


def _esc(s) -> str:
    return html.escape(str(s))


def render(old_doc, new_doc, migrated, unresolved) -> str:
    mig_rows = []
    for m in migrated:
        old_para = old_doc.by_id.get(m["migration"]["old_paragraph_id"])
        new_para = new_doc.by_id.get(m["paragraph_id"])
        os_, oe = m["migration"]["old_range"]
        old_view = _mark(old_para.text, os_, oe) if old_para else "（段落缺失）"
        new_view = _mark(new_para.text, m["start"], m["end"]) if new_para else "（段落缺失）"
        mig_rows.append(
            "<tr>"
            f"<td>{_esc(m['comment_id'])}</td>"
            f"<td>{old_view}<br><small>{_esc(old_para.chapter_title if old_para else '')} "
            f"/ {_esc(m['migration']['old_paragraph_id'])} [{os_},{oe})</small></td>"
            f"<td>{new_view}<br><small>{_esc(new_para.chapter_title if new_para else '')} "
            f"/ {_esc(m['paragraph_id'])} [{m['start']},{m['end']})</small></td>"
            f"<td>{_esc(m['migration']['kind'])}</td>"
            f"<td>{_esc('；'.join(m['migration']['evidence']))}</td>"
            "</tr>"
        )
    unr_rows = []
    for u in unresolved:
        old_para = old_doc.by_id.get(u["old_paragraph_id"])
        os_, oe = u["old_range"]
        old_view = _mark(old_para.text, os_, oe) if old_para else "（段落缺失）"
        cands = u.get("candidates") or []
        cand_html = "<br>".join(
            _esc(f"{c['pid']}（{c['chapter_title']}）score={c['score']} "
                 f"sim={c['text_sim']} exact={c['exact']}：{c['preview']}")
            for c in cands
        ) or "（无候选）"
        unr_rows.append(
            "<tr>"
            f"<td>{_esc(u['comment_id'])}</td>"
            f"<td>{old_view}<br><small>{_esc(u['old_paragraph_id'])} [{os_},{oe})</small></td>"
            f"<td>{_esc(u['reason'])}</td>"
            f"<td>{_esc(u.get('detail', ''))}</td>"
            f"<td>{cand_html}</td>"
            "</tr>"
        )
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>批注迁移对照（只读）</title>
<style>
body {{ font-family: sans-serif; margin: 2em; }}
table {{ border-collapse: collapse; width: 100%; margin-bottom: 2em; }}
th, td {{ border: 1px solid #999; padding: 6px 8px; vertical-align: top; text-align: left; }}
th {{ background: #eee; }}
mark {{ background: #ffe58f; padding: 0 1px; }}
small {{ color: #555; }}
</style>
</head>
<body>
<h1>批注迁移对照（只读）</h1>
<p>old 摘要：<code>{old_doc.digest}</code><br>
new 摘要：<code>{new_doc.digest}</code><br>
已迁移 {len(migrated)} 条，待人工处理 {len(unresolved)} 条。</p>
<h2>已迁移</h2>
<table>
<tr><th>批注</th><th>旧文（高亮为原范围）</th><th>新文（高亮为新范围）</th><th>方式</th><th>依据</th></tr>
{''.join(mig_rows)}
</table>
<h2>待人工处理</h2>
<table>
<tr><th>批注</th><th>旧文（高亮为原范围）</th><th>原因</th><th>说明</th><th>候选</th></tr>
{''.join(unr_rows)}
</table>
</body>
</html>
"""
