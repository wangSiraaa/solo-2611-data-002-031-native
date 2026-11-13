# orderimp — 嵌套批次订单 XML 本地导入器

Python 3.11，仅标准库。事件式解析（expat）+ SQLite 暂存，面向超过 128 MiB 的大型
`urn:orders:v1` 导出文件，峰值 RSS 控制在 96 MiB 以内。

## 快速开始

```bash
# 1. 生成全部本地合成样例（含约 179 MiB 的 big.xml；--skip-big 可跳过）
python3 importer.py generate --out samples

# 2. 导入一个文件并发布为新版本（块大小可调，结果不变）
python3 importer.py import samples/small.xml --data-dir data --chunk-size 65536

# 3. 查看当前已发布版本（清单 + 各数据集抽样 + 拒绝汇总）
python3 importer.py inspect --data-dir data

# 4. 运行完整自检（生成样例→导入→断言；--skip-big 跳过 128 MiB 内存测试）
python3 importer.py verify
```

退出码：`0` 成功；`2` 文档级失败（未发布任何数据）；`1` 其他错误。

## 业务规则实现要点

- 根元素固定为 `{urn:orders:v1}export`；`batch` 可嵌套，`id` 全局唯一非空。
- `customer`/`order` 归属最近的 `batch` 祖先；不同 batch 可有相同 customer_id；
  订单只解析本 batch 内的客户（不继承父批次）；客户可晚于订单出现（前向引用，
  全文解析完成后统一解析引用）。
- 其他命名空间的子树整体忽略，其中的伪订单不予识别；业务命名空间内的未知
  元素同样忽略并计入诊断。
- `customer`：`id`、`name` 必填非空；`order`：`id`、`customer_id` 必填，至少一行；
  `line`：`sku` 非空、`qty` 为正整数、`unit_cents` 为非负整数（严格十进制，
  不接受符号/空白/小数）。
- 客户 id、订单 id 在各自 batch 内唯一；重复身份（含 batch id）使**整个输入失败**。
- 坏记录**整单隔离**：坏明细使整单隔离（不保留其余合法明细）；批次结束后仍无
  有效客户的订单整单隔离；无效客户被记拒绝，引用它的订单按 `customer_rejected`
  隔离。`order` 内禁止再嵌套 `order`/`batch`（违反则该单隔离，子树整体忽略）。
- 订单总金额 = 各行 `qty × unit_cents` 的精确整数和（Python 任意精度整数，
  不经浮点/64 位截断）。
- 拒绝报告含批次、客户/订单标识与文档出现序号（`seq`，customer/order 统一计数）。

## 输出与版本发布

每次成功导入发布一个不可变版本目录，`current` 符号链接原子切换：

```
data/
  versions/v000001/{customers,orders,lines,rejects}.jsonl, manifest.json
  versions/v000002/...
  current -> versions/v000002
```

- 三份数据（customers/orders/lines）与诊断（rejects + manifest）作为**一个完整
  版本**同时发布；解析或校验失败时不产生新版本，旧版本仍可读取。
- `rejects.jsonl`：`{"kind","batch_id","record_id","seq","reason"[,"detail"]}`。
- `manifest.json`：版本、源文件 sha256/大小、计数、总金额、拒绝分类汇总、
  忽略子树诊断、限值参数。
- 文档级失败（不发布）：XML 格式错误/截断/未闭合、编码错误、DTD 或实体声明、
  根元素错误、嵌套深度超过 32、重复身份、记录出现在 batch 之外。

## 资源限值

- 嵌套深度 ≤ 32（超出为文档级失败）；单个 customer/order 子树 ≤ 64 KiB
  （超出则该记录隔离，文件其余部分继续）。
- 整份文件可超过 128 MiB：流式解析不建树，暂存于磁盘 SQLite（页缓存 2 MiB），
  实测 179 MiB 文件导入峰值 RSS 约 30 MiB（见下）。

## 自检内容（`python3 importer.py verify`）

| 验证项 | 样例 |
|---|---|
| 前向引用、同名客户跨作用域、嵌套批次、父批次客户不可继承 | `small.xml` |
| 命名空间前缀替换结果一致（输出逐字节相同） | `small_prefixed.xml` |
| 扩展子树/伪订单/未知业务标签忽略 | `extension.xml` |
| 坏明细整单隔离、缺字段、嵌套违规、64 KiB 子树上限 | `bad_records.xml` |
| 文件尾截断、DTD、实体、重复身份、坏根、错命名空间、深度 33、乱码等 14 种文档级失败 | `docfailure` 系列 |
| 深度恰好 32 通过 | `deep_ok.xml` |
| 读取块大小 1/13/1K/64K/1M 结果逐字节不变 | `small.xml`、`bad_records.xml` |
| 失败导入后旧版本仍可读、下次成功导入发布新版本 | 版本保持测试 |
| 179 MiB 大文件、峰值 RSS < 96 MiB | `big.xml` |

### 小样例手算核对（small.xml）

有效订单 4 张：`b1/o1` = 2×150+1×0 = **300**；`b1/o2` = 3×7 = **21**（前向引用
`c2`）；`b1a/o1` = 1×150 = **150**；`b2/o9` = 10×25 = **250**。合计 **721** 分，
明细 5 行，客户 4 个。拒绝 6 条：`oX`(seq 7, unknown_customer)、`oP`(seq 8,
unknown_customer，仅父批次有 `c2`)、`oBad`(seq 11, bad_line)、`cBad`(seq 12,
missing_name)、`oUseBad`(seq 13, customer_rejected)、`oEmpty`(seq 14, no_lines)。
扩展子树中的 `FAKE1/FAKE2` 不计入任何统计。

### 大样例内存实测（本机）

`big.xml`：179.0 MiB，1,200,000 订单 / 2,400,000 明细 / 400,000 客户 /
1,100 批次，导入约 23–92 s（热/冷页缓存），**峰值 RSS 30.6 MiB**（限值
96 MiB），总金额 35,971,925,689 分，与生成器预期完全一致。复现：

```bash
python3 importer.py generate --out samples            # 含 big.xml
python3 importer.py import samples/big.xml --data-dir data --chunk-size 1048576
```

## 设计取舍（明确记录）

- 数量/单价采用严格十进制语法（仅数字）；`007` 接受为 7，`" 1"`、`+1`、`1.0`
  均判非法明细。
- 无 id 的记录不占用身份（可重复出现，各自记拒绝）；有 id 的无效记录仍占用身份，
  同 batch 内重复即整份失败。
- 业务命名空间内的未知元素、`batch` 内游离的 `line`、`customer` 的子元素均忽略
  并计数，不影响其他记录。
- 子树字节数用 expat 字节偏移精确计量；自闭合且为父元素末子的记录可能高估数
  字节（仅影响 64 KiB 边界 ±12 字节，属资源护栏而非业务边界）。
- 暂存 SQLite 为一次性草稿（journal/synchronous 关闭），导入结束即删除；
  发布物只有版本目录中的 JSONL 与 manifest。

## 代码结构

```
importer.py            入口（python3 importer.py <命令>）
orderimp/
  streamparse.py       expat 事件式解析、限值、记录切分
  staging.py           SQLite 暂存（身份唯一性在此强制）
  publish.py           引用解析 + 原子版本发布
  api.py               导入流水线
  generate.py          本地合成样例生成
  verify.py            自检套件
  cli.py               generate / import / inspect / verify
```
