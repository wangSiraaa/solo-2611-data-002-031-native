"""合成数据生成：固定 seed；热点客户、日期倾斜、字段相关性。

相关性设计（可复现地构造"真实"分布）：
- 客户: 前 hotspot_customers 个客户贡献 hotspot_share 比例的订单（热点）
- 订单日期: 向近期倾斜（指数 < 1 的幂律）
- 订单状态 与 日期相关: 越近越可能 open/paid，越早多为 shipped/returned
- 商品类目 与 价格带相关
- 客户地区 与 等级相关
- orders.total 由明细累加得到（跨表相关）
"""
from __future__ import annotations

import random
from datetime import date, timedelta

from .config import Config

CATEGORIES = [
    ("electronics", 0.25, (200.0, 2000.0)),
    ("home", 0.20, (20.0, 400.0)),
    ("books", 0.15, (5.0, 60.0)),
    ("sports", 0.12, (15.0, 300.0)),
    ("fashion", 0.10, (10.0, 250.0)),
    ("beauty", 0.08, (8.0, 120.0)),
    ("toys", 0.06, (5.0, 90.0)),
    ("office", 0.04, (3.0, 80.0)),
]
REGIONS = ["north", "south", "east", "west"]
# 地区 -> 等级分布（相关）
TIER_BY_REGION = {
    "north": [("gold", 0.30), ("silver", 0.40), ("bronze", 0.30)],
    "south": [("gold", 0.10), ("silver", 0.30), ("bronze", 0.60)],
    "east": [("gold", 0.20), ("silver", 0.50), ("bronze", 0.30)],
    "west": [("gold", 0.05), ("silver", 0.25), ("bronze", 0.70)],
}
CHANNELS = [("web", 0.60), ("app", 0.30), ("store", 0.10)]


def _weighted(rng: random.Random, items):
    population = [x[0] for x in items]
    weights = [x[1] for x in items]
    return rng.choices(population, weights=weights, k=1)[0]


def param_context(cfg: Config) -> dict:
    """参数符号 -> 具体值。由构造保证：id 1..15 为热点客户等。"""
    s = cfg.scale
    return {
        "hot1": 1,
        "hot2": 2,
        "hot3": 3,
        "cold": s["customers"],          # 最后一个客户，非热点
        "popular1": 1,                   # 第一个商品属于最热类目
        "rare1": s["products"],          # 最后一个商品属于最冷类目
    }


def gen_products(cfg: Config, rng: random.Random) -> list[tuple]:
    rows = []
    n = cfg.scale["products"]
    cats = [(c, w) for c, w, _ in CATEGORIES]
    band = {c: b for c, _, b in CATEGORIES}
    for pid in range(1, n + 1):
        cat = _weighted(rng, cats)
        lo, hi = band[cat]
        price = round(rng.uniform(lo, hi), 2)
        rows.append((pid, cat, price, f"product-{pid}"))
    return rows


def gen_customers(cfg: Config, rng: random.Random) -> list[tuple]:
    rows = []
    n = cfg.scale["customers"]
    d0 = date(2015, 1, 1)
    for cid in range(1, n + 1):
        region = rng.choice(REGIONS)
        tier = _weighted(rng, TIER_BY_REGION[region])
        created = d0 + timedelta(days=rng.randrange(0, 3000))
        rows.append((cid, region, tier, created.isoformat()))
    return rows


def gen_orders_items(cfg: Config, rng: random.Random, products: list[tuple]):
    """生成订单与明细；total 由明细累加（跨表相关）。"""
    s = cfg.scale
    n_orders = s["orders"]
    n_cust = s["customers"]
    n_hot = s["hotspot_customers"]
    hot_share = s["hotspot_share"]

    d0 = date.fromisoformat(s["date_start"])
    d1 = date.fromisoformat(s["date_end"])
    span = (d1 - d0).days
    today = date(2026, 11, 13)

    # 客户权重：热点客户均分 hotspot_share，其余均分剩余
    hot_w = hot_share / n_hot
    cold_w = (1 - hot_share) / (n_cust - n_hot)
    cust_pop = list(range(1, n_cust + 1))
    cust_w = [hot_w if c <= n_hot else cold_w for c in cust_pop]

    prod_ids = [p[0] for p in products]
    prod_cat = {p[0]: p[1] for p in products}
    prod_price = {p[0]: float(p[2]) for p in products}
    cat_w = {c: w for c, w, _ in CATEGORIES}
    prod_w = [cat_w[prod_cat[pid]] for pid in prod_ids]

    # 一次性抽出全部订单的客户与日期（向近期倾斜）
    cust_draws = rng.choices(cust_pop, weights=cust_w, k=n_orders)
    date_draws = [d0 + timedelta(days=int(span * (rng.random() ** 0.55))) for _ in range(n_orders)]

    orders = []
    items = []
    for oid in range(1, n_orders + 1):
        cid = cust_draws[oid - 1]
        odate = date_draws[oid - 1]
        age = (today - odate).days
        r = rng.random()
        if age < 30:
            status = "open" if r < 0.50 else ("paid" if r < 0.80 else "shipped")
        elif age < 90:
            status = "open" if r < 0.10 else ("paid" if r < 0.40 else ("shipped" if r < 0.90 else "returned"))
        else:
            status = "shipped" if r < 0.80 else "returned"
        channel = _weighted(rng, CHANNELS)
        n_lines = 1 + min(4, int(rng.expovariate(1 / 1.6)))
        total = 0.0
        # 加权不放回抽样：同一订单内商品不重复（PK 为 (order_id, product_id)）
        chosen: set[int] = set()
        while len(chosen) < n_lines:
            pid = rng.choices(prod_ids, weights=prod_w, k=1)[0]
            if pid in chosen:
                continue
            chosen.add(pid)
            qty = rng.randrange(1, 6)
            price = prod_price[pid]
            total += qty * price
            items.append((oid, pid, qty, price))
        orders.append((oid, cid, odate.isoformat(), status, channel, round(total, 2)))
    return orders, items


DDL = """
CREATE TABLE customers (
    customer_id integer PRIMARY KEY,
    region text NOT NULL,
    tier text NOT NULL,
    created_at date NOT NULL
);
CREATE TABLE products (
    product_id integer PRIMARY KEY,
    category text NOT NULL,
    price numeric(10,2) NOT NULL,
    name text NOT NULL
);
CREATE TABLE orders (
    order_id integer PRIMARY KEY,
    customer_id integer NOT NULL REFERENCES customers(customer_id),
    order_date date NOT NULL,
    status text NOT NULL,
    channel text NOT NULL,
    total numeric(12,2) NOT NULL
);
CREATE TABLE order_items (
    order_id integer NOT NULL REFERENCES orders(order_id),
    product_id integer NOT NULL REFERENCES products(product_id),
    qty integer NOT NULL,
    unit_price numeric(10,2) NOT NULL,
    PRIMARY KEY (order_id, product_id)
);
-- 预置"业务索引"（非本实验创建，清理时不得触碰）
CREATE INDEX orders_channel_idx ON orders (channel);
"""


def create_schema(conn, schema: str):
    conn.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
    conn.execute(f"CREATE SCHEMA {schema}")
    conn.execute(f"SET search_path TO {schema}")
    for stmt in DDL.split(";"):
        stmt = stmt.strip()
        if stmt:
            # 去掉注释行
            lines = [ln for ln in stmt.splitlines() if not ln.strip().startswith("--")]
            body = "\n".join(lines).strip()
            if body:
                conn.execute(body)


def copy_rows(conn, table: str, cols: list[str], rows: list[tuple]):
    with conn.cursor() as cur:
        with cur.copy(f"COPY {table} ({', '.join(cols)}) FROM STDIN") as copy:
            for r in rows:
                copy.write_row(r)


def load_initial(cfg: Config, conn, schema: str) -> dict:
    """生成数据并 COPY 进指定 schema，返回行数统计。"""
    rng = random.Random(cfg.seed)
    products = gen_products(cfg, rng)
    customers = gen_customers(cfg, rng)
    orders, items = gen_orders_items(cfg, rng, products)
    conn.execute(f"SET search_path TO {schema}")
    copy_rows(conn, "customers", ["customer_id", "region", "tier", "created_at"], customers)
    copy_rows(conn, "products", ["product_id", "category", "price", "name"], products)
    copy_rows(
        conn,
        "orders",
        ["order_id", "customer_id", "order_date", "status", "channel", "total"],
        orders,
    )
    copy_rows(conn, "order_items", ["order_id", "product_id", "qty", "unit_price"], items)
    return {
        "customers": len(customers),
        "products": len(products),
        "orders": len(orders),
        "order_items": len(items),
    }
