# NDJSON 帧协议

网关对每个 `POST /query` 请求返回 `Content-Type: application/x-ndjson` 的响应体：
一行一个 JSON 帧（UTF-8），帧顺序固定为

```
meta | rows* | terminal
```

`rows` 可以有零到多帧；`terminal` 有且仅有一帧，并且一定是最后一帧。
客户端**只有读到 `terminal` 且 `status == "success"` 时才能把结果标记为完成**；
读到部分 `rows` 但没有成功终态（断连、取消、报错、超预算）一律视为未完成。

## 1. meta 帧（首帧）

```json
{
  "type": "meta",
  "request_id": "8f16fdb87b1f47798e1fbd08749c3f1e",
  "query_id": "point_lookup",
  "columns": [
    {"name": "event_id", "type_oid": 20, "type_name": "int8", "display_size": null}
  ],
  "params": {"event_id": 42424}
}
```

- `columns` 给出列名、PostgreSQL 类型 OID 与类型名。
- `params` 回显服务端已绑定的参数，供导出结果独立重放核对；客户端无法借此修改 SQL。

## 2. rows 帧（数据帧，可多帧）

```json
{
  "type": "rows",
  "seq": 0,
  "row_count": 2,
  "rows": [
    [
      {"t": "int8", "v": 42424},
      {"t": "numeric", "v": "271.76"},
      {"t": "timestamptz", "v": "2026-01-19T04:01:28.000000+00:00"},
      {"t": "date", "v": "2026-02-12"},
      {"t": "bytea", "v": "qgWluA=="},
      null
    ]
  ]
}
```

每个单元格是带类型标签的对象 `{"t": <类型>, "v": <值>}`，`NULL` 直接是 JSON `null`。
标签由**列的 PostgreSQL 类型 OID** 决定，而不是按值的大小猜测，因此 int8 列即使值很小
仍标记为 `int8`，NUMERIC 保留十进制文本避免二进制浮点误差。

| 标签 | PostgreSQL 类型 | 线上承载 | 客户端还原类型 |
| --- | --- | --- | --- |
| `int2` / `int4` / `int8` | int2/int4/int8 | JSON number | Python `int` |
| `numeric` | numeric | 十进制字符串 | Python `decimal.Decimal` |
| `float4` / `float8` | real/double | JSON number | Python `float` |
| `bool` | bool | JSON bool | Python `bool` |
| `text` | text/name/varchar 等 | JSON string | Python `str` |
| `timestamptz` | timestamptz/timestamp | ISO 8601 字符串 | aware `datetime` |
| `date` | date | `YYYY-MM-DD` | `datetime.date` |
| `bytea` | bytea | base64 字符串 | Python `bytes` |
| `json` | json/jsonb | JSON 对象/数组 | dict/list |
| `null`（无标签） | SQL NULL | JSON `null` | Python `None` |

## 3. terminal 帧（唯一终态）

```json
{
  "type": "terminal",
  "request_id": "8f16...",
  "status": "success",
  "rows_sent": 120000,
  "bytes_sent": 32120055
}
```

失败终态额外带 `detail`，数据库错误还带 `sqlstate`：

```json
{"type": "terminal", "status": "query_error", "sqlstate": "22012",
 "detail": "division by zero", "rows_sent": 0, "bytes_sent": 0}
```

终态 `status` 取值：

| status | 含义 |
| --- | --- |
| `success` | 完整成功，`rows_sent`/`bytes_sent` 为已发送统计 |
| `row_budget_exceeded` | 超过目录行数上限 |
| `byte_budget_exceeded` | 超过目录字节预算 |
| `time_limit_exceeded` | 超过目录执行时间预算（应用侧或 `statement_timeout`） |
| `query_error` | 数据库执行报错（含 sqlstate） |
| `cancelled` | 被取消接口或停机流程取消 |
| `client_disconnected` | 客户端断开，终态无法送达，仅记录在服务状态 |
| `send_failed` | 网络发送失败，终态无法送达，仅记录在服务状态 |

## 4. 非 200 的同步拒绝（没有帧）

以下情况在任何帧写出之前就以普通 JSON 响应拒绝：

- `503 {"error":"pool_queue_full"}`：连接池与等待队列均已满，快速拒绝；
- `503 {"error":"shutting_down"}`：服务停止中，拒绝新请求；
- `400 {"error":"bad_params"}`：参数缺失或类型不符；
- `404`：未知 `query_id`。

## 5. 背压与终态送达保证

- 服务端生产者与网络消费者之间是容量为 `frame_queue_limit` 的有界 `asyncio.Queue`。
  队列满时生产者停止 `fetchmany`，因此慢客户端不会导致完整结果物化进 Python 列表。
- 客户端断开时，`client_disconnected` / `send_failed` 终态无法写回连接，
  但一定会记录在 `GET /requests/{id}/status` 与诊断日志中（`terminal_delivered=false`）。
- 取消通过独立连接向 PostgreSQL 发送 CancelRequest，只作用于目标 `request_id`，
  复用同一 TCP 连接的其它 HTTP 请求不受影响。
