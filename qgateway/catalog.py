"""查询目录：query_id -> 固定 SQL、绑定参数类型、行数/字节/时间预算。

客户端只能提交 query_id 与参数值，不能上传 SQL。所有 SQL 与数据均由项目生成。
预算在服务端强制执行，客户端无法覆盖。
"""
from __future__ import annotations

import datetime as _dt
import decimal
from dataclasses import dataclass
from typing import Any

# 参数类型 -> psycopg 占位说明。校验后以原生 Python 值交给驱动绑定。
INT = "int"
BIGINT = "bigint"
NUMERIC = "numeric"
TIMESTAMPTZ = "timestamptz"
TEXT = "text"
BOOL = "bool"


@dataclass(frozen=True)
class ParamSpec:
    name: str
    pg_type: str
    nullable: bool = False


@dataclass(frozen=True)
class QueryEntry:
    query_id: str
    title: str
    sql: str
    params: tuple[ParamSpec, ...]
    # 服务端预算（客户端不可配置）
    row_limit: int
    byte_budget: int
    time_limit_seconds: float
    read_only: bool = True


_QUERIES: dict[str, QueryEntry] = {}


def _register(entry: QueryEntry) -> None:
    _QUERIES[entry.query_id] = entry


# 1) 快速点查：主键单行，覆盖 int8/numeric/timestamptz/date/NULL
_register(
    QueryEntry(
        query_id="point_lookup",
        title="快速主键点查",
        sql="""
            SELECT e.event_id, e.tenant_id, e.kind_id, e.amount, e.event_time,
                   e.payload, e.occurred_on, e.notes
              FROM gateway.events e
             WHERE e.event_id = %s
        """,
        params=(ParamSpec("event_id", BIGINT),),
        row_limit=1,
        byte_budget=64 * 1024,
        time_limit_seconds=2.0,
    )
)

# 2) 多表聚合：events 与 event_kinds 连接 + GROUP BY；kind_id 允许 NULL 参数
_register(
    QueryEntry(
        query_id="kind_aggregate",
        title="多表分组聚合",
        sql="""
            SELECT k.kind_id,
                   k.kind_name,
                   count(*)                              AS event_count,
                   sum(e.amount)                         AS total_amount,
                   max(e.event_time)                     AS last_event_time
              FROM gateway.events e
              JOIN gateway.event_kinds k ON k.kind_id = e.kind_id
             WHERE (%s::bigint IS NULL OR e.kind_id = %s)
               AND e.event_time >= %s
               AND e.event_time <  %s
             GROUP BY k.kind_id, k.kind_name
             ORDER BY k.kind_id
        """,
        # 第 1、2 个占位符复用 kind_id（NULL 可识别），由 bind_positions 映射
        params=(
            ParamSpec("kind_id", BIGINT, nullable=True),
            ParamSpec("kind_id", BIGINT, nullable=True),
            ParamSpec("start_time", TIMESTAMPTZ),
            ParamSpec("end_time", TIMESTAMPTZ),
        ),
        row_limit=16,
        byte_budget=256 * 1024,
        time_limit_seconds=5.0,
    )
)

# 3) 大结果集：参数化有界扫描，演示至少十万行的流式读取
_register(
    QueryEntry(
        query_id="large_scan",
        title="大结果集有界扫描",
        sql="""
            SELECT e.event_id, e.tenant_id, e.kind_id, e.amount,
                   e.event_time, e.payload, e.occurred_on, e.notes
              FROM gateway.events e
             WHERE e.event_id BETWEEN %s AND %s
             ORDER BY e.event_id
        """,
        params=(
            ParamSpec("start_id", BIGINT),
            ParamSpec("end_id", BIGINT),
        ),
        row_limit=120_000,
        byte_budget=256 * 1024 * 1024,
        time_limit_seconds=30.0,
    )
)

# 4) 可控制为慢执行的只读查询：sleep_seconds 由参数驱动，预算在目录里固定
_register(
    QueryEntry(
        query_id="slow_read",
        title="可控慢只读查询",
        sql="""
            SELECT e.event_id, e.amount, e.event_time
              FROM gateway.events e
              CROSS JOIN LATERAL pg_sleep(%s::double precision) AS s
             WHERE e.event_id BETWEEN %s AND %s
             ORDER BY e.event_id
        """,
        params=(
            ParamSpec("sleep_seconds", NUMERIC),
            ParamSpec("start_id", BIGINT),
            ParamSpec("end_id", BIGINT),
        ),
        row_limit=100_000,
        byte_budget=128 * 1024 * 1024,
        # 目录固定时间上限，演示 SQL 超时终态（客户端无法改）
        time_limit_seconds=3.0,
    )
)

# 4b) 阻塞占用查询：与 slow_read 同构但时间预算更宽，用于确定性占满连接池/排队/取消
_register(
    QueryEntry(
        query_id="blocking_hold",
        title="可控阻塞只读查询（池占用/排队/取消演示）",
        sql="""
            SELECT e.event_id, e.amount, e.event_time
              FROM gateway.events e
              CROSS JOIN LATERAL pg_sleep(%s::double precision) AS s
             WHERE e.event_id BETWEEN %s AND %s
             ORDER BY e.event_id
        """,
        params=(
            ParamSpec("sleep_seconds", NUMERIC),
            ParamSpec("start_id", BIGINT),
            ParamSpec("end_id", BIGINT),
        ),
        row_limit=100_000,
        byte_budget=128 * 1024 * 1024,
        time_limit_seconds=60.0,
    )
)

# 5) 故障查询：denominator=0 触发 division_by_zero（22012），
#    用于验证 SQL 失败后同一池连接被清理并能再次成功借用
_register(
    QueryEntry(
        query_id="faulty_divide",        title="可触发 SQL 执行错误的只读查询",
        sql="""
            SELECT e.event_id,
                   e.amount / (%s::numeric) AS ratio
              FROM gateway.events e
             WHERE e.event_id BETWEEN %s AND %s
             ORDER BY e.event_id
        """,
        params=(
            ParamSpec("denominator", NUMERIC),
            ParamSpec("start_id", BIGINT),
            ParamSpec("end_id", BIGINT),
        ),
        row_limit=1_000,
        byte_budget=4 * 1024 * 1024,
        time_limit_seconds=5.0,
    )
)


def get(query_id: str) -> QueryEntry:
    if query_id not in _QUERIES:
        raise KeyError(query_id)
    return _QUERIES[query_id]


def catalog_snapshot() -> list[dict[str, Any]]:
    result = []
    for q in _QUERIES.values():
        # 对外只展示唯一参数名（重复占位符是实现细节）
        seen: dict[str, dict[str, Any]] = {}
        for p in q.params:
            seen.setdefault(
                p.name, {"name": p.name, "pg_type": p.pg_type, "nullable": p.nullable}
            )
        result.append(
            {
                "query_id": q.query_id,
                "title": q.title,
                "params": list(seen.values()),
                "row_limit": q.row_limit,
                "byte_budget": q.byte_budget,
                "time_limit_seconds": q.time_limit_seconds,
                "read_only": q.read_only,
            }
        )
    return result


def _coerce_int(raw: Any) -> int:
    if isinstance(raw, bool):
        raise ValueError("布尔值不能作为整数参数")
    if isinstance(raw, int):
        return raw
    if isinstance(raw, str) and raw.strip().lstrip("-").isdigit():
        return int(raw.strip())
    raise ValueError(f"需要整数，收到 {raw!r}")


def _coerce_numeric(raw: Any) -> decimal.Decimal:
    if isinstance(raw, bool):
        raise ValueError("布尔值不能作为 NUMERIC 参数")
    try:
        return decimal.Decimal(str(raw))
    except (decimal.InvalidOperation, ValueError) as exc:
        raise ValueError(f"需要 NUMERIC，收到 {raw!r}") from exc


def _coerce_timestamptz(raw: Any) -> _dt.datetime:
    if isinstance(raw, _dt.datetime):
        dt = raw
    elif isinstance(raw, str):
        try:
            dt = _dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(f"需要 ISO 8601 时间戳，收到 {raw!r}") from exc
    else:
        raise ValueError(f"需要时间戳字符串，收到 {raw!r}")
    if dt.tzinfo is None:
        # naive 输入按 UTC 解释，保证协议里时间类型明确
        dt = dt.replace(tzinfo=_dt.timezone.utc)
    return dt


def bind_params(entry: QueryEntry, raw_params: Any) -> list[Any]:
    """按目录声明校验并转换参数，返回驱动绑定所需的原生 Python 值列表。

    ParamSpec 允许重名（同一命名参数被多个占位符复用），对客户端仍只暴露唯一参数名；
    每个占位符位置都会得到一个独立绑定值，由驱动完成参数绑定。
    """
    if not isinstance(raw_params, dict):
        raise ValueError("params 必须是对象")
    # 先按唯一参数名完成一次校验/转换
    converted: dict[str, Any] = {}
    unique_names = list(dict.fromkeys(spec.name for spec in entry.params))
    for name in unique_names:
        spec = next(s for s in entry.params if s.name == name)
        if name not in raw_params:
            raise ValueError(f"缺少参数 {name}")
        raw = raw_params[name]
        if raw is None:
            if not spec.nullable:
                raise ValueError(f"参数 {name} 不允许 NULL")
            converted[name] = None
            continue
        if spec.pg_type in (INT, BIGINT):
            converted[name] = _coerce_int(raw)
        elif spec.pg_type == NUMERIC:
            converted[name] = _coerce_numeric(raw)
        elif spec.pg_type == TIMESTAMPTZ:
            converted[name] = _coerce_timestamptz(raw)
        elif spec.pg_type == TEXT:
            converted[name] = str(raw)
        elif spec.pg_type == BOOL:
            converted[name] = bool(raw)
        else:  # pragma: no cover - 目录内部一致性
            raise ValueError(f"不支持的参数类型 {spec.pg_type}")
    # 再按占位符顺序展开（重名参数复用同一转换结果）
    return [converted[spec.name] for spec in entry.params]
