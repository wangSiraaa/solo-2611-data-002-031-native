"""数据库结构。

源表 records 保留全部原业务字段（id / raw_code / amount_cents / active），
仅额外增加迁移辅助版本字段 row_version（原业务字段语义不变，主键禁止修改）。

变更捕获通过触发器实现：源写入与变更记录在*同一个事务*里提交/回滚，
因此回滚的写入绝不可能被迁移看到。
"""
from __future__ import annotations

SCHEMA_SQL = r"""
CREATE SEQUENCE IF NOT EXISTS record_changes_seq;

-- 源表（原始表，仍在持续写入）
CREATE TABLE IF NOT EXISTS records (
    id           BIGINT PRIMARY KEY,
    raw_code     TEXT        NOT NULL,
    amount_cents BIGINT      NOT NULL,
    active       BOOLEAN     NOT NULL,
    -- 迁移辅助版本字段：本行当前版本，单调递增，触发器维护，业务侧无需关心
    row_version  BIGINT      NOT NULL DEFAULT 1
);

-- 可靠变更日志：触发器写入，与源事务同生共死
CREATE TABLE IF NOT EXISTS record_changes (
    change_seq BIGINT PRIMARY KEY DEFAULT nextval('record_changes_seq'),
    id         BIGINT  NOT NULL,
    op         TEXT    NOT NULL CHECK (op IN ('I', 'U', 'D')),
    row_version BIGINT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_record_changes_seq ON record_changes (change_seq);
CREATE INDEX IF NOT EXISTS idx_record_changes_id  ON record_changes (id, change_seq);

-- 规范表（迁移目标），多带两个迁移用列，不影响“相同 id / code / 金额 / active”的业务语义
CREATE TABLE IF NOT EXISTS canonical_records (
    id           BIGINT PRIMARY KEY,
    code         TEXT        NOT NULL,
    amount       NUMERIC(18, 2) NOT NULL,
    active       BOOLEAN     NOT NULL,
    row_version  BIGINT      NOT NULL
);

-- 迁移元数据：单行，持久化进度与状态
CREATE TABLE IF NOT EXISTS migration_meta (
    id                  INTEGER PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    state               TEXT NOT NULL DEFAULT 'INIT'
        CHECK (state IN ('INIT', 'RUNNING', 'FAILED', 'READY')),
    backfill_after_id   BIGINT NOT NULL DEFAULT 0,  -- 已完成回填的最大主键（0 = 尚未开始）
    backfill_done       BOOLEAN NOT NULL DEFAULT FALSE,
    watermark           BIGINT NOT NULL DEFAULT 0,  -- 已确认连续处理到的 change_seq
    applied_max         BIGINT NOT NULL DEFAULT 0,  -- 已应用过的最大 change_seq（含洞位预应用）
    chunk_size          INTEGER NOT NULL,
    started_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    fail_stage          TEXT,
    fail_message        TEXT
);

-- 每个回填块的持久化进度（题目要求每块都有持久化进度）
CREATE TABLE IF NOT EXISTS migration_chunks (
    chunk_no       INTEGER PRIMARY KEY,
    id_range       TEXT NOT NULL,
    state          TEXT NOT NULL CHECK (state IN ('DONE')),
    rows_scanned   INTEGER NOT NULL,
    rows_applied   INTEGER NOT NULL,
    errors         INTEGER NOT NULL,
    finished_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 失败位置（格式错误等），逐行逐版本记录，修复后重放可自动清除
CREATE TABLE IF NOT EXISTS migration_errors (
    id           BIGINT NOT NULL,
    row_version  BIGINT NOT NULL,
    stage        TEXT NOT NULL,
    raw_code     TEXT,
    message      TEXT NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (id, row_version)
);
"""

TRIGGER_SQL = r"""
CREATE OR REPLACE FUNCTION records_audit_fn() RETURNS trigger AS $fn$
BEGIN
    IF TG_OP = 'INSERT' THEN
        NEW.row_version := 1;
        INSERT INTO record_changes (id, op, row_version)
        VALUES (NEW.id, 'I', NEW.row_version);
        RETURN NEW;
    ELSIF TG_OP = 'UPDATE' THEN
        IF NEW.id <> OLD.id THEN
            RAISE EXCEPTION 'records 主键 id 不允许修改';
        END IF;
        NEW.row_version := OLD.row_version + 1;
        INSERT INTO record_changes (id, op, row_version)
        VALUES (NEW.id, 'U', NEW.row_version);
        RETURN NEW;
    ELSIF TG_OP = 'DELETE' THEN
        INSERT INTO record_changes (id, op, row_version)
        VALUES (OLD.id, 'D', OLD.row_version);
        RETURN OLD;
    END IF;
END;
$fn$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS records_audit ON records;
CREATE TRIGGER records_audit
BEFORE INSERT OR UPDATE OR DELETE ON records
FOR EACH ROW EXECUTE FUNCTION records_audit_fn();
"""


def install(conn) -> None:
    with conn.transaction():
        conn.execute(SCHEMA_SQL)
        conn.execute(TRIGGER_SQL)
