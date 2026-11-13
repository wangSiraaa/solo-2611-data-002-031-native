"""导出编排：读请求 -> 求闭包 -> 生成 DDL -> 写清单与 SQL 文件。"""

import json
from collections import Counter

from .catalog import CatalogReader
from .ddl import DdlBuilder
from .errors import ExportError
from .model import quote_qualified

SUPPORTED_KINDS_REQUEST = {
    "ordinary_table",
    "sequence",
    "view",
    "primary_key",
    "foreign_key",
    "default",
    "sequence_ownership",
}


def load_request(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as fh:
        request = json.load(fh)
    required = {"root", "target_schema"}
    missing = required - set(request)
    if missing:
        raise ExportError(f"request.json 缺少字段：{sorted(missing)}")
    root = request["root"]
    if {"schema", "name", "kind"} - set(root):
        raise ExportError("root 需要包含 schema/name/kind 三个字段")
    if root["kind"] not in {"view", "ordinary_table"}:
        raise ExportError(f"根对象 kind={root['kind']} 不在支持范围（view/ordinary_table）")
    declared = set(request.get("supported_kinds", SUPPORTED_KINDS_REQUEST))
    unsupported_decl = declared - SUPPORTED_KINDS_REQUEST
    if unsupported_decl:
        raise ExportError(f"request 声明了工具不支持的种类：{sorted(unsupported_decl)}")
    if request.get("include_rows"):
        raise ExportError("本工具只迁移结构，include_rows=true 不在支持范围")
    return request


def run_export(dsn: str, request: dict):
    """执行导出，返回可写入文件的 manifest 字典。不修改源对象。"""
    source_schema = request["root"]["schema"]
    target_schema = request["target_schema"]
    if source_schema == target_schema:
        raise ExportError("源 schema 与目标 schema 不能相同")

    reader = CatalogReader(dsn)
    try:
        if reader.schema_exists(target_schema):
            raise ExportError(
                f"目标 schema {target_schema!r} 已存在，拒绝执行（要求目标 schema 为空/不存在）"
            )

        root = reader.find_relation(
            source_schema, request["root"]["name"], request["root"]["kind"]
        )
        objects, edges = reader.closure(root, source_schema)

        builder = DdlBuilder(reader, objects, edges, source_schema, target_schema)
        statements = builder.build()
    finally:
        reader.close()

    user_objects = [
        o for o in objects.values()
        if o.kind not in {"column", "view_rule", "builtin_type", "namespace"}
    ]
    kind_counter = Counter(o.kind for o in user_objects)

    manifest = {
        "root": request["root"],
        "source_schema": source_schema,
        "target_schema": target_schema,
        "include_rows": False,
        "structure_inventory": [
            {
                "kind": o.kind,
                "name": o.name,
                "source": quote_qualified(o.schema, o.name) if o.schema else o.name,
                "target": quote_qualified(target_schema, o.name)
                if o.kind in {"ordinary_table", "sequence", "view"} else None,
                "description": o.description,
            }
            for o in sorted(user_objects, key=lambda x: (x.kind, x.schema, x.name))
        ],
        "dependency_edges": [e.to_dict() for e in edges],
        "remapped_references": builder.manifest_refs,
        "object_count_by_kind": dict(sorted(kind_counter.items())),
        "excluded_from_closure": [
            "源 schema 中未被根对象依赖链触及的所有对象（例如无关表 unrelated）不导出",
            "所有业务行不导出",
            "源序列当前业务进度（last_value）不导出，新序列按声明的 START WITH 初始化",
        ],
        "ddl": [
            {"object": label, "sql": sql} for label, sql in statements
        ],
    }
    return manifest


def write_outputs(manifest: dict, out_dir: str):
    import os

    os.makedirs(out_dir, exist_ok=True)
    manifest_path = os.path.join(out_dir, "export_manifest.json")
    sql_path = os.path.join(out_dir, "schema_export.sql")
    with open(manifest_path, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    with open(sql_path, "w", encoding="utf-8") as fh:
        fh.write("-- 由 pgview_export 生成；仅结构，不含业务行。\n")
        fh.write(f"-- 源 schema: {manifest['source_schema']!r} -> 目标 schema: {manifest['target_schema']!r}\n\n")
        for item in manifest["ddl"]:
            fh.write(f"-- >>> {item['object']}\n")
            fh.write(item["sql"] + "\n\n")
    return manifest_path, sql_path
