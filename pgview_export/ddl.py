"""根据闭包对象生成映射到目标 schema 的可执行 DDL。

执行顺序：CREATE SCHEMA -> SEQUENCE -> TABLE（内联 PK、列默认值）
-> 单向外键 ALTER -> 序列 OWNED BY -> VIEW。
序列只按声明属性初始化，不复制源序列当前值。
"""

from .errors import ExportError
from .model import quote_ident, quote_qualified, remap_sql
import pglast
from pglast import ast

# 白名单：唯一允许出现在列默认值里的函数（序列取号），其 regclass 参数会被改写
_ALLOWED_DEFAULT_FUNCS = {"nextval"}


def _assert_default_supported(expression_sql: str, table_name: str, col_name: str):
    """默认值只允许常量表达式与 nextval；出现其他函数调用即范围外依赖。"""
    parsed = pglast.parse_sql(f"SELECT {expression_sql}")

    def walk(node):
        if not isinstance(node, ast.Node):
            return
        if node.__class__.__name__ == "FuncCall":
            fn = node.funcname[-1].sval if node.funcname else ""
            if fn not in _ALLOWED_DEFAULT_FUNCS:
                raise ExportError(
                    f"表 {table_name} 列 {col_name} 的默认值使用了范围外函数 {fn}()，"
                    f"本工具只支持常量默认值与 nextval 取号，已停止"
                )
        for member in node:
            value = getattr(node, member)
            if isinstance(value, tuple):
                for item in value:
                    walk(item)
            else:
                walk(value)

    walk(parsed[0].stmt.targetList[0].val)


class DdlBuilder:
    def __init__(self, reader, objects, edges, source_schema: str, target_schema: str):
        self.reader = reader
        self.objects = objects            # dict[(classid, oid, subid)] -> CatalogObject
        self.edges = edges
        self.source_schema = source_schema
        self.target_schema = target_schema
        self.tables = [o for o in objects.values() if o.kind == "ordinary_table"]
        self.sequences = [o for o in objects.values() if o.kind == "sequence"]
        self.views = [o for o in objects.values() if o.kind == "view"]
        self.statements = []              # [(对象标签, SQL)]
        self.manifest_refs = []           # 记录 AST 改写发生的位置，供清单展示

    def _remap(self, sql, context):
        new_sql, refs = remap_sql(sql, self.source_schema, self.target_schema)
        for ref_kind, ref_name in refs:
            self.manifest_refs.append({"context": context, "kind": ref_kind, "name": ref_name})
        return new_sql

    # ------------------------------------------------------------------

    def build(self):
        self._schema()
        self._sequences()
        self._tables()
        self._foreign_keys()
        self._sequence_ownership()
        self._views()
        return self.statements

    def _schema(self):
        self.statements.append(
            (
                f"schema {self.target_schema}",
                f"CREATE SCHEMA {quote_ident(self.target_schema)};",
            )
        )

    # ------------------------------------------------------------------

    def _sequences(self):
        for seq in self.sequences:
            detail = self.reader.sequence_detail(seq.obj_id.oid)
            target_name = quote_qualified(self.target_schema, seq.name)
            sql = (
                f"CREATE SEQUENCE {target_name} AS {detail['seqtype']}\n"
                f"    START WITH {detail['seqstart']}\n"
                f"    INCREMENT BY {detail['seqincrement']}\n"
                f"    MINVALUE {detail['seqmin']}\n"
                f"    MAXVALUE {detail['seqmax']}\n"
                f"    CACHE {detail['seqcache']}\n"
                f"    {'CYCLE' if detail['seqcycle'] else 'NO CYCLE'};"
            )
            self.statements.append((f"sequence {seq.schema}.{seq.name}", sql))

    # ------------------------------------------------------------------

    def _tables(self):
        for table in self.tables:
            oid = table.obj_id.oid
            columns = self.reader.table_columns(oid)
            defaults = {
                row["attname"]: row["default_expr"]
                for row in self.reader.column_defaults(oid)
            }
            constraints = self.reader.table_constraints(oid)
            pks = [c for c in constraints if c["contype"] == "p"]
            if len(pks) > 1:
                raise ExportError(f"表 {table.name} 存在多个主键，超出支持范围")

            col_lines = []
            for col in columns:
                if not col["type_builtin"]:
                    raise ExportError(
                        f"表 {table.name} 列 {col['attname']} 使用了非内置类型 "
                        f"{col['type_schema']}.{col['typname']}，超出支持范围"
                    )
                line = (
                    f"    {quote_ident(col['attname'])} {col['formatted_type']}"
                    f"{' NOT NULL' if col['attnotnull'] else ''}"
                )
                if col["attname"] in defaults:
                    raw_default = defaults[col["attname"]]
                    _assert_default_supported(raw_default, table.name, col["attname"])
                    default_sql = self._remap(
                        f"SELECT {raw_default}",
                        f"default {table.name}.{col['attname']}",
                    )
                    # 去掉外层 SELECT，保留表达式文本
                    expr = default_sql.strip()
                    assert expr.startswith("SELECT ")
                    expr = expr[len("SELECT "):].rstrip(";")
                    line += f" DEFAULT {expr}"
                col_lines.append(line)

            if pks:
                pk = pks[0]
                pk_cols = self._constraint_column_names(oid, pk["conkey"])
                col_lines.append(
                    f"    CONSTRAINT {quote_ident(pk['conname'])} PRIMARY KEY ("
                    + ", ".join(quote_ident(c) for c in pk_cols)
                    + ")"
                )

            sql = (
                f"CREATE TABLE {quote_qualified(self.target_schema, table.name)} (\n"
                + ",\n".join(col_lines)
                + "\n);"
            )
            self.statements.append((f"table {table.schema}.{table.name}", sql))

    def _constraint_column_names(self, table_oid, attnums):
        rows = self.reader.query(
            "SELECT attname, attnum FROM pg_attribute "
            "WHERE attrelid = %s AND attnum = ANY(%s) ORDER BY array_position(%s, attnum)",
            (table_oid, list(attnums), list(attnums)),
        )
        return [r["attname"] for r in rows]

    # ------------------------------------------------------------------

    def _foreign_keys(self):
        table_oids = {t.obj_id.oid for t in self.tables}
        fks = [o for o in self.objects.values() if o.kind == "foreign_key"]

        # 循环外键不在支持范围：有向图上环直接报错
        self._assert_acyclic(fks)

        for fk in sorted(fks, key=lambda o: o.name):
            row = self.reader.query_one(
                "SELECT * FROM pg_constraint WHERE oid = %s", (fk.obj_id.oid,)
            )
            if row["confrelid"] not in table_oids:
                # 正常情况下闭包遍历已把被引用表纳入；这里是防御性检查
                raise ExportError(
                    f"外键 {fk.name} 引用的表不在依赖闭包内，已停止"
                )
            ref_table = self.reader.query_one(
                "SELECT c.relname FROM pg_class c WHERE c.oid = %s",
                (row["confrelid"],),
            )
            local_cols = self._constraint_column_names(row["conrelid"], row["conkey"])
            ref_cols = self._constraint_column_names(row["confrelid"], row["confkey"])
            action_map = {"a": "NO ACTION", "r": "RESTRICT", "c": "CASCADE",
                          "n": "SET NULL", "d": "SET DEFAULT"}
            parts = [
                f"ALTER TABLE {quote_qualified(self.target_schema, fk.detail['table'])}",
                f"    ADD CONSTRAINT {quote_ident(fk.name)} FOREIGN KEY (",
                "        " + ", ".join(quote_ident(c) for c in local_cols) + ")",
                f"    REFERENCES {quote_qualified(self.target_schema, ref_table['relname'])} (",
                "        " + ", ".join(quote_ident(c) for c in ref_cols) + ")",
                f"    ON DELETE {action_map[row['confdeltype']]}",
                f"    ON UPDATE {action_map[row['confupdtype']]}",
            ]
            if row["condeferrable"]:
                parts.append("    DEFERRABLE" + (" INITIALLY DEFERRED" if row["condeferred"] else ""))
            parts[-1] = parts[-1] + ";"
            self.statements.append(
                (f"foreign_key {fk.schema}.{fk.name}", "\n".join(parts))
            )

    def _assert_acyclic(self, fks):
        # 以表为节点建 FK 图并做 DFS 环检测
        graph = {}
        for fk in fks:
            row = self.reader.query_one(
                "SELECT conrelid, confrelid FROM pg_constraint WHERE oid = %s",
                (fk.obj_id.oid,),
            )
            graph.setdefault(row["conrelid"], set()).add(row["confrelid"])
        state = {}

        def visit(node, stack):
            state[node] = 1
            for nxt in graph.get(node, ()):
                if state.get(nxt) == 1:
                    cycle = " -> ".join(str(x) for x in stack + [nxt])
                    raise ExportError(f"检测到循环外键，超出支持范围：{cycle}")
                if state.get(nxt) != 2:
                    visit(nxt, stack + [nxt])
            state[node] = 2

        for node in graph:
            if state.get(node) != 2:
                visit(node, [node])

    # ------------------------------------------------------------------

    def _sequence_ownership(self):
        for seq in self.sequences:
            owner = self.reader.sequence_owner(seq.obj_id.oid)
            if owner is None:
                continue
            sql = (
                f"ALTER SEQUENCE {quote_qualified(self.target_schema, seq.name)} "
                f"OWNED BY {quote_qualified(self.target_schema, owner['table_name'])}"
                f".{quote_ident(owner['column_name'])};"
            )
            self.statements.append(
                (f"sequence_ownership {seq.name} -> {owner['table_name']}.{owner['column_name']}",
                 sql)
            )

    # ------------------------------------------------------------------

    def _views(self):
        # 视图之间按依赖边拓扑排序（本工具支持的普通视图场景通常只有一个根视图）
        view_oids = {v.obj_id.oid for v in self.views}
        depends_on = {v.obj_id.oid: set() for v in self.views}
        for edge in self.edges:
            if (
                edge.object.kind == "view"
                and edge.referenced.kind == "view"
                and edge.object.obj_id.oid in view_oids
            ):
                depends_on[edge.object.obj_id.oid].add(edge.referenced.obj_id.oid)

        ordered = []
        done = set()

        def emit(oid):
            if oid in done:
                return
            for dep in depends_on.get(oid, ()):
                emit(dep)
            done.add(oid)
            ordered.append(oid)

        for view in self.views:
            emit(view.obj_id.oid)

        for oid in ordered:
            detail = self.reader.view_detail(oid)
            # 用完整 CREATE VIEW 包裹后交给 AST 改写，限定表名必然指向目标 schema
            create_sql = (
                f"CREATE VIEW {quote_qualified(self.source_schema, detail['relname'])} AS\n"
                f"{detail['view_def'].rstrip().rstrip(';')}"
            )
            new_sql = self._remap(create_sql, f"view {detail['relname']}")
            self.statements.append(
                (f"view {detail['nspname']}.{detail['relname']}", new_sql)
            )
