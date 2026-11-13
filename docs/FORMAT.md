# 格式说明

## 1. 输入 CSV 语法

* **编码**：UTF-8。文件开头可选一个 UTF-8 BOM（`EF BB BF`，3 字节）。BOM 不属于任何
  逻辑记录，也不计入记录偏移。
* **记录**：第一条逻辑记录是表头，其余为数据记录。
* **字段**：逗号 `,` 分列。字段可以用双引号包围。字段内连续两个双引号 `""` 表示一个
  字面双引号。
* **引号内**：逗号、LF、CR、CRLF、中文等任意字节都属于字段内容，不结束记录。
* **记录终止符**：只接受 LF（`\n`，1 字节）或 CRLF（`\r\n`，2 字节）。单独的 CR 也按
  1 字节终止符处理（宽松兼容旧 Mac 风格）；它在字段外、非 CRLF 情况下终止记录。
* **空行**：完全空的一行是一个只含单个空字段的记录（字段数为 1）。
* **末记录**：文件最后一条记录可以没有终止符。
* **空文件**：没有任何逻辑记录，因此缺少表头 → 拒绝。仅 BOM 的文件同样缺少表头 → 拒绝。
* **仅表头**：合法，索引中数据记录数为 0。

### 非法输入（必须拒绝）

| 情形 | 报告位置 |
| --- | --- |
| 引号字段未闭合（含到 EOF） | 开始引号的字节偏移 |
| 未加引号字段中出现 `"` | 该双引号的字节偏移 |
| 闭合引号后、到分隔符/换行之间出现其它字符（含空格） | 该字符的字节偏移 |
| 单条逻辑记录超过 1 MiB | 记录起点 + 越限位置 |
| 数据记录字段数与表头不一致 | 该数据记录编号与起始偏移 |

错误报告包含：`header` 或 `data record N`（N 从 0 开始）、逻辑记录序号（表头为 1）、
绝对字节偏移，以及错误类别。

## 2. 偏移语义

* 所有偏移都是**绝对 0 基字节偏移**，从文件第一个字节算起（BOM 之后的第一条记录起点为 3）。
* `start` 含、`end` 不含：`[start, end)`。
* **终止符属于前一条记录**：例如 `a,b\n` 若 `start=3`，则 `end=7`，`term_len=1`。
* 无终止符的末记录 `term_len=0`，其 `end == source_size`。
* 表头单独登记在 `meta` 中（`header_start`、`header_end`、`header_term_len`、
  `header_fields`），不作为 `records` 表中的一行。

## 3. 索引文件格式（`format_version = "1"`）

索引是一个 SQLite 3 数据库（标准库 `sqlite3`），以只读方式打开供抽取/校验。

### `meta` 表（`key TEXT PRIMARY KEY, value TEXT NOT NULL`，WITHOUT ROWID）

| key | 含义 |
| --- | --- |
| `format_version` | 固定 `"1"` |
| `source_size` | 源文件字节数 |
| `source_sha256` | 源文件完整 SHA256（每个块流式计算） |
| `bom_len` | `0` 或 `3` |
| `header_start` | 表头记录起始绝对偏移 |
| `header_end` | 表头记录结束（不含），含其终止符 |
| `header_term_len` | 表头终止符长度：0/1/2 |
| `header_fields` | 表头字段数 |
| `data_record_count` | 数据记录条数 |
| `index_checksum` | 索引自身校验值（见下） |

### `records` 表（`record_no INTEGER PRIMARY KEY, start_off, end_off, term_len, fields`）

每行一条**数据记录**，`record_no` 从 0 连续编号。`start_off`/`end_off` 如上；`term_len` 为
0/1/2；`fields` 为该记录字段数（已保证与表头一致）。

结构不变量（verify 会检查）：记录编号连续、相邻记录首尾相接（`start == 前一条 end`）、
范围不超出文件、最后一条结束于 `source_size`、`term_len=0` 仅允许出现在 EOF、字段数一致。

### 自身校验值

`index_checksum` = SHA256(规范化文本)：

* 对 `meta` 中除 `index_checksum` 的所有键按键名升序，写 `meta\tKEY\tVALUE\n`；
* 再对 `records` 按 `record_no` 升序，每行写
  `record\tNO\tSTART\tEND\tTERM\tFIELDS\n`。

校验时重新计算并比较（常量时间比较）。任何 meta 或行被篡改都会被发现。另外还运行
SQLite `PRAGMA integrity_check`。

### 原子发布

`build` 在索引目标目录创建临时 SQLite 文件，全部成功并写入校验值后 `fsync`，再
`os.replace` 到目标路径并 `fsync` 目录。任何失败（语法错误、列数不符、记录超限、
源文件变化等）都会删除临时文件，**不触碰既有索引**。

## 4. 抽取语义

* `--ranges A:B` 为从 0 开始、左闭右开的数据记录编号区间，可给多个。
* 按请求顺序输出；重复区间重复输出（原始字节会被再次读取）。
* 输出只写一次表头（含 BOM 与表头原有终止符），然后按区间写原始记录字节。
* **补 LF**：如果源文件“无终止符的末记录”在输出中不在最后位置（后面还有记录），
  在该记录后补一个 LF，避免与下一条拼成一条逻辑记录。JSON 结果中
  `terminator_padding` 非空并在 `offset_in_output` 报告补入位置；其它情况为 `null`。
* 抽取前核对源文件大小与 SHA256；不匹配即拒绝，绝不使用旧索引读取已修改的文件。
* 输出先写同目录临时文件，全部成功并 `fsync` 后原子替换；失败时保留此前成功的文件。
