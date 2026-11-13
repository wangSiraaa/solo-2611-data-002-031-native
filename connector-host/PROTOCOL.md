# 通信协议说明（JSON-RPC 2.0 over stdin/stdout）

宿主与每个连接器通过子进程的 **stdin / stdout** 交换 JSON-RPC 2.0 消息；
连接器的 **stderr 只用于诊断日志**，不承载任何协议数据。

## 1. 分帧（Framing）

每条消息由 ASCII 头、空行、UTF-8 JSON 主体组成：

```
Content-Length: <N>\r\n
\r\n
<恰好 N 字节的 JSON>
```

规则：

1. `Content-Length` 的值是 JSON 主体的**字节数，不是字符数**。中文在 UTF-8 中占 3 字节。
2. 头与主体之间用 `\r\n\r\n` 分隔（读取端兼容仅 `\n` 的宽松实现）。
3. 一次管道读取可能得到：多条粘连消息、半条头、半条 JSON 主体，甚至半个多字节字符。
   两端都使用增量字节缓冲（宿主 `host/framing.py`、连接器 `connectors/common.py` 各自独立实现），
   不依赖 read 边界。
4. 每条消息必须是 JSON 对象，且带 `"jsonrpc": "2.0"`。
5. 超过连接器配置 `max_message_bytes` 的**声明长度**在读取主体前即被拒绝，
   错误携带连接器名与 stdout 字节偏移。

## 2. 消息类型

### 2.1 请求（宿主 → 连接器），带 `id`

| 方法 | 说明 |
|---|---|
| `initialize` | 握手，连接器返回能力声明 |
| `query` | 执行采集查询 |
| `status` | 查询连接器运行态 |

### 2.2 响应（连接器 → 宿主），带 `id`

- 成功：`{"jsonrpc":"2.0","id":<同请求 id>,"result":{...}}`
- 失败：`{"jsonrpc":"2.0","id":<同请求 id>,"error":{"code":-32602,"message":"..."}}`

**响应严格按 `id` 关联原请求，绝不按到达顺序配对。** 同一连接器进程可同时存在
多个在途请求（见 reorder 连接器的相反完成顺序演示）。

### 2.3 通知（无 `id`，因此没有响应）

| 方向 | 方法 | 说明 |
|---|---|---|
| 连接器 → 宿主 | `progress` | 纯采集进度，**不含记录**，不能被当作查询结果 |
| 连接器 → 宿主 | `dataBatch` | 一批查询数据，带 `queryId` 与递增 `batchNo` |
| 宿主 → 连接器 | `cancel` | 取消请求，参数为 `queryId` |

通知没有响应 id，宿主不会把任何通知与某个查询的终态结果配对。

## 3. initialize 能力声明

```json
{
  "connector": "fast",
  "capabilitiesVersion": "1.0.0",
  "supportsCancel": true,
  "sources": {
    "fast_orders": {
      "fields": ["id", "name", "region", "category", "amount", "score", "ts"],
      "filterable": ["region", "category", "amount", "score"],
      "queryParams": ["fields", "filters", "batchSize"],
      "description": "..."
    }
  },
  "filterOps": ["eq", "ne", "gt", "ge", "lt", "le", "in", "contains"]
}
```

## 4. query 与分批结果

请求参数：

```json
{
  "queryId": "q-abc123",
  "source": "fast_orders",
  "fields": ["id", "name", "region"],
  "filters": {"region": {"eq": "华北"}, "amount": {"gt": 100}}
}
```

- `fields` 为投影字段列表；空列表表示全部字段。请求未声明的字段会被连接器拒绝。
- `filters` 为受控过滤：只允许能力声明中 `filterable` 的字段与固定 8 个操作符。

数据批通知（`batchNo` 从 1 开始、严格递增、连续不跳号）：

```json
{"jsonrpc":"2.0","method":"dataBatch",
 "params":{"queryId":"q-abc123","batchNo":1,"records":[ {...}, {...} ]}}
```

终态响应（声明总批数与总记录数）：

```json
{"jsonrpc":"2.0","id":7,"result":{
  "queryId":"q-abc123","status":"completed",
  "batchesTotal":5,"recordsTotal":200}}
```

宿主把查询标记为完整**之前**必须核对：

1. 每个 `dataBatch` 的 `queryId` 属于在途查询（防串查询）；
2. `batchNo` 严格等于期望的下一个序号（防乱序、重复、跳号、串批）；
3. 终态 `batchesTotal` 等于实际连续到齐的批数；
4. 终态 `recordsTotal` 等于逐批累计保存的记录数。

任一不符即协议错误：失败该查询并按策略终止该连接器，已核对完成的其他查询结果保留。

## 5. 取消与竞争

- 取消是独立的 `cancel` 通知，不是对某个请求 id 的响应。
- `supportsCancel=false` 的连接器（reorder）不接受取消；查询超时后宿主直接结束其进程，
  再报告受影响请求，且不影响其他连接器。
- `supportsCancel=true` 时，超时先发 cancel，给 `cancel_grace_s` 宽限期，仍无终态才结束进程。
- **取消与终态同时到达的竞争**只有一个明确终态：
  - 终态成功响应先被宿主核对 → `completed (cancel request lost the race)`；
  - 连接器在终态前确认取消 → `cancelled (cancel won the race)`。
  终态唯一，宿主在 status / 导出清单中明确展示实际采用的终态。

## 6. 协议错误处置

| 错误 | 定位信息 | 处置 |
|---|---|---|
| 未知响应 id | 连接器 + 偏移 | 终止该连接器 |
| 未知 queryId 的通知/终态 | 连接器 + 偏移 | 终止该连接器 |
| 批序错误 / 重复终态 / 归属不符 | 连接器 + 偏移 | 失败查询并终止连接器 |
| 声明长度超 `max_message_bytes` | 连接器 + 偏移 | 不读主体，立即终止 |
| 非法 UTF-8 / 非法 JSON | 连接器 + 偏移 | 终止该连接器 |
| 数据超 `data_cap_bytes` | 连接器 + 偏移 | 失败查询并终止连接器 |
| 子进程半途退出 | 退出码 | 在途请求全部明确失败，已完成结果保留 |

终止动作只作用于**当前连接器**；其他连接器的进程、管道与查询完全不受影响。
