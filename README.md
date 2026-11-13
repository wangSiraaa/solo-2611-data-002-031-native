# csvslice — 大型离线 CSV 的本地索引与区间切片

`csvslice` 只依赖 Python 3.11 标准库，把大型 UTF-8 CSV 按**逻辑记录**建立字节级索引，
然后按记录区间（从 0 开始、左闭右开）抽取原始字节，用于复核材料。

关键点：字段内的换行不是记录边界。扫描器是字节级状态机，引号内的逗号、LF、CRLF
和中文都算字段内容；只有引号外的 LF / CRLF 才终止记录。

## 命令

```
python3 -m csvslice generate <output.csv> [选项]
python3 -m csvslice build    <source.csv> <index.db> [--chunk-size BYTES] [--max-record-bytes N]
python3 -m csvslice verify   <index.db> [--source <source.csv>]
python3 -m csvslice extract  <source.csv> <index.db> <output.csv> --ranges A:B [C:D ...] [--force]
```

所有命令把结果以 JSON 打印到 stdout；可预期的失败打印一行错误到 stderr 并以退出码 1 结束。

## 快速开始（完全本地，无外部服务）

```bash
cd /workspace

# 1) 生成样例（含中文、多行字段、转义引号、BOM、末行无换行等）
python3 -m csvslice generate /tmp/demo.csv --rows 50 \
    --multiline-every 7 --quoted-comma-every 5 --quoted-quote-every 6

# 2) 流式建索引（块大小可调；索引先写临时文件，成功后才原子发布）
python3 -m csvslice build /tmp/demo.csv /tmp/demo.idx --chunk-size 65536

# 3) 校验索引自身校验值，并核对源文件大小与 SHA256
python3 -m csvslice verify /tmp/demo.idx --source /tmp/demo.csv

# 4) 抽取：按请求顺序输出区间 [0,3) 与 [10,12) 的记录，重复区间也保留
python3 -m csvslice extract /tmp/demo.csv /tmp/demo.idx /tmp/slice.csv \
    --ranges 0:3 10:12
```

`extract` 默认拒绝覆盖已存在的输出文件，确认要替换时加 `--force`。

## 测试

```bash
cd /workspace
python3 -m unittest discover -s tests -v
```

测试覆盖：不同块大小下索引偏移一致、独立 CSV 解析器核对切片内容、连续区间拼接
等于原文读取、每种非法引号错误位置、空文件/仅表头/末行无换行，以及原子替换与
源文件被修改后拒绝读取等。

## 性能与内存测量

```bash
cd /workspace
python3 scripts/perf_check.py --size 130MiB
```

脚本生成大于 128 MiB 的样例，分别测量 `build` 和 `extract` 子进程的 `VmHWM`
（来自 `/proc/<pid>/status` 的峰值常驻内存），要求均不超过 64 MiB，并报告抽取
真正读取的源字节数。方法细节见 [docs/PERFORMANCE.md](docs/PERFORMANCE.md)。

## 文档

* [docs/FORMAT.md](docs/FORMAT.md) — CSV 语法、偏移语义、索引格式与校验值
* [docs/PERFORMANCE.md](docs/PERFORMANCE.md) — 内存/读取量测量方法与实测结果
* [docs/RUNBOOK.md](docs/RUNBOOK.md) — 完整本地运行、失败排查与运维步骤
