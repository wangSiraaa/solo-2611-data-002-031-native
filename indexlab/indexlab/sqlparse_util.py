"""用 pglast 解析受控目录中的只读 SELECT，提取查询结构供候选索引生成。

提取内容（均来自真实查询结构，而非预写死列名）：
- 每表等值谓词列（col = 参数/常量）
- 每表范围谓词列（< <= > >=）
- 连接谓词（col = col）
- ORDER BY 列与方向
- 每表 SELECT 输出列（用于 INCLUDE 候选）
"""
from __future__ import annotations

from dataclasses import dataclass, field

from pglast import ast, parse_sql, visitors
from pglast.enums import A_Expr_Kind, SortByDir

RANGE_OPS = {">", ">=", "<", "<="}
EQ_OP = "="


@dataclass
class QueryStructure:
    qid: str
    tables: dict[str, str] = field(default_factory=dict)      # alias -> table
    eq_cols: dict[str, list[str]] = field(default_factory=dict)    # table -> [col,...] 保序去重
    range_cols: dict[str, list[str]] = field(default_factory=dict)
    join_pairs: list[tuple[str, str, str, str]] = field(default_factory=list)  # (t1,c1,t2,c2)
    order_cols: list[tuple[str, str, str]] = field(default_factory=list)       # (table, col, dir)
    select_cols: dict[str, list[str]] = field(default_factory=dict)
    limit: bool = False

    def eq(self, table):
        return self.eq_cols.get(table, [])

    def rng(self, table):
        return self.range_cols.get(table, [])


def _colref(node) -> tuple[str, str] | None:
    """ColumnRef -> (表或别名, 列)；无法解析返回 None。"""
    if not isinstance(node, ast.ColumnRef):
        return None
    parts = [f.sval for f in node.fields if isinstance(f, ast.String)]
    if len(parts) == 2:
        return parts[0], parts[1]
    if len(parts) == 1:
        return "", parts[0]
    return None


class _Extractor(visitors.Visitor):
    def __init__(self, qs: QueryStructure):
        self.qs = qs

    def visit_RangeVar(self, _anc, n: ast.RangeVar):
        alias = n.alias.aliasname if n.alias else n.relname
        self.qs.tables[alias] = n.relname

    def visit_A_Expr(self, _anc, n: ast.A_Expr):
        if n.kind != A_Expr_Kind.AEXPR_OP or not n.name:
            return
        op = n.name[0].sval
        l = _colref(n.lexpr)
        r = _colref(n.rexpr)
        # 连接谓词：两侧都是列
        if op == EQ_OP and l and r:
            t1 = self.qs.tables.get(l[0], l[0])
            t2 = self.qs.tables.get(r[0], r[0])
            if t1 != t2:
                self.qs.join_pairs.append((t1, l[1], t2, r[1]))
                return
        # 列 op 参数/常量（或反向）
        if l and r is None:
            self._pred(l, op, n.rexpr, False)
        elif r and l is None:
            self._pred(r, op, n.lexpr, True)

    def _pred(self, colref, op, other, flipped):
        # 另一侧必须是参数或常量（即过滤谓词），而不是另一列
        if isinstance(other, ast.ColumnRef):
            return
        table = self.qs.tables.get(colref[0], colref[0])
        col = colref[1]
        eff = op
        if flipped:  # 交换方向
            eff = {">": "<", ">=": "<=", "<": ">", "<=": ">="}.get(op, op)
        if eff == EQ_OP:
            lst = self.qs.eq_cols.setdefault(table, [])
            if col not in lst:
                lst.append(col)
        elif eff in RANGE_OPS:
            lst = self.qs.range_cols.setdefault(table, [])
            if col not in lst:
                lst.append(col)

    def visit_SortBy(self, _anc, n: ast.SortBy):
        ref = _colref(n.node)
        if not ref:
            return
        table = self.qs.tables.get(ref[0], ref[0])
        direction = "DESC" if n.sortby_dir == SortByDir.SORTBY_DESC else "ASC"
        self.qs.order_cols.append((table, ref[1], direction))


def _extract_select_cols(stmt: ast.SelectStmt, qs: QueryStructure):
    for t in stmt.targetList or ():
        rt = t
        if isinstance(rt, ast.ResTarget) and isinstance(rt.val, ast.ColumnRef):
            ref = _colref(rt.val)
            if ref:
                table = qs.tables.get(ref[0], ref[0])
                lst = qs.select_cols.setdefault(table, [])
                if ref[1] not in lst:
                    lst.append(ref[1])


def validate_readonly(stmt) -> None:
    if not isinstance(stmt, ast.SelectStmt):
        raise ValueError(f"只允许 SELECT，得到 {type(stmt).__name__}")
    if stmt.lockingClause:
        raise ValueError("不允许锁子句 (FOR UPDATE 等)")
    if stmt.withClause and any(
        not isinstance(c.ctequery, ast.SelectStmt) for c in stmt.withClause.ctes
    ):
        raise ValueError("不允许写 CTE")


def parse_query(qid: str, sql: str) -> QueryStructure:
    stmts = parse_sql(sql)
    if len(stmts) != 1:
        raise ValueError(f"{qid}: 每条查询文件只允许一条语句")
    stmt = stmts[0].stmt
    validate_readonly(stmt)
    qs = QueryStructure(qid=qid)
    # 第一遍收集表别名
    class _Tables(visitors.Visitor):
        def visit_RangeVar(self, _anc, n):
            alias = n.alias.aliasname if n.alias else n.relname
            qs.tables[alias] = n.relname
    _Tables()(stmt)
    _Extractor(qs)(stmt)
    _extract_select_cols(stmt, qs)
    qs.limit = stmt.limitCount is not None
    return qs
