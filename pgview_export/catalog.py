"""只读访问 pg_catalog，并沿真实依赖图求根对象的必要依赖闭包。

所有结构信息都来自系统目录，不解析视图文本去猜表名，也不导出整个源 schema。
"""

import psycopg2
import psycopg2.extras

from .errors import UnsupportedDependency
from .model import CatalogObject, DependencyEdge, ObjectId

# 本工具支持的 relkind / contype 白名单
SUPPORTED_RELKINDS = {"r": "ordinary_table", "S": "sequence", "v": "view"}
SUPPORTED_CONTYPES = {"p": "primary_key", "f": "foreign_key"}


class CatalogReader:
    def __init__(self, dsn: str):
        # 只读会话：导出过程物理上无法修改源对象
        self.dsn = dsn
        self.conn = psycopg2.connect(dsn)
        self.conn.set_session(readonly=True, autocommit=True)

    def close(self):
        self.conn.close()

    def _query(self, sql, params=None):
        with self.conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, params or ())
            return cur.fetchall()

    def query(self, sql, params=None):
        """包内其他模块复用的只读查询入口。"""
        return self._query(sql, params)

    def query_one(self, sql, params=None):
        rows = self._query(sql, params)
        return rows[0] if rows else None

    def _query_one(self, sql, params=None):
        rows = self._query(sql, params)
        return rows[0] if rows else None

    # ------------------------------------------------------------------
    # 基础查找
    # ------------------------------------------------------------------

    def find_relation(self, schema: str, name: str, expected_kind: str):
        row = self._query_one(
            """
            SELECT c.oid, c.relkind, c.relname, n.nspname,
                   pg_describe_object(c.tableoid, c.oid, 0) AS description
              FROM pg_class c
              JOIN pg_namespace n ON n.oid = c.relnamespace
             WHERE n.nspname = %s AND c.relname = %s
            """,
            (schema, name),
        )
        if row is None:
            raise UnsupportedDependency(
                [f"根对象不存在：{expected_kind} {schema}.{name}"]
            )
        actual = SUPPORTED_RELKINDS.get(row["relkind"])
        if actual != expected_kind:
            raise UnsupportedDependency(
                [
                    f"根对象类型不符：{schema}.{name} 实际为 {actual or row['relkind']}，"
                    f"请求声明为 {expected_kind}"
                ]
            )
        return self._make_relation_object(row)

    def _make_relation_object(self, row):
        kind = SUPPORTED_RELKINDS[row["relkind"]]
        return CatalogObject(
            obj_id=ObjectId("pg_class", row["oid"]),
            kind=kind,
            schema=row["nspname"],
            name=row["relname"],
            description=row["description"],
        )

    # ------------------------------------------------------------------
    # 依赖图
    # ------------------------------------------------------------------

    def _all_depend_rows(self):
        return self._query(
            """
            SELECT d.classid, d.objid, d.objsubid, d.refclassid,
                   d.refobjid, d.refobjsubid, d.deptype,
                   d.classid::regclass::text  AS classid_name,
                   d.refclassid::regclass::text AS refclassid_name
              FROM pg_depend d
            """
        )

    def _describe(self, classid_name, oid, objsubid):
        row = self._query_one(
            "SELECT pg_describe_object(%s::regclass, %s, %s) AS text",
            (classid_name, oid, objsubid),
        )
        return row["text"] if row else f"{classid_name} {oid}/{objsubid}"

    def _classify(self, classid_name, oid, objsubid, description):
        """把依赖图上的对象分类为支持的种类，或标记为范围外。"""
        if classid_name == "pg_class":
            row = self._query_one(
                """
                SELECT c.relkind, c.relname, n.nspname
                  FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
                 WHERE c.oid = %s
                """,
                (oid,),
            )
            if row is None:
                return None, None
            # pg_depend 中对列的引用记为 pg_class + objsubid=attnum，识别为列对象
            if objsubid != 0:
                col = self._query_one(
                    "SELECT a.attname FROM pg_attribute a "
                    "WHERE a.attrelid = %s AND a.attnum = %s",
                    (oid, objsubid),
                )
                if col is None:
                    return None, None
                return (
                    CatalogObject(
                        obj_id=ObjectId(classid_name, oid, objsubid),
                        kind="column",
                        schema=row["nspname"],
                        name=f"{row['relname']}.{col['attname']}",
                        description=description,
                    ),
                    None,
                )
            # 普通索引（含主键 backing index）与 TOAST 表是内部实现对象，
            # 随约束/表自动重建，不作为独立结构导出
            if row["relkind"] in {"i", "t"}:
                return None, None
            # 分区表、物化视图、外部表、复合类型等关系类型明确在范围之外
            if row["relkind"] in {"p", "m", "f", "c"}:
                return None, (
                    f"不支持的关系类型 relkind={row['relkind']}：{description}"
                )
            kind = SUPPORTED_RELKINDS.get(row["relkind"])
            if kind is None:
                return None, f"不支持的关系类型 relkind={row['relkind']}：{description}"
            return (
                CatalogObject(
                    obj_id=ObjectId(classid_name, oid, objsubid),
                    kind=kind,
                    schema=row["nspname"],
                    name=row["relname"],
                    description=description,
                ),
                None,
            )

        if classid_name == "pg_attribute":
            row = self._query_one(
                """
                SELECT a.attname, c.relkind, n.nspname, c.relname
                  FROM pg_attribute a
                  JOIN pg_class c ON c.oid = a.attrelid
                  JOIN pg_namespace n ON n.oid = c.relnamespace
                 WHERE a.attrelid = %s AND a.attnum = %s
                """,
                (oid, objsubid),
            )
            if row is None:
                return None, None
            if row["relkind"] not in SUPPORTED_RELKINDS:
                return None, f"不支持关系上的列依赖：{description}"
            return (
                CatalogObject(
                    obj_id=ObjectId(classid_name, oid, objsubid),
                    kind="column",
                    schema=row["nspname"],
                    name=f"{row['relname']}.{row['attname']}",
                    description=description,
                ),
                None,
            )

        if classid_name == "pg_constraint":
            row = self._query_one(
                """
                SELECT con.conname, con.contype, n.nspname, c.relname
                  FROM pg_constraint con
                  JOIN pg_class c ON c.oid = con.conrelid
                  JOIN pg_namespace n ON n.oid = con.connamespace
                 WHERE con.oid = %s
                """,
                (oid,),
            )
            if row is None:
                return None, None
            kind = SUPPORTED_CONTYPES.get(row["contype"])
            if kind is None:
                label = {"u": "UNIQUE", "c": "CHECK", "x": "EXCLUDE", "t": "TRIGGER"}.get(
                    row["contype"], row["contype"]
                )
                return None, f"不支持的约束类型 {label}：{description}"
            return (
                CatalogObject(
                    obj_id=ObjectId(classid_name, oid),
                    kind=kind,
                    schema=row["nspname"],
                    name=row["conname"],
                    detail={"table": row["relname"]},
                    description=description,
                ),
                None,
            )

        if classid_name == "pg_attrdef":
            row = self._query_one(
                """
                SELECT a.attname, n.nspname, c.relname
                  FROM pg_attrdef d
                  JOIN pg_class c ON c.oid = d.adrelid
                  JOIN pg_namespace n ON n.oid = c.relnamespace
                  JOIN pg_attribute a ON a.attrelid = d.adrelid AND a.attnum = d.adnum
                 WHERE d.oid = %s
                """,
                (oid,),
            )
            if row is None:
                return None, None
            return (
                CatalogObject(
                    obj_id=ObjectId(classid_name, oid),
                    kind="default",
                    schema=row["nspname"],
                    name=f"{row['relname']}.{row['attname']} 默认值",
                    description=description,
                ),
                None,
            )

        if classid_name == "pg_rewrite":
            # 视图的 _RETURN 规则属于视图内部机制，随视图一起导出
            return (
                CatalogObject(
                    obj_id=ObjectId(classid_name, oid),
                    kind="view_rule",
                    schema="",
                    name=description,
                    description=description,
                ),
                None,
            )

        if classid_name == "pg_namespace":
            # schema 容器本身按目标 schema 重建，不属于结构依赖
            return (
                CatalogObject(
                    obj_id=ObjectId(classid_name, oid),
                    kind="namespace",
                    schema="",
                    name=description,
                    description=description,
                ),
                None,
            )

        if classid_name == "pg_type":
            row = self._query_one(
                """
                SELECT t.typtype, t.typname, n.nspname,
                       n.nspname = 'pg_catalog' OR n.nspname = 'information_schema'
                         AS is_builtin
                  FROM pg_type t JOIN pg_namespace n ON n.oid = t.typnamespace
                 WHERE t.oid = %s
                """,
                (oid,),
            )
            if row is None:
                return None, None
            if row["is_builtin"]:
                return (
                    CatalogObject(
                        obj_id=ObjectId(classid_name, oid),
                        kind="builtin_type",
                        schema=row["nspname"],
                        name=row["typname"],
                        description=description,
                    ),
                    None,
                )
            return None, f"不支持的非内置类型/自定义类型依赖：{description}"

        # 函数、操作符、扩展、触发器、角色、ACL 等一律在范围之外
        friendly = {
            "pg_proc": "函数/过程",
            "pg_operator": "操作符",
            "pg_extension": "扩展",
            "pg_trigger": "触发器",
            "pg_roles": "角色",
            "pg_authid": "角色",
            "pg_collation": "排序规则",
            "pg_conversion": "编码转换",
        }.get(classid_name, classid_name)
        return None, f"不支持的对象类型（{friendly}）：{description}"

    def closure(self, root: CatalogObject, source_schema: str):
        """求根对象的必要依赖闭包，返回 (对象表, 边列表)。

        pg_depend 的挂载方向有两种：
          * 视图 -> _RETURN 规则 -> 表/列：从规则正向遍历；
          * 表 <- 约束/默认值（反向 a 边挂在表上），约束/默认值再正向指向
            被引用表、序列等：对入闭包的表先反向收集载体再正向扩展。
        范围外依赖汇总后一次性抛出 UnsupportedDependency，绝不静默丢弃。
        """
        rows = self._all_depend_rows()
        outgoing, incoming = {}, {}
        for row in rows:
            outgoing.setdefault(
                (row["classid_name"], row["objid"], row["objsubid"]), []
            ).append(row)
            incoming.setdefault(
                (row["refclassid_name"], row["refobjid"], row["refobjsubid"]), []
            ).append(row)

        objects = {root.obj_id.as_tuple(): root}
        edges = []
        unsupported = []
        carrier_owner = {}          # 载体坐标 -> 所属用户对象坐标
        seen_edges = set()
        visited_relations = set()   # 每个关系只做一次载体收集，避免重复入队
        relation_queue = [root]     # 需要做“反向收集载体”的关系对象

        def classify_edge(row, direction):
            """direction='out' 时被引用端是 ref*，'in' 时依赖端是 obj*。"""
            if direction == "out":
                cid, oid, sub = row["refclassid_name"], row["refobjid"], row["refobjsubid"]
            else:
                cid, oid, sub = row["classid_name"], row["objid"], row["objsubid"]
            desc = self._describe(cid, oid, sub)
            return self._classify(cid, oid, sub, desc)

        def add_edge(subject, referenced, row):
            edge_key = (
                row["classid_name"], row["objid"], row["objsubid"],
                row["refclassid_name"], row["refobjid"], row["refobjsubid"],
            )
            if edge_key in seen_edges:
                return
            seen_edges.add(edge_key)
            edges.append(DependencyEdge(
                object=subject, referenced=referenced,
                deptype=row["deptype"],
                description=self._describe(row["classid_name"], row["objid"], row["objsubid"]),
            ))

        def expand_forward(owner_obj, carrier_id):
            """沿载体（pg_rewrite/pg_constraint/pg_attrdef）的正向边扩展。"""
            for row in outgoing.get(carrier_id.as_tuple(), []):
                if row["deptype"] == "i":
                    continue
                referenced, problem = classify_edge(row, "out")
                if problem is not None:
                    unsupported.append(
                        f"{problem}（被 {self._describe(row['classid_name'], row['objid'], row['objsubid'])} 依赖）"
                    )
                    continue
                if referenced is None or referenced.kind in {"namespace", "builtin_type"}:
                    continue
                add_edge(owner_obj, referenced, row)
                key = referenced.obj_id.as_tuple()
                if key not in objects:
                    objects[key] = referenced
                if referenced.kind == "column":
                    # 列所属的关系必须入闭包并入队，以便收集其约束/默认值/序列
                    rel_obj, rel_problem = self._classify(
                        "pg_class", referenced.obj_id.oid, 0,
                        self._describe("pg_class", referenced.obj_id.oid, 0),
                    )
                    if rel_problem is not None:
                        unsupported.append(rel_problem)
                    elif rel_obj is not None:
                        rel_key = rel_obj.obj_id.as_tuple()
                        if rel_key not in objects:
                            objects[rel_key] = rel_obj
                            relation_queue.append(rel_obj)
                elif referenced.kind in {"ordinary_table", "sequence", "view"}:
                    relation_queue.append(referenced)

        while relation_queue:
            relation = relation_queue.pop(0)
            if relation.obj_id.as_tuple() in visited_relations:
                continue
            visited_relations.add(relation.obj_id.as_tuple())
            rid = relation.obj_id

            # 1) 视图：_RETURN 规则正向到表/列
            if relation.kind == "view":
                for rule in self.query(
                    "SELECT oid FROM pg_rewrite WHERE ev_class = %s AND rulename = '_RETURN'",
                    (rid.oid,),
                ):
                    carrier_id = ObjectId("pg_rewrite", rule["oid"])
                    carrier_owner[carrier_id.as_tuple()] = rid.as_tuple()
                    expand_forward(relation, carrier_id)

            # 2) 普通表：反向收集约束与列默认值。
            #    这些载体的 a/n 边挂在列（pg_class+attnum 或 pg_attribute）上，
            #    因此把表本身和所有列坐标都作为反向查找键。
            if relation.kind == "ordinary_table":
                ref_keys = [rid.as_tuple()]
                for col in self.query(
                    "SELECT attnum FROM pg_attribute "
                    "WHERE attrelid = %s AND attnum > 0 AND NOT attisdropped",
                    (rid.oid,),
                ):
                    ref_keys.append(("pg_class", rid.oid, col["attnum"]))
                    ref_keys.append(("pg_attribute", rid.oid, col["attnum"]))
                for ref_key in ref_keys:
                    for row in incoming.get(ref_key, []):
                        if row["deptype"] not in {"a", "n"}:
                            continue
                        if row["classid_name"] not in {"pg_constraint", "pg_attrdef"}:
                            continue
                        carrier_id = ObjectId(row["classid_name"], row["objid"])
                        carrier_owner[carrier_id.as_tuple()] = rid.as_tuple()
                        carrier_obj, problem = classify_edge(row, "in")
                        if problem is not None:
                            unsupported.append(problem)
                            continue
                        if carrier_obj is None:
                            continue
                        add_edge(relation, carrier_obj, row)
                        key = carrier_obj.obj_id.as_tuple()
                        if key not in objects:
                            objects[key] = carrier_obj
                        # 约束/默认值继续正向扩展（FK 指向被引用表、默认值指向序列）
                        expand_forward(relation, carrier_id)

            # 3) 序列：OWNED BY 是 sequence -> column 的 a 边（正向）
            if relation.kind == "sequence":
                for row in outgoing.get(rid.as_tuple(), []):
                    if row["deptype"] != "a":
                        continue
                    referenced, problem = classify_edge(row, "out")
                    if problem is not None:
                        unsupported.append(problem)
                        continue
                    if referenced is not None and referenced.kind == "column":
                        add_edge(relation, referenced, row)
                        objects.setdefault(referenced.obj_id.as_tuple(), referenced)

        # 列对象不进结构清单（列随表导出），但保留在边里用于展示依赖
        inventory_objects = {
            key: obj for key, obj in objects.items() if obj.kind != "column"
        }

        # 闭包中的用户对象必须位于源 schema（跨 schema 引用属于范围外，避免误搬）
        for obj in objects.values():
            if obj.kind in {"ordinary_table", "sequence", "view",
                            "primary_key", "foreign_key", "default", "column"}:
                if obj.schema != source_schema:
                    unsupported.append(
                        f"跨 schema 依赖超出单次迁移范围：{obj.description}"
                    )

        if unsupported:
            raise UnsupportedDependency(sorted(set(unsupported)))

        return inventory_objects, edges

    # ------------------------------------------------------------------
    # 结构详情读取
    # ------------------------------------------------------------------

    def table_columns(self, table_oid: int):
        return self._query(
            """
            SELECT a.attname, a.atttypid, a.atttypmod, a.attnotnull,
                   format_type(a.atttypid, a.atttypmod) AS formatted_type,
                   t.typname, tn.nspname AS type_schema,
                   tn.nspname IN ('pg_catalog', 'information_schema') AS type_builtin,
                   a.atthasdef
              FROM pg_attribute a
              JOIN pg_type t ON t.oid = a.atttypid
              JOIN pg_namespace tn ON tn.oid = t.typnamespace
             WHERE a.attrelid = %s AND a.attnum > 0 AND NOT a.attisdropped
             ORDER BY a.attnum
            """,
            (table_oid,),
        )

    def table_constraints(self, table_oid: int):
        return self._query(
            """
            SELECT con.oid, con.conname, con.contype, con.conrelid,
                   con.conkey, con.confkey,
                   con.confrelid, con.condeferrable, con.condeferred,
                   con.confupdtype, con.confdeltype, con.convalidated,
                   pg_get_constraintdef(con.oid) AS constraint_def
              FROM pg_constraint con
             WHERE con.conrelid = %s
             ORDER BY con.oid
            """,
            (table_oid,),
        )

    def column_defaults(self, table_oid: int):
        return self._query(
            """
            SELECT a.attname, d.oid AS attrdef_oid,
                   pg_get_expr(d.adbin, d.adrelid) AS default_expr
              FROM pg_attrdef d
              JOIN pg_attribute a ON a.attrelid = d.adrelid AND a.attnum = d.adnum
             WHERE d.adrelid = %s
            """,
            (table_oid,),
        )

    def sequence_detail(self, sequence_oid: int):
        return self._query_one(
            """
            SELECT c.relname, n.nspname,
                   s.seqtypid, format_type(s.seqtypid, NULL) AS seqtype,
                   s.seqstart, s.seqincrement, s.seqmin, s.seqmax,
                   s.seqcache, s.seqcycle
              FROM pg_sequence s
              JOIN pg_class c ON c.oid = s.seqrelid
              JOIN pg_namespace n ON n.oid = c.relnamespace
             WHERE s.seqrelid = %s
            """,
            (sequence_oid,),
        )

    def sequence_owner(self, sequence_oid: int):
        """OWNED BY 信息在 pg_depend 中是 sequence -> column 的 a 边。"""
        return self._query_one(
            """
            SELECT c.relname AS table_name, n.nspname AS table_schema,
                   a.attname AS column_name
              FROM pg_depend d
              JOIN pg_class c ON c.oid = d.refobjid
              JOIN pg_namespace n ON n.oid = c.relnamespace
              JOIN pg_attribute a ON a.attrelid = d.refobjid AND a.attnum = d.refobjsubid
             WHERE d.objid = %s AND d.classid = 'pg_class'::regclass
               AND d.refclassid = 'pg_class'::regclass AND d.objsubid = 0
               AND d.deptype = 'a'
            """,
            (sequence_oid,),
        )

    def view_detail(self, view_oid: int):
        return self._query_one(
            """
            SELECT c.relname, n.nspname,
                   pg_get_viewdef(c.oid, true) AS view_def
              FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
             WHERE c.oid = %s
            """,
            (view_oid,),
        )

    def schema_exists(self, schema_name: str) -> bool:
        return self._query_one(
            "SELECT EXISTS(SELECT 1 FROM pg_namespace WHERE nspname = %s) AS yes",
            (schema_name,),
        )["yes"]
