"""Reads of pg_catalog that describe the partitioned table as it actually is.

Nothing here trusts table names: the parent is resolved to its pg_class row,
the partition key is read from pg_partitioned_table, and every partition's
coverage comes from parsing its stored relpartbound expression.
"""
from __future__ import annotations

from dataclasses import dataclass

from .bounds import Bound, parse_partition_bound
from .errors import CatalogError

#: Partition-key types this tool can move rows for, mapped to the SQL keyword
#: used to prefix range literals in CHECK constraints.
KEY_TYPE_KEYWORD = {
    "timestamp with time zone": "TIMESTAMPTZ",
    "timestamp without time zone": "TIMESTAMP",
}


def qi(*parts: str) -> str:
    """Quote identifiers and join them into a qualified SQL name."""
    return ".".join('"' + p.replace('"', '""') + '"' for p in parts)


@dataclass
class ParentInfo:
    oid: int
    schema: str
    name: str
    key_column: str
    key_type: str
    columns: list  # column names in attnum order

    @property
    def qname(self) -> str:
        return f"{self.schema}.{self.name}"


@dataclass
class PartitionInfo:
    oid: int
    schema: str
    name: str
    bound: Bound

    @property
    def qname(self) -> str:
        return f"{self.schema}.{self.name}"


_PARENT_SQL = """
SELECT c.oid, n.nspname, c.relname, c.relkind
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = %(schema)s AND c.relname = %(name)s
"""

_PARTKEY_SQL = """
SELECT p.partstrat, p.partnatts, p.partattrs::text AS attrs
FROM pg_partitioned_table p
WHERE p.partrelid = %(oid)s
"""

_ATTR_SQL = """
SELECT attname, atttypid::regtype::text AS typ, attidentity, attgenerated
FROM pg_attribute
WHERE attrelid = %(oid)s AND attnum = %(attnum)s
"""

_COLUMNS_SQL = """
SELECT attname, attidentity, attgenerated
FROM pg_attribute
WHERE attrelid = %(oid)s AND attnum > 0 AND NOT attisdropped
ORDER BY attnum
"""

_TRIGGERS_SQL = """
SELECT tgname FROM pg_trigger
WHERE tgrelid = %(oid)s AND NOT tgisinternal
"""

_PARTITIONS_SQL = """
SELECT c.oid, n.nspname AS schema, c.relname AS name,
       pg_get_expr(c.relpartbound, c.oid) AS bound_expr
FROM pg_inherits i
JOIN pg_class c ON c.oid = i.inhrelid
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE i.inhparent = %(oid)s
ORDER BY n.nspname, c.relname
"""

_RELATION_SQL = """
SELECT c.oid, c.relkind, c.relispartition, obj_description(c.oid, 'pg_class') AS comment
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = %(schema)s AND c.relname = %(name)s
"""


def read_parent(conn, schema: str, name: str) -> ParentInfo:
    """Resolve the parent table and validate it has the required shape."""
    row = conn.execute(_PARENT_SQL, {"schema": schema, "name": name}).fetchone()
    if row is None:
        raise CatalogError(f"table {schema}.{name} does not exist")
    oid, _nsp, _rel, relkind = row
    if relkind != "p":
        raise CatalogError(f"{schema}.{name} is not a partitioned table (relkind={relkind!r})")

    key = conn.execute(_PARTKEY_SQL, {"oid": oid}).fetchone()
    if key is None:
        raise CatalogError(f"{schema}.{name}: no pg_partitioned_table entry")
    partstrat, partnatts, attrs = key
    if partstrat != "r":
        raise CatalogError(f"{schema}.{name}: partitioning strategy is not RANGE")
    if partnatts != 1:
        raise CatalogError(
            f"{schema}.{name}: partition key has {partnatts} columns; "
            "this tool handles single-column keys only"
        )
    attnum = int(str(attrs).split()[0])
    attname, atttyp, attidentity, attgenerated = conn.execute(
        _ATTR_SQL, {"oid": oid, "attnum": attnum}
    ).fetchone()
    if atttyp not in KEY_TYPE_KEYWORD:
        raise CatalogError(
            f"{schema}.{name}: partition key column {attname!r} has type {atttyp!r}; "
            f"supported types: {sorted(KEY_TYPE_KEYWORD)}"
        )

    columns = []
    for colname, ident, gen in conn.execute(_COLUMNS_SQL, {"oid": oid}).fetchall():
        if ident or gen:
            raise CatalogError(
                f"{schema}.{name}: column {colname!r} is identity/generated; "
                "this tool refuses to move rows for such tables"
            )
        columns.append(colname)

    triggers = [r[0] for r in conn.execute(_TRIGGERS_SQL, {"oid": oid}).fetchall()]
    if triggers:
        raise CatalogError(
            f"{schema}.{name}: table has user triggers {triggers}; ATTACH would not "
            "propagate them to the new partition, so this tool refuses to proceed"
        )

    return ParentInfo(
        oid=oid, schema=schema, name=name,
        key_column=attname, key_type=atttyp, columns=columns,
    )


def read_partitions(conn, parent_oid: int) -> list:
    """All partitions of the parent, with bounds parsed from the catalog."""
    parts = []
    for oid, schema, name, bound_expr in conn.execute(
        _PARTITIONS_SQL, {"oid": parent_oid}
    ).fetchall():
        parts.append(
            PartitionInfo(oid=oid, schema=schema, name=name,
                          bound=parse_partition_bound(bound_expr))
        )
    return parts


def lookup_relation(conn, schema: str, name: str):
    """Return {oid, relkind, relispartition, comment} for schema.name, or None."""
    row = conn.execute(_RELATION_SQL, {"schema": schema, "name": name}).fetchone()
    if row is None:
        return None
    return {"oid": row[0], "relkind": row[1], "relispartition": row[2], "comment": row[3]}


def count_rows(conn, schema: str, name: str) -> int:
    return conn.execute(f"SELECT count(*) FROM {qi(schema, name)}").fetchone()[0]


def count_rows_in_range(conn, schema: str, name: str, key_column: str, start, end) -> int:
    """Exact count of rows whose key falls in [start, end)."""
    sql = (
        f"SELECT count(*) FROM {qi(schema, name)} "
        f"WHERE {qi(key_column)} >= %(start)s AND {qi(key_column)} < %(end)s"
    )
    return conn.execute(sql, {"start": start, "end": end}).fetchone()[0]
