"""数据模型与标识符处理。

对象的唯一标识统一使用 (classid, oid, objsubid) 三元组，与 pg_depend 对齐，
避免依赖名字做判断（名字可能含空格、大小写、引号，且不保证全局唯一）。
"""

from dataclasses import dataclass, field

import pglast
from pglast import ast
from pglast.stream import RawStream
from pglast.visitors import Visitor


# ---------------------------------------------------------------------------
# 标识符引用
# ---------------------------------------------------------------------------

def quote_ident(name: str) -> str:
    """按 PostgreSQL quote_ident 的规则给单个标识符加引号。

    只有“全小写/下划线开头且仅含小写字母、数字、下划线、$”的标识符才可以裸写；
    含大写字母、空格或特殊字符的必须双引号包裹，否则 PG 会折叠成小写而改名。
    内嵌引号双写。
    """
    safe = bool(name) and (name[0].isalpha() or name[0] == "_") and all(
        ch.islower() or ch.isdigit() or ch in "_$" for ch in name
    )
    if safe:
        return name
    return '"' + name.replace('"', '""') + '"'


def quote_qualified(schema: str, name: str) -> str:
    return f"{quote_ident(schema)}.{quote_ident(name)}"


def quote_literal(value: str) -> str:
    """生成 SQL 字符串常量（标准 conforming strings 规则）。"""
    return "'" + value.replace("'", "''") + "'"


# ---------------------------------------------------------------------------
# 目录对象模型
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ObjectId:
    """pg_depend 风格的对象坐标。"""

    classid: str          # 系统目录名，如 pg_class
    oid: int
    objsubid: int = 0

    def as_tuple(self):
        return self.classid, self.oid, self.objsubid


@dataclass
class CatalogObject:
    """闭包中的一个源对象（只记录生成 DDL 和报告所需的信息）。"""

    obj_id: ObjectId
    kind: str                         # ordinary_table / sequence / view / column ...
    schema: str
    name: str
    detail: dict = field(default_factory=dict)
    description: str = ""             # pg_describe_object 的结果，用于报错清单


@dataclass
class DependencyEdge:
    """一条依赖边：object 依赖 referenced。"""

    object: CatalogObject
    referenced: CatalogObject
    deptype: str
    description: str

    @staticmethod
    def _label(obj: CatalogObject) -> str:
        label = f"{obj.kind}:{obj.schema}.{obj.name}" if obj.schema else f"{obj.kind}:{obj.name}"
        if obj.kind == "column":
            return label  # 列对象的 name 已含“表.列”
        return label

    def to_dict(self):
        return {
            "object": self._label(self.object),
            "referenced": self._label(self.referenced),
            "deptype": self.deptype,
            "meaning": self.description,
        }


# ---------------------------------------------------------------------------
# SQL AST 改写：把源 schema 的真实对象引用重写到目标 schema
# ---------------------------------------------------------------------------

def _regclass_parts(literal: str):
    """把 regclass 文本常量解析成 (schema, relation) 名字段。

    regclass 输入的语法就是关系名列表，借 FROM 子句让真正的 SQL 解析器分词，
    不做任何字符串切割/替换。
    """
    parsed = pglast.parse_sql(f"SELECT 1 FROM {literal} _reparse")
    range_var = parsed[0].stmt.fromClause[0]
    return range_var.schemaname, range_var.relname


class SchemaRemapVisitor(Visitor):
    """遍历 AST，把指向源 schema 的对象引用改写为目标 schema。

    覆盖两类真实对象引用：
      1. RangeVar —— 视图定义里的 schema 限定表/视图名；
      2. `<字符串常量>::regclass` —— nextval 等函数的 regclass 参数。
    普通字符串常量（即使内容里出现同名 schema 文字）不是对象引用，保持原样。
    """

    def __init__(self, source_schema: str, target_schema: str):
        super().__init__()
        self.source_schema = source_schema
        self.target_schema = target_schema
        self.remapped_refs = []

    def visit_RangeVar(self, ancestors, node):
        if node.schemaname == self.source_schema:
            node.schemaname = self.target_schema
            self.remapped_refs.append(("RangeVar", node.relname))

    def visit_TypeCast(self, ancestors, node):
        type_name = node.typeName
        if not type_name or [s.sval for s in type_name.names] != ["regclass"]:
            return
        arg = node.arg
        if not (isinstance(arg, ast.A_Const) and isinstance(arg.val, ast.String)):
            return
        schema, relation = _regclass_parts(arg.val.sval)
        if schema == self.source_schema:
            arg.val.sval = ".".join(
                [quote_ident(self.target_schema), quote_ident(relation)]
            )
            self.remapped_refs.append(("regclass", relation))


def remap_sql(sql: str, source_schema: str, target_schema: str):
    """解析 SQL 并返回 (改写后的 SQL, 改写引用列表)。

    只改写 AST 层面的对象引用；无法解析的输入直接抛错而不是退回字符串替换。
    """
    statements = pglast.parse_sql(sql)
    visitor = SchemaRemapVisitor(source_schema, target_schema)
    for statement in statements:
        visitor(statement)
    return RawStream()(statements), visitor.remapped_refs


def deparse_expr(expression_sql: str) -> str:
    """把一个表达式 SQL 反解析为规范文本（用于等价比较）。"""
    parsed = pglast.parse_sql(f"SELECT {expression_sql}")
    return RawStream()(parsed[0].stmt.targetList[0].val)
