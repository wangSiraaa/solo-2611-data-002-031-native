-- 增量连接物化 schema（PostgreSQL 16）
-- 本文件可重复执行（IF NOT EXISTS），由 incjoin.db.migrate 在每次命令启动时应用。

-- 左表 L：row_id 唯一、join_key 非空、qty 为正整数
CREATE TABLE IF NOT EXISTS l_source (
    row_id              TEXT PRIMARY KEY,
    join_key            TEXT NOT NULL,
    qty                 BIGINT NOT NULL CHECK (qty > 0),
    last_changed_batch  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_l_source_join_key ON l_source (join_key);

-- 右表 R：row_id 唯一、join_key 非空、unit_cents 为非负整数
CREATE TABLE IF NOT EXISTS r_source (
    row_id              TEXT PRIMARY KEY,
    join_key            TEXT NOT NULL,
    unit_cents          BIGINT NOT NULL CHECK (unit_cents >= 0),
    last_changed_batch  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_r_source_join_key ON r_source (join_key);

-- 连接明细：join_key 完整相等产生的全部左右配对，主键为 (l_row_id, r_row_id)
-- 金额 amount_cents = qty * unit_cents
-- 外键 ON DELETE CASCADE：删源行时仅移除该行参与的配对，同键其他配对不受影响；
-- 批次维护步骤随后仍会按受影响键统一重算，保证最终集合与源表 JOIN 完全一致。
CREATE TABLE IF NOT EXISTS join_detail (
    l_row_id            TEXT NOT NULL
        REFERENCES l_source (row_id) ON DELETE CASCADE,
    r_row_id            TEXT NOT NULL
        REFERENCES r_source (row_id) ON DELETE CASCADE,
    join_key            TEXT NOT NULL,
    qty                 BIGINT NOT NULL CHECK (qty > 0),
    unit_cents          BIGINT NOT NULL CHECK (unit_cents >= 0),
    amount_cents        BIGINT NOT NULL CHECK (amount_cents >= 0),
    last_changed_batch  TEXT NOT NULL,
    PRIMARY KEY (l_row_id, r_row_id)
);
CREATE INDEX IF NOT EXISTS idx_join_detail_join_key ON join_detail (join_key);

-- 每个 join_key 的汇总：配对数、配对数量合计（按实际配对累加）、金额合计
CREATE TABLE IF NOT EXISTS key_summary (
    join_key            TEXT PRIMARY KEY,
    pair_count          BIGINT NOT NULL CHECK (pair_count >= 0),
    qty_total           BIGINT NOT NULL,
    amount_total_cents  BIGINT NOT NULL,
    last_changed_batch  TEXT NOT NULL
);

-- 批次回执：batch_id 唯一；content_hash 为规范内容哈希，用于幂等重放与内容冲突拒绝
CREATE TABLE IF NOT EXISTS batch_receipt (
    batch_id        TEXT PRIMARY KEY,
    content_hash    TEXT NOT NULL,
    canonical_json  TEXT NOT NULL,
    affected_keys   JSONB NOT NULL DEFAULT '[]'::jsonb,
    op_count        INTEGER NOT NULL,
    status          TEXT NOT NULL DEFAULT 'committed'
                    CHECK (status IN ('committed', 'replayed')),
    applied_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
