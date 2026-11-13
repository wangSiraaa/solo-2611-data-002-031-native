-- 库存预占工具初始化迁移
-- 适用: PostgreSQL 16
-- 设计要点:
--   1. stock.on_hand / reserved 均为非负 BIGINT, 可用量 = on_hand - reserved
--   2. 每个订单只有一行 order_records, 状态机由 CHECK + 应用层守卫共同保证
--   3. 逐商品流水 stock_ledger 记录每一次库存/预占变化, 可据此重算库存
--   4. request_records 以 request_id 为主键, 保存请求内容指纹与终态结果, 实现幂等
--   5. 所有跨表变化在单事务内提交, 并由触发器保证库存/预占不变量

BEGIN;

-- 可重复执行的初始化迁移: 先清理旧版本对象再重建.
-- (业务数据清理由 CLI 的 reset_all / TRUNCATE 负责, 与结构迁移分开.)
DROP TABLE IF EXISTS stock_ledger CASCADE;
DROP TABLE IF EXISTS order_items CASCADE;
DROP TABLE IF EXISTS order_records CASCADE;
DROP TABLE IF EXISTS request_records CASCADE;
DROP TABLE IF EXISTS stock CASCADE;
DROP FUNCTION IF EXISTS stock_touch() CASCADE;
DROP FUNCTION IF EXISTS inventory_reconciliation() CASCADE;

-- ---------------------------------------------------------------------------
-- SKU 库存表
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS stock (
    sku        TEXT PRIMARY KEY,
    on_hand    BIGINT NOT NULL CHECK (on_hand >= 0),   -- 实际库存
    reserved   BIGINT NOT NULL DEFAULT 0 CHECK (reserved >= 0 AND reserved <= on_hand),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------------
-- 订单表 (一个订单一行, 状态机)
-- 状态: RESERVED -> SHIPPED | CANCELLED, 终态不可互转
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS order_records (
    order_id   TEXT PRIMARY KEY,
    status     TEXT NOT NULL CHECK (status IN ('RESERVED', 'SHIPPED', 'CANCELLED')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 订单逐商品明细 (RESERVED/SHIPPED 时为已预占数量; CANCELLED 后行保留作为审计)
CREATE TABLE IF NOT EXISTS order_items (
    order_id TEXT NOT NULL REFERENCES order_records(order_id),
    sku      TEXT NOT NULL,
    qty      BIGINT NOT NULL CHECK (qty > 0),
    PRIMARY KEY (order_id, sku)
);

-- ---------------------------------------------------------------------------
-- 请求台账: 幂等控制
--   request_id 全局唯一; content_hash 为规范化请求体的 SHA-256
--   相同 request_id 不同内容 -> 应用层检测后拒绝并持久化 REJECTED 结果
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS request_records (
    request_id   TEXT PRIMARY KEY,
    order_id     TEXT NOT NULL,
    action       TEXT NOT NULL CHECK (action IN ('reserve', 'ship', 'cancel')),
    content_hash TEXT NOT NULL,
    status       TEXT NOT NULL CHECK (status IN ('PENDING', 'SUCCESS', 'FAILED', 'REJECTED')),
    result_code  TEXT NOT NULL,          -- 机器可读结果码
    message      TEXT NOT NULL,          -- 人类可读说明
    request_body JSONB NOT NULL,         -- 原始请求体 (审计)
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 同一订单只允许一次成功 reserve; 重复预占在应用层依据此索引拒绝
CREATE UNIQUE INDEX IF NOT EXISTS ux_order_single_reserve
    ON request_records(order_id)
    WHERE action = 'reserve' AND status = 'SUCCESS';

-- ---------------------------------------------------------------------------
-- 逐商品流水: 库存变化的唯一事实来源, 可据此重算 on_hand / reserved
-- INIT    : 初始化导入 (delta_on_hand = +初始库存, delta_reserved = 0)
-- RESERVE : 预占 (delta_on_hand = 0, delta_reserved = +qty)
-- SHIP    : 发货 (delta_on_hand = -qty, delta_reserved = -qty)
-- CANCEL  : 取消 (delta_on_hand = 0, delta_reserved = -qty)
-- 初始化流水的 request_id/order_id 使用保留值 __init__ / __init__
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS stock_ledger (
    ledger_id  BIGSERIAL PRIMARY KEY,
    request_id TEXT NOT NULL,
    order_id   TEXT NOT NULL,
    sku        TEXT NOT NULL,
    qty        BIGINT NOT NULL CHECK (qty > 0),
    delta_on_hand  BIGINT NOT NULL,
    delta_reserved BIGINT NOT NULL,
    reason     TEXT NOT NULL CHECK (reason IN ('INIT', 'RESERVE', 'SHIP', 'CANCEL')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT ck_ledger_init     CHECK (reason <> 'INIT'    OR (delta_on_hand = qty AND delta_reserved = 0)),
    CONSTRAINT ck_ledger_reserve  CHECK (reason <> 'RESERVE'  OR (delta_on_hand = 0 AND delta_reserved = qty)),
    CONSTRAINT ck_ledger_ship     CHECK (reason <> 'SHIP'     OR (delta_on_hand = -qty AND delta_reserved = -qty)),
    CONSTRAINT ck_ledger_cancel   CHECK (reason <> 'CANCEL'   OR (delta_on_hand = 0 AND delta_reserved = -qty))
);

CREATE INDEX IF NOT EXISTS ix_ledger_sku ON stock_ledger(sku);
CREATE INDEX IF NOT EXISTS ix_ledger_order ON stock_ledger(order_id);

-- ---------------------------------------------------------------------------
-- 触发器: stock 行更新时维护 updated_at, 并强制库存不变量
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION stock_touch() RETURNS trigger AS $$
BEGIN
    NEW.updated_at := now();
    IF NEW.on_hand < 0 THEN
        RAISE EXCEPTION 'invariant violated: on_hand negative for sku=%', NEW.sku;
    END IF;
    IF NEW.reserved < 0 THEN
        RAISE EXCEPTION 'invariant violated: reserved negative for sku=%', NEW.sku;
    END IF;
    IF NEW.reserved > NEW.on_hand THEN
        RAISE EXCEPTION 'invariant violated: reserved(%) > on_hand(%) for sku=%',
            NEW.reserved, NEW.on_hand, NEW.sku;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_stock_touch ON stock;
CREATE TRIGGER trg_stock_touch BEFORE UPDATE ON stock
    FOR EACH ROW EXECUTE FUNCTION stock_touch();

-- ---------------------------------------------------------------------------
-- 对账函数: 完全由流水重算每个 SKU 的库存与预占
--   ledger_on_hand  = SUM(delta_on_hand)  (INIT 基线 + 后续发货扣减)
--   ledger_reserved = SUM(delta_reserved)  (初始为 0, reserve/ship/cancel 累计)
-- 同时核对"预占 == 所有 RESERVED 订单数量之和".
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION inventory_reconciliation()
RETURNS TABLE (
    sku                TEXT,
    table_on_hand      BIGINT,
    table_reserved     BIGINT,
    ledger_on_hand     BIGINT,
    ledger_reserved    BIGINT,
    reserved_orders    BIGINT,
    on_hand_match      BOOLEAN,
    reserved_match     BOOLEAN,
    orders_match       BOOLEAN
) AS $$
BEGIN
    RETURN QUERY
    WITH led AS (
        SELECT l.sku,
               COALESCE(SUM(l.delta_on_hand), 0)::bigint  AS l_on,
               COALESCE(SUM(l.delta_reserved), 0)::bigint AS l_res
        FROM stock_ledger l
        GROUP BY l.sku
    ),
    ord AS (
        SELECT oi.sku, COALESCE(SUM(oi.qty), 0)::bigint AS o_res
        FROM order_items oi
        JOIN order_records o ON o.order_id = oi.order_id
        WHERE o.status = 'RESERVED'
        GROUP BY oi.sku
    )
    SELECT s.sku,
           s.on_hand,
           s.reserved,
           COALESCE(led.l_on, 0)  AS ledger_on_hand,
           COALESCE(led.l_res, 0) AS ledger_reserved,
           COALESCE(ord.o_res, 0) AS reserved_orders,
           (s.on_hand  = COALESCE(led.l_on, 0))  AS on_hand_match,
           (s.reserved = COALESCE(led.l_res, 0)) AS reserved_match,
           (s.reserved = COALESCE(ord.o_res, 0)) AS orders_match
    FROM stock s
    LEFT JOIN led ON led.sku = s.sku
    LEFT JOIN ord ON ord.sku = s.sku
    ORDER BY s.sku;
END;
$$ LANGUAGE plpgsql;

COMMIT;
