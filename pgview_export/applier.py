"""在单个事务中应用导出的 DDL，失败整体回滚并报告失败对象与原因。"""

import psycopg2

from .errors import ApplyError


def apply_manifest(dsn: str, manifest: dict, target_schema: str):
    """幂等前置检查 + 事务化应用。

    - 目标 schema 已存在 -> 拒绝执行；
    - 任一条 DDL 失败 -> ROLLBACK，数据库不留半套结构，抛出 ApplyError。
    """
    conn = psycopg2.connect(dsn)
    try:
        conn.autocommit = False
        with conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM pg_namespace WHERE nspname = %s", (target_schema,)
            )
            if cur.fetchone() is not None:
                conn.rollback()
                raise ApplyError(
                    f"schema {target_schema}",
                    "目标 schema 已存在，拒绝执行",
                    f"CREATE SCHEMA {target_schema}",
                )

            for item in manifest["ddl"]:
                try:
                    cur.execute(item["sql"])
                except psycopg2.Error as exc:
                    conn.rollback()
                    raise ApplyError(
                        item["object"], str(exc).strip(), item["sql"]
                    ) from exc
        conn.commit()
    finally:
        conn.close()
