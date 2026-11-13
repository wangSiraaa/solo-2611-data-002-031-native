# 本地运行与运维手册

## 环境

* Linux，Python 3.11（仅标准库：`sqlite3`、`hashlib`、`argparse` 等）。
* 无需数据库服务、无需联网；所有样例由 `generate` 本地生成。

在本仓库根目录（`/workspace`）运行所有命令。若使用虚拟环境：

```bash
cd /workspace
python3 -m venv .venv
. .venv/bin/activate
python3 --version   # 期望 3.11.x
```

本工具没有第三方依赖，无需 `pip install`。

## 完整流程

### 1. 生成样例

```bash
python3 -m csvslice generate data.csv \
    --rows 100 \
    --bom \
    --multiline-every 9 \
    --quoted-comma-every 4 \
    --quoted-quote-every 5 \
    --no-final-newline
```

大文件（用于性能验证）：

```bash
python3 -m csvslice generate big.csv --target-size 130MiB --chunk-size 2048
```

`generate` 输出 JSON：`bytes`、`data_records`、`bom`、`seed`。

### 2. 建索引

```bash
python3 -m csvslice build data.csv data.idx --chunk-size 65536
```

* 块大小 `--chunk-size` 只影响扫描 I/O 分块，不影响索引偏移；可随时调整。
* 逻辑记录上限固定为 1 MiB（`--max-record-bytes` 只能调小，不能超过 1 MiB）。
* 索引写入成功后才会原子替换目标；失败不会破坏已有索引。
* 输出 JSON 含 `source_size`、`source_sha256`、表头偏移/字段数、`data_records`。

成功返回 0；以下情况返回 1 并在 stderr 给出一行原因：

| 现象 | 原因 |
| --- | --- |
| `source contains no header record` | 空文件或仅 BOM |
| `unterminated quoted field` | 引号未闭合（带偏移） |
| `double quote inside an unquoted field` | 未引号字段中的引号 |
| `unexpected character after a closing quote` | 闭合引号后有多余字符 |
| `data record N ... has X field(s), header has Y` | 列数不一致，索引未发布 |
| `exceeds the 1048576-byte logical-record limit` | 单条记录超 1 MiB |

### 3. 校验

```bash
python3 -m csvslice verify data.idx --source data.csv
```

* 不带 `--source`：校验索引自身（SQLite 完整性、结构不变量、自身 SHA256 校验值）。
* 带 `--source`：再核对源文件大小与 SHA256，确认索引没有过期。

### 4. 抽取复核材料

```bash
python3 -m csvslice extract data.csv data.idx review.csv \
    --ranges 0:60 300:420 60:80
```

* `--ranges` 可重复给多个，顺序即输出顺序；重复区间会重复输出。
* 默认不覆盖已存在输出，替换时加 `--force`。
* 若源文件无终止符的末记录被放在非末位置，会自动补一个 LF，并在 JSON
  的 `terminator_padding` 中报告 `added_bytes` 与 `offset_in_output`。
* 源文件与索引不匹配时直接失败（退出码 1）。

抽取 JSON 中与审计相关的字段：

| 字段 | 含义 |
| --- | --- |
| `records_written` | 实际写出的记录条数 |
| `bytes_written` | 输出文件总字节数 |
| `header_bytes` | 表头（含 BOM/终止符）字节数 |
| `source_bytes_read_for_records` | 除身份核验外，从源文件读取的记录字节数 |
| `source_read_ops` | 对应的读取次数 |
| `terminator_padding` | 补 LF 信息或 `null` |

## 测试

```bash
cd /workspace
python3 -m unittest discover -s tests -v
```

约 85 个用例，覆盖：

* 扫描器与独立参照解析器（`tests/csvref.py`）在 1..1024 字节块下逐记录一致；
* 中文跨块、CR/LF 分属两块、转义双引号跨块、字段内多行、空字段、空文件、
  仅表头、末行无换行；
* 每种非法引号的最小构造与错误偏移断言；
* 不同块大小索引偏移完全一致；
* 连续区间拼接等于整段读取、单条切片内容用独立解析器核对；
* 源文件修改（同大小与不同大小）均被拒绝；
* 索引校验值被篡改、SQLite 损坏被识别；
* 抽取失败保留既有成功输出、无临时文件残留；
* 抽取读取量恰好等于请求范围。

## 性能验证

```bash
cd /workspace
python3 scripts/perf_check.py --size 130MiB
```

期望输出 `RESULT: PASS`，且：

* 文件 > 128 MiB；
* `build` / `extract` 的 `VmHWM ≤ 64 MiB`；
* `extract read accounting` 显示的记录读取字节数远小于源文件大小。

`--keep` 可保留样例目录，`--workdir DIR` 可指定位置（建议放在 `.runtime/` 下，
该目录已在 `.gitignore` 中忽略）。测量方法见 [PERFORMANCE.md](PERFORMANCE.md)。

## 目录结构

```
csvslice/
  scanner.py     # 字节级流式状态机（跨块引号/CRLF 处理、1 MiB 上限）
  index.py       # SQLite 索引：build / verify / IndexReader
  extract.py     # 区间抽取、补 LF、原子替换
  generate.py    # 本地样例生成
  cli.py         # generate/build/extract/verify 命令
  errors.py      # 用户可见错误类型
tests/           # unittest 用例与独立参照解析器
scripts/perf_check.py
docs/
```

## 运维注意事项

* 索引是自包含 SQLite 文件，可用 `verify` 独立检查；建议在发布复核材料前先 `verify`。
* 索引与源文件应成对保存；源文件任何改动都会使 `extract`/`verify --source` 失败，
  此时需重建索引。
* 抽取与建索引都使用临时文件 + `os.replace`，在正常文件系统上具备原子性。
* 不要将 `index.db` 放在网络文件系统上依赖其锁语义；本工具以单写者、只读并发读取为设计前提。
