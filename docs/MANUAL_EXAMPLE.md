# 小规模手算样例（多币种 + 冲销 + 关账）

全部为合成数据。金额单位为“分”。币种之间不做汇率折算、不允许串抵；
每个币种是一条独立的平衡账。

## 账户

| account_id | currency |
| --- | --- |
| cash-usd | USD |
| revenue-usd | USD |
| receivable-usd | USD |
| cash-eur | EUR |
| revenue-eur | EUR |

## 凭证

### E1（2026-03-05，多币种）

| 账户 | 币种 | 借方 | 贷方 |
| --- | --- | ---: | ---: |
| cash-usd | USD | 120000 | 0 |
| revenue-usd | USD | 0 | 120000 |
| cash-eur | EUR | 90000 | 0 |
| revenue-eur | EUR | 0 | 90000 |

- USD：借 120000 = 贷 120000 ✓
- EUR：借 90000 = 贷 90000 ✓
- 整张凭证总借 210000 = 总贷 210000，但两个币种是分别平衡的。

### E2（2026-03-10，USD）

| 账户 | 币种 | 借方 | 贷方 |
| --- | --- | ---: | ---: |
| receivable-usd | USD | 75000 | 0 |
| revenue-usd | USD | 0 | 75000 |

USD：75000 = 75000 ✓

### R1（2026-04-02，冲销 E2）

E2 逐行借贷互换：

| 账户 | 币种 | 借方 | 贷方 |
| --- | --- | ---: | ---: |
| receivable-usd | USD | 0 | 75000 |
| revenue-usd | USD | 75000 | 0 |

## 2026-03 期间关账快照（手算）

按“账户 × 币种”汇总 2026-03 的 E1、E2：

| 账户 | 币种 | 借方 | 贷方 | 净额(借-贷) |
| --- | --- | ---: | ---: | ---: |
| cash-eur | EUR | 90000 | 0 | +90000 |
| cash-usd | USD | 120000 | 0 | +120000 |
| receivable-usd | USD | 75000 | 0 | +75000 |
| revenue-eur | EUR | 0 | 90000 | -90000 |
| revenue-usd | USD | 0 | 195000 | -195000 |

- EUR 合计：+90000 − 90000 = 0 ✓
- USD 合计：+195000 − 195000 = 0 ✓
- 入口凭证 2 张，分录行 6 行。
2026-03 关账后，E1/E2 所在期间关闭，新的 2026-03 入账被拒绝。R1 落在
开放的 2026-04，因此允许冲销已关账期间的原凭证 E2。

## 2026-04 期间（仅 R1）

| 账户 | 币种 | 借方 | 贷方 | 净额 |
| --- | --- | ---: | ---: | ---: |
| receivable-usd | USD | 0 | 75000 | -75000 |
| revenue-usd | USD | 75000 | 0 | +75000 |

USD 合计为 0 ✓。全账累计（累计余额表）：

| 账户 | 币种 | 累计借方 | 累计贷方 | 累计净额 |
| --- | --- | ---: | ---: | ---: |
| cash-eur | EUR | 90000 | 0 | +90000 |
| cash-usd | USD | 120000 | 0 | +120000 |
| receivable-usd | USD | 75000 | 75000 | 0 |
| revenue-eur | EUR | 0 | 90000 | -90000 |
| revenue-usd | USD | 75000 | 195000 | -120000 |

- 每个币种全账累计净额均为 0（EUR、USD 各自 √）。
- 原凭证 E2 + 冲销 R1 对每个账户的净影响为 0：
  receivable-usd = 75000−75000 = 0，revenue-usd = −75000+75000 = 0 ✓

## 一个应当被拒绝的例子（币种串抵）

| 账户 | 币种 | 借方 | 贷方 |
| --- | --- | ---: | ---: |
| cash-usd | USD | 500 | 0 |
| revenue-usd | USD | 0 | 100 |
| cash-eur | EUR | 100 | 0 |
| revenue-eur | EUR | 0 | 500 |

总借 600 = 总贷 600，但 USD 是 +400、EUR 是 −400，两个币种各自不平衡，
因此整张凭证失败，不会落任何分录。

## 如何用 CLI 复现

样例文件在 `docs/manual_example/`（`accounts.csv`、`e1.json`、`e2.json`、
`r1.json`、`reject-cross.json`）。使用独立数据库复现：

```sh
export LEDGER_PGDATABASE=ledger_manual
createdb -h /tmp/ledger-pg-sock -p 55432 ledger_manual
.venv/bin/python -m ledger reset-db --yes
.venv/bin/python -m ledger load-accounts docs/manual_example/accounts.csv
.venv/bin/python -m ledger post --entry-file docs/manual_example/e1.json
.venv/bin/python -m ledger post --entry-file docs/manual_example/e2.json
.venv/bin/python -m ledger post --entry-file docs/manual_example/reject-cross.json  # 应被拒绝
.venv/bin/python -m ledger close --period 2026-03
.venv/bin/python -m ledger reverse --reversal-file docs/manual_example/r1.json
.venv/bin/python -m ledger trial-balance --period 2026-04
.venv/bin/python -m ledger audit
```

已实测：2026-03 关账快照 `entry_count=2, posting_count=6`，账户汇总与上表
逐行一致；`reject-cross` 返回 `unbalanced`（USD 差 +400、EUR 差 −400）；
R1 落在 2026-04，且原 E2 + R1 每账户净额为 0；`audit` 返回 `"ok": true`。

