# 性能与内存：测量方法与实测

## 目标

* 输入大于 128 MiB。
* `build` 的峰值常驻内存（RSS）≤ 64 MiB，且内存不随文件大小或记录数增长。
* `extract` 除一次完整身份核验（读取全文件计算 SHA256）外，只读取请求记录范围，
  不重新解析全部 CSV。

## 测量方法

脚本：`scripts/perf_check.py`（全部在本地生成数据，不访问外部服务）。

1. **生成**：`python3 -m csvslice generate --target-size 130MiB --chunk-size 2048`，
   生成约 130 MiB 的 UTF-8 CSV（含中文、多行字段、转义引号）。
2. **子进程隔离**：每个被测步骤都作为独立子进程运行
   `python3 -m csvslice build|extract ...`。
3. **峰值 RSS**：父进程在子进程运行期间每 20 ms 读取
   `/proc/<pid>/status` 的 `VmHWM`（high-water mark，峰值常驻集），取最大值。
   这比 `resource.getrusage(RUSAGE_CHILDREN).ru_maxrss` 更精确：后者是所有已回收
   子进程的历史最大值，不能保证归属到当前步骤。脚本也只把子进程视为被测对象，
   父进程自身的 Python 开销不计入。
4. **判定**：`VmHWM ≤ 65536 KiB` 为通过；同时断言文件大小 > 128 MiB。
5. **抽取读取量**：`extract` 的 JSON 输出包含 `source_bytes_read_for_records`
   （实际从源文件读取的记录字节数）与 `source_read_ops`（读取次数）。单元测试
   `tests/test_extract.py::ReadAccountingTests` 进一步用记录了 `(pos, size)` 的文件
   对象包装源文件，断言读取字节数恰好等于“表头 + 请求记录范围”，且远小于全文件。

## 内存为什么有界

* `build` 按固定块（默认 1 MiB，可配）读文件，扫描器状态是与记录/文件长度无关的
  常数状态；每 4096 条记录批量插入 SQLite，批次随即清空。
* SQLite 缓存显式限制为 4 MiB（`PRAGMA cache_size=-4096`），`mmap_size=0`。
* 单条逻辑记录硬上限 1 MiB，超限立即抛错，因此单条记录对应的临时字节量也有界。
* `extract` 逐条记录 `seek` + `read` 写入，不缓存整段文件；仅身份核验阶段按 1 MiB
  块流式计算 SHA256。

## 实测结果（本机，Python 3.11）

```
workdir: /tmp/csvslice-perf-...
generated: 136313909 bytes (130.00 MiB) in 1.4s
build: 1.57s, peak RSS 23.6 MiB (24204 KiB), index 1.70 MiB
extract: 0.66s, peak RSS 23.1 MiB, ranges '0:3 85173:85176'
extract read accounting: 8880 record bytes read in 6 operations (source is 136313909 bytes)
fixture > 128 MiB: PASS
build peak RSS <= 64 MiB: PASS (24204 KiB <= 65536 KiB)
extract peak RSS <= 64 MiB: PASS (23676 KiB <= 65536 KiB)
index/source size ratio: 0.013 (data records: 85176)
RESULT: PASS
```

结论：130 MiB 输入建索引峰值 RSS 约 23.6 MiB，远低于 64 MiB；抽取在两个小区间上
只读取约 8.7 KiB 的源数据（另有一次性全文件 SHA256 核验），未重新解析全 CSV。

复现：

```bash
cd /workspace
python3 scripts/perf_check.py --size 130MiB
# 想保留样例文件以便检查：
python3 scripts/perf_check.py --size 130MiB --keep
```
