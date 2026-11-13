"""在同一测试库的目标 schema 中验证导出结果。

检查项：
1. 列（名字/类型/可空）、主键/外键、列默认值与源对象语义等价；
2. 序列声明属性与 OWNED BY 归属正确，且不携带源序列业务进度；
3. 视图依赖只指向目标 schema，不存在任何指向源 schema 的对象引用；
4. 无关表与业务行未带入；
5. 插入少量新行，验证 nextval 默认取号与视图可查询，业务字符串保持原样。
"""

import psycopg2
import pglast
from pglast import ast

from .catalog import CatalogReader
from .model import deparse_expr, quote_qualified, remap_sql


class VerificationFailure(AssertionError):
    pass


def _check(condition, message, failures):
    if not condition:
        failures.append(message)


def _schema_refs_in_sql(sql: str, schema_name: str):
    """用 AST 找出 SQL 中真正引用指定 schema 的对象名（不看字符串常量内容）。"""
    refs = []
    parsed = pglast.parse_sql(sql)

    def walk(node):
        if not isinstance(node, ast.Node):
            return
        if node.__class__.__name__ == "RangeVar" and node.schemaname == schema_name:
            refs.append(f"RangeVar:{node.relname}")
        if node.__class__.__name__ == "TypeCast":
            tn = node.typeName
            if tn and [s.sval for s in tn.names] == ["regclass"]:
                arg = node.arg
                if (
                    isinstance(arg, ast.A_Const)
                    and isinstance(arg.val, ast.String)
                    and arg.val.sval.startswith(f'"{schema_name}"')
                ):
                    refs.append(f"regclass:{arg.val.sval}")
        for member in node:
            value = getattr(node, member)
            if isinstance(value, tuple):
                for item in value:
                    walk(item)
            else:
                walk(value)

    for stmt in parsed:
        walk(stmt)
    return refs


def _canonical_default(expr, source, target):
    """把源默认值按同样的 AST 映射规则规范化后再比较。"""
    rewritten, _ = remap_sql(f"SELECT {expr}", source, target)
    body = rewritten.strip()
    assert body.startswith("SELECT ")
    return deparse_expr(body[len("SELECT "):].rstrip(";"))


def verify(dsn: str, manifest: dict, smoke_test: bool = True):
    source = manifest["source_schema"]
    target = manifest["target_schema"]
    reader = CatalogReader(dsn)
    failures = []
    report = {"checks": [], "smoke": None}

    def ok(message):
        report["checks"].append({"status": "pass", "message": message})

    def find_oid(schema, name):
        return reader.query_one(
            "SELECT c.oid FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
            "WHERE n.nspname=%s AND c.relname=%s",
            (schema, name),
        )["oid"]

    inventory = manifest["structure_inventory"]
    table_names = [o["name"] for o in inventory if o["kind"] == "ordinary_table"]
    seq_names = [o["name"] for o in inventory if o["kind"] == "sequence"]
    view_names = [o["name"] for o in inventory if o["kind"] == "view"]

    try:
        # 1) 对象集合：目标只含闭包内的表/序列/视图
        target_rels = {
            (r["relkind"], r["relname"])
            for r in reader.query(
                "SELECT c.relkind, c.relname FROM pg_class c "
                "JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE n.nspname = %s AND c.relkind IN ('r','S','v')",
                (target,),
            )
        }
        expected = ({("r", n) for n in table_names}
                    | {("S", n) for n in seq_names}
                    | {("v", n) for n in view_names})
        _check(target_rels == expected,
               f"对象清单不一致：多出 {target_rels - expected}，缺少 {expected - target_rels}",
               failures)
        ok("目标 schema 对象集合与依赖闭包一致，无关表未带入")

        # 业务行未带入
        for name in table_names:
            count = reader.query_one(
                f"SELECT count(*) AS n FROM {quote_qualified(target, name)}"
            )["n"]
            _check(count == 0, f"表 {name} 带入了 {count} 行业务数据", failures)
        ok("所有目标表初始为空，业务行未复制")

        # 2) 列、默认值、约束逐项等价
        for name in table_names:
            src_oid, dst_oid = find_oid(source, name), find_oid(target, name)
            src_cols = reader.table_columns(src_oid)
            dst_cols = reader.table_columns(dst_oid)
            _check(
                [(c["attname"], c["formatted_type"], c["attnotnull"]) for c in src_cols]
                == [(c["attname"], c["formatted_type"], c["attnotnull"]) for c in dst_cols],
                f"表 {name} 列定义（名/类型/可空）不一致", failures,
            )

            src_def = {r["attname"]: r["default_expr"] for r in reader.column_defaults(src_oid)}
            dst_def = {r["attname"]: r["default_expr"] for r in reader.column_defaults(dst_oid)}
            _check(set(src_def) == set(dst_def), f"表 {name} 默认值列集合不一致", failures)
            for col, expr in src_def.items():
                _check(
                    _canonical_default(expr, source, target) == deparse_expr(dst_def[col]),
                    f"表 {name} 列 {col} 默认值不等价", failures,
                )

            _check(
                _normalize_sig(_constraint_sig(reader, src_oid, source))
                == _normalize_sig(_constraint_sig(reader, dst_oid, target)),
                f"表 {name} 主键/外键签名不一致", failures,
            )
        ok("列、可空性、默认值（含 nextval regclass）、主键与外键语义等价")

        # 3) 序列属性、OWNED BY、业务进度
        for name in seq_names:
            src_seq = reader.sequence_detail(find_oid(source, name))
            dst_seq = reader.sequence_detail(find_oid(target, name))
            for key in ("seqtype", "seqstart", "seqincrement", "seqmin", "seqmax",
                        "seqcache", "seqcycle"):
                _check(src_seq[key] == dst_seq[key],
                       f"序列 {name} 属性 {key} 不一致", failures)
            src_owner = reader.sequence_owner(find_oid(source, name))
            dst_owner = reader.sequence_owner(find_oid(target, name))
            _check(
                src_owner and dst_owner
                and src_owner["table_name"] == dst_owner["table_name"]
                and src_owner["column_name"] == dst_owner["column_name"]
                and dst_owner["table_schema"] == target,
                f"序列 {name} OWNED BY 归属不一致：{src_owner} != {dst_owner}", failures,
            )
        ok("序列声明属性与 OWNED BY 一致；未复制源序列当前进度（见冒烟测试）")

        # 4) 视图定义与 pg_depend 依赖只指向目标 schema
        for name in view_names:
            dst_view = reader.view_detail(find_oid(target, name))
            refs = _schema_refs_in_sql(f"CREATE VIEW x AS {dst_view['view_def']}", source)
            _check(not refs, f"视图 {name} 仍引用源 schema：{refs}", failures)

            dep_schemas = {
                r["nspname"]
                for r in reader.query(
                    """
                    SELECT DISTINCT n.nspname FROM pg_depend d
                    JOIN pg_rewrite r ON r.oid = d.objid
                    JOIN pg_class vc ON vc.oid = r.ev_class
                    JOIN pg_class c ON c.oid = d.refobjid
                    JOIN pg_namespace n ON n.oid = c.relnamespace
                    WHERE vc.oid = %s AND d.deptype = 'n'
                    """,
                    (find_oid(target, name),),
                )
            }
            _check(dep_schemas <= {target},
                   f"视图 {name} 依赖落到目标 schema 之外：{sorted(dep_schemas)}", failures)
        ok("视图定义与 pg_depend 依赖均只指向目标 schema")

        for ddl_item in manifest["ddl"]:
            refs = _schema_refs_in_sql(ddl_item["sql"], source)
            _check(not refs,
                   f"DDL[{ddl_item['object']}] 残留源 schema 对象引用：{refs}", failures)
        ok("全部导出 DDL 无指向源 schema 的对象引用（字符串常量中的同名文字不受影响）")

        # 5) 冒烟测试
        if smoke_test:
            report["smoke"] = _smoke_test(reader.dsn, target, table_names, seq_names, view_names)
            ok("插入新行默认取号、视图查询、业务字符串保持原样均通过")

    finally:
        reader.close()

    if failures:
        raise VerificationFailure("; ".join(failures))
    return report


def _normalize_sig(sig):
    """比较时忽略被引用表所在 schema（源/目标本就不同），只保留结构语义。"""
    out = []
    for item in sig:
        if item[0] == "f":
            item = item[:4] + ("<schema>", item[5]) + item[6:]
        out.append(item)
    return out


def _constraint_sig(reader, table_oid, expected_schema):
    """主键/外键的可比较签名：列名、被引用表的 schema/名称与动作。"""
    sig = []
    for row in reader.table_constraints(table_oid):
        if row["contype"] == "p":
            cols = _column_names(reader, table_oid, row["conkey"])
            sig.append(("p", row["conname"], tuple(cols)))
        elif row["contype"] == "f":
            local = _column_names(reader, row["conrelid"], row["conkey"])
            ref_cols = _column_names(reader, row["confrelid"], row["confkey"])
            ref = reader.query_one(
                "SELECT c.relname, n.nspname FROM pg_class c "
                "JOIN pg_namespace n ON n.oid=c.relnamespace WHERE c.oid=%s",
                (row["confrelid"],),
            )
            sig.append((
                "f", row["conname"], tuple(local), tuple(ref_cols),
                ref["nspname"], ref["relname"],
                row["confdeltype"], row["confupdtype"],
                row["condeferrable"], row["condeferred"],
            ))
    # 被引用表 schema 必须就是当前检查的 schema（源对源、目标对目标）
    for item in sig:
        if item[0] == "f":
            assert item[4] == expected_schema
    return sorted(sig, key=lambda x: x[1])


def _column_names(reader, table_oid, attnums):
    rows = reader.query(
        "SELECT attname, attnum FROM pg_attribute "
        "WHERE attrelid = %s AND attnum = ANY(%s)",
        (table_oid, list(attnums)),
    )
    by_num = {r["attnum"]: r["attname"] for r in rows}
    return [by_num[n] for n in attnums]


def _smoke_test(dsn, target, table_names, seq_names, view_names):
    """动态选择：含 nextval 默认值的表取号，含文本默认值的表检查业务字符串。

    全部操作在一个事务内，结束即回滚，目标结构保持空表。
    """
    conn = psycopg2.connect(dsn)
    result = {}
    try:
        conn.autocommit = False
        with conn.cursor() as cur:
            # 找到带序列默认值的表/列，以及序列的声明起点
            seq_defaults = []   # (table, column, seq_name, start_value)
            literal_defaults = []  # (table, column, literal)
            for tname in table_names:
                cur.execute(
                    """
                    SELECT a.attname, pg_get_expr(d.adbin, d.adrelid) AS expr
                      FROM pg_attrdef d
                      JOIN pg_attribute a ON a.attrelid=d.adrelid AND a.attnum=d.adnum
                      JOIN pg_class c ON c.oid=d.adrelid
                      JOIN pg_namespace n ON n.oid=c.relnamespace
                     WHERE n.nspname=%s AND c.relname=%s
                    """,
                    (target, tname),
                )
                for attname, expr in cur.fetchall():
                    if "nextval" in expr and "regclass" in expr:
                        seq_name = seq_names[0]
                        cur.execute(
                            "SELECT seqstart FROM pg_sequence s JOIN pg_class c ON c.oid=s.seqrelid "
                            "JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname=%s AND c.relname=%s",
                            (target, seq_name),
                        )
                        seq_defaults.append((tname, attname, seq_name, cur.fetchone()[0]))
                    else:
                        # 解析文本默认值的常量内容
                        parsed = pglast.parse_sql(f"SELECT {expr}")[0].stmt.targetList[0].val
                        node = parsed
                        if isinstance(node, ast.TypeCast) and isinstance(node.arg, ast.A_Const):
                            literal_defaults.append((tname, attname, node.arg.val.sval))

            inserted = {}
            # 第一遍：含序列默认值的表，插两行验证取号起点与推进
            for tname, column, seq_name, start in seq_defaults:
                required = _required_columns(cur, target, tname, exclude=column)
                first_id = _insert_one(cur, target, tname, column, required, 0)
                second_id = _insert_one(cur, target, tname, column, required, 1)
                inserted[tname] = (first_id, second_id)
                result.setdefault("sequence_checks", []).append({
                    "table": tname, "column": column, "sequence": seq_name,
                    "first_value": first_id, "second_value": second_id,
                    "declared_start": start,
                    "start_ok": first_id == start and second_id == start + 1,
                })

            # 第二遍：其余表（典型为外键子表）插入关联行，让视图 JOIN 能返回数据
            parent_ids = {t: ids[0] for t, ids in inserted.items()}
            for tname in table_names:
                if tname in inserted:
                    continue
                # NOT NULL 且无默认值的列必须赋值；外键列即使可空也要填上以建立关联
                cur.execute(
                    "SELECT a.attname FROM pg_attribute a "
                    "JOIN pg_class c ON c.oid=a.attrelid JOIN pg_namespace n ON n.oid=c.relnamespace "
                    "WHERE n.nspname=%s AND c.relname=%s AND a.attnum>0 AND NOT a.attisdropped "
                    "AND a.attnotnull AND NOT a.atthasdef ORDER BY a.attnum",
                    (target, tname),
                )
                required = [r[0] for r in cur.fetchall()]
                cur.execute(
                    "SELECT a.attname FROM pg_constraint con "
                    "JOIN pg_class c ON c.oid=con.conrelid JOIN pg_namespace n ON n.oid=c.relnamespace "
                    "JOIN pg_attribute a ON a.attrelid=con.conrelid AND a.attnum=ANY(con.conkey) "
                    "WHERE n.nspname=%s AND c.relname=%s AND con.contype='f'",
                    (target, tname),
                )
                fk_cols = {r[0] for r in cur.fetchall()}
                for col_name in fk_cols:
                    if col_name not in required:
                        required.append(col_name)
                cols, vals = [], []
                for i, col_name in enumerate(required):
                    cols.append(quote_ident_name(col_name))
                    ref_table = _fk_ref_table(cur, target, tname, col_name)
                    if ref_table is not None:
                        vals.append(str(parent_ids.get(ref_table, 1)))
                    else:
                        vals.append(_placeholder_for(cur, target, tname, col_name, i))
                cur.execute(
                    f"INSERT INTO {quote_qualified(target, tname)} "
                    f"({', '.join(cols)}) VALUES ({', '.join(vals)})"
                )
                result.setdefault("child_inserts", []).append(
                    {"table": tname, "columns": cols}
                )

            for tname, column, literal in literal_defaults:
                cur.execute(
                    f"SELECT {quote_ident_name(column)} FROM {quote_qualified(target, tname)} LIMIT 1"
                )
                row = cur.fetchone()
                result.setdefault("literal_checks", []).append({
                    "table": tname, "column": column,
                    "value": row[0] if row else None, "kept": bool(row) and row[0] == literal,
                    "expected": literal,
                })

            # 视图可查询且能 JOIN 出冒烟插入的关联行
            for vname in view_names:
                cur.execute(f"SELECT * FROM {quote_qualified(target, vname)}")
                view_rows = cur.fetchall()
                cur.execute(f"SELECT count(*) FROM {quote_qualified(target, vname)}")
                row_count = cur.fetchone()[0]
                cur.execute(
                    "SELECT pg_get_viewdef(c.oid, true) FROM pg_class c "
                    "JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname=%s AND c.relname=%s",
                    (target, vname),
                )
                viewdef = cur.fetchone()[0]
                result.setdefault("view_checks", []).append({
                    "view": vname, "row_count": row_count, "queryable": True,
                    "rows_preview": [list(r) for r in view_rows],
                    "viewdef": " ".join(viewdef.split()),
                })
                assert row_count >= 1, f"视图 {vname} 未能 JOIN 出冒烟数据，行数={row_count}"

            # 序列未携带源业务进度：首个 nextval 即声明起点（已在上面校验）
            conn.rollback()
    finally:
        conn.close()

    for check in result.get("sequence_checks", []):
        assert check["start_ok"], f"序列取号不符合声明起点：{check}"
    for check in result.get("literal_checks", []):
        assert check["kept"], f"业务字符串默认值被改写：{check}"
    result["rolled_back"] = True
    return result


def _required_columns(cur, schema, table, exclude):
    cur.execute(
        "SELECT a.attname FROM pg_attribute a "
        "JOIN pg_class c ON c.oid=a.attrelid JOIN pg_namespace n ON n.oid=c.relnamespace "
        "WHERE n.nspname=%s AND c.relname=%s AND a.attnum>0 AND NOT a.attisdropped "
        "AND a.attnotnull AND NOT a.atthasdef ORDER BY a.attnum",
        (schema, table),
    )
    return [r[0] for r in cur.fetchall() if r[0] != exclude]


def _insert_one(cur, schema, table, default_col, required, index):
    cols = [quote_ident_name(default_col)]
    vals = ["DEFAULT"]
    for i, col_name in enumerate(required):
        cols.append(quote_ident_name(col_name))
        vals.append(_placeholder_for(cur, schema, table, col_name, index * 10 + i))
    cur.execute(
        f"INSERT INTO {quote_qualified(schema, table)} "
        f"({', '.join(cols)}) VALUES ({', '.join(vals)}) "
        f"RETURNING {quote_ident_name(default_col)}"
    )
    return cur.fetchone()[0]


def _fk_ref_table(cur, schema, table, column):
    cur.execute(
        """
        SELECT rc.relname FROM pg_constraint con
        JOIN pg_class c ON c.oid=con.conrelid JOIN pg_namespace n ON n.oid=c.relnamespace
        JOIN pg_attribute a ON a.attrelid=con.conrelid AND a.attnum=ANY(con.conkey)
        JOIN pg_class rc ON rc.oid=con.confrelid
        WHERE n.nspname=%s AND c.relname=%s AND a.attname=%s AND con.contype='f'
        """,
        (schema, table, column),
    )
    row = cur.fetchone()
    return row[0] if row else None


def quote_ident_name(name):
    from .model import quote_ident
    return quote_ident(name)


def _placeholder_for(cur, schema, table, column, index):
    """为无默认值的 NOT NULL 列生成一个类型兼容的占位常量。"""
    cur.execute(
        "SELECT format_type(a.atttypid, a.atttypmod) FROM pg_attribute a "
        "JOIN pg_class c ON c.oid=a.attrelid JOIN pg_namespace n ON n.oid=c.relnamespace "
        "WHERE n.nspname=%s AND c.relname=%s AND a.attname=%s",
        (schema, table, column),
    )
    formatted = cur.fetchone()[0]
    base = formatted.split("(")[0].strip().lower()
    if base in {"bigint", "integer", "smallint", "numeric", "decimal", "real",
                "double precision", "int"}:
        return str(9000 + index)
    if base in {"boolean", "bool"}:
        return "true"
    # 其余一律按带引号文本处理（text/varchar/char/date 等均可接受字面量）
    return f"'smoke {index}'"
