"""配置加载与实验常量。"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "workload.yaml"
SQL_DIR = ROOT / "sql"


@dataclass
class QuerySpec:
    qid: str
    file: str
    kind: str
    desc: str
    param_groups: list[dict]  # [{name, weight, params:[...]}]（params 已解析符号）
    sql: str = ""


@dataclass
class Config:
    tag: str
    dbname: str
    seed: int
    scale: dict
    stats: dict
    measurement: dict
    writebench: dict
    constraints: dict
    explore: dict
    queries: list[QuerySpec] = field(default_factory=list)

    # 派生名称（全部带任务标识前缀）
    @property
    def schema_main(self) -> str:
        return f"{self.tag}_main"

    @property
    def schema_tpl(self) -> str:
        return f"{self.tag}_tpl"

    @property
    def schema_write_prefix(self) -> str:
        return f"{self.tag}_w_"

    @property
    def index_prefix(self) -> str:
        return f"{self.tag}_ix_"

    @property
    def archive(self) -> Path:
        p = ROOT / "archive" / self.tag
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def state_path(self) -> Path:
        return self.archive / "state.json"


def resolve_param(tok, ctx: dict):
    """参数符号解析：hot1/cold/popular1 等由数据上下文给出，其余按字面值。"""
    if isinstance(tok, str) and tok in ctx:
        return ctx[tok]
    return tok


def load_config(param_ctx: dict | None = None) -> Config:
    raw = yaml.safe_load(CONFIG_PATH.read_text())
    e = raw["experiment"]
    ctx = param_ctx or {}
    queries = []
    for q in raw["queries"]:
        groups = []
        for g in q["param_groups"]:
            groups.append(
                {
                    "name": g["name"],
                    "weight": float(g["weight"]),
                    "params": [resolve_param(p, ctx) for p in g["params"]],
                }
            )
        sql_path = SQL_DIR / q["file"]
        queries.append(
            QuerySpec(
                qid=q["id"],
                file=q["file"],
                kind=q["kind"],
                desc=q["desc"],
                param_groups=groups,
                sql=sql_path.read_text(),
            )
        )
    return Config(
        tag=e["tag"],
        dbname=e["dbname"],
        seed=int(e["seed"]),
        scale=e["scale"],
        stats=e["stats"],
        measurement=e["measurement"],
        writebench=e["writebench"],
        constraints=e["constraints"],
        explore=e["explore"],
        queries=queries,
    )


def dsn(cfg: Config) -> str:
    return (
        f"host={os.environ.get('PGHOST', '127.0.0.1')} "
        f"port={os.environ.get('PGPORT', '5433')} "
        f"user={os.environ.get('PGUSER', 'postgres')} dbname={cfg.dbname}"
    )
