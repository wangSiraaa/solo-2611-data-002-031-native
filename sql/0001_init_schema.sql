-- 0001_init_schema.sql
-- 工序依赖维护工具：节点表、直接边表、可达闭包表、操作回执表、迁移记录表。
-- node_id 为正数 BIGINT；闭包表不含自身对 (ancestor <> descendant)。

CREATE TABLE IF NOT EXISTS schema_migrations (
    version     TEXT PRIMARY KEY,
    applied_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS nodes (
    node_id  BIGINT PRIMARY KEY CHECK (node_id > 0)
);

CREATE TABLE IF NOT EXISTS edges (
    prerequisite            BIGINT NOT NULL REFERENCES nodes (node_id),
    dependent               BIGINT NOT NULL REFERENCES nodes (node_id),
    last_changed_operation  TEXT NOT NULL,
    PRIMARY KEY (prerequisite, dependent),
    CHECK (prerequisite <> dependent)
);

-- 物化可达关系：每对 (祖先, 后代) 至多一行，且不含自身。
CREATE TABLE IF NOT EXISTS closure (
    ancestor                BIGINT NOT NULL REFERENCES nodes (node_id),
    descendant              BIGINT NOT NULL REFERENCES nodes (node_id),
    last_changed_operation  TEXT NOT NULL,
    PRIMARY KEY (ancestor, descendant),
    CHECK (ancestor <> descendant)
);

CREATE INDEX IF NOT EXISTS idx_closure_descendant ON closure (descendant, ancestor);
CREATE INDEX IF NOT EXISTS idx_edges_dependent    ON edges (dependent, prerequisite);

-- 只记录成功提交的批次；拒绝不写表。重试凭 operation_id + request_hash 幂等。
CREATE TABLE IF NOT EXISTS operations (
    operation_id   TEXT PRIMARY KEY,
    request_hash   TEXT NOT NULL,
    status         TEXT NOT NULL DEFAULT 'committed' CHECK (status = 'committed'),
    added_pairs    JSONB NOT NULL,
    removed_pairs  JSONB NOT NULL,
    applied_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
