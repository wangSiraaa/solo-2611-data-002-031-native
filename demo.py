#!/usr/bin/env python3
"""
demo.py — 四个演示场景（每次运行重建 .runtime/erasure-demo/ 下的独立输入）：

  s1  主流程：无保留删除（U01）+ 保留子记录需父键骨架（U02/O21），
      普通读取与公共导出均不再流出已删除/受限对象
  s2  计划后新增保留令：apply 拒绝过期计划；重新 plan 后按新保留令执行
  s3  文件替换阶段中断（临时文件已写、替换未完成）后的续办
  s4  数据库提交后、写执行记录前崩溃的续办（凭库内事务标记恢复）
"""
import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
TOOL = ROOT / "erasure_tool.py"
PREPARE = ROOT / "fixtures" / "prepare.py"
RUN = ROOT / ".runtime" / "erasure-demo"


def sh(cmd, expect=0):
    print(f"$ {' '.join(str(c) for c in cmd)}")
    p = subprocess.run([str(c) for c in cmd], capture_output=True, text=True)
    out = (p.stdout + p.stderr).rstrip()
    if out:
        print(out)
    if expect is not None and p.returncode != expect:
        raise SystemExit(f"!! 期望退出码 {expect}，实际 {p.returncode}")
    print()
    return p


def tool(workdir, *argv, expect=0):
    return sh([sys.executable, TOOL, *argv, "--workdir", workdir], expect=expect)


def fresh(name) -> Path:
    w = RUN / name
    if w.exists():
        shutil.rmtree(w)
    sh([sys.executable, PREPARE, w])
    return w


def banner(title):
    print("=" * 72)
    print(title)
    print("=" * 72)


def scenario_s1():
    banner("场景 s1：无保留删除 + 保留子记录需父键骨架（主流程）")
    w = fresh("s1-main")
    tool(w, "plan")
    tool(w, "apply")
    tool(w, "verify")
    tool(w, "status")

    print("---- 普通读取入口：已删除/受限对象不流出 ----")
    r = tool(w, "read")                      # 普通全量视图
    assert "U01" not in r.stdout and "U02" not in r.stdout
    assert "争议处理中" not in r.stdout       # 受限订单原文不可见
    assert "U03" in r.stdout                  # 未请求对象不受影响
    r = tool(w, "read", "--subject", "U02")
    assert "无普通业务记录" in r.stdout
    r = tool(w, "read", "--export")          # 公共导出副本
    assert '"U01"' not in r.stdout and '"U02"' not in r.stdout
    assert "争议处理中" not in r.stdout       # 保留令不授权留在公共文件
    assert r.stdout.count('"U03"') == 2      # U03 客户行 + O31 订单行
    print("s1 断言通过：U01 全删；U02 骨架+O21 受限；导出只剩 U03。\n")


def scenario_s2():
    banner("场景 s2：计划后新增保留令 -> 拒绝过期计划 -> 重新计划")
    w = fresh("s2-stale-hold")
    tool(w, "plan")

    holds_path = w / "holds.json"
    holds = json.loads(holds_path.read_text(encoding="utf-8"))
    holds["holds"].append({"order_id": "O11", "active": True,
                           "reason": "late-dispute",
                           "retain": "order_original_in_restricted_store"})
    holds_path.write_text(json.dumps(holds, ensure_ascii=False, indent=2),
                      encoding="utf-8")
    print(">> 已在计划后向 holds.json 新增 O11 的保留令\n")

    r = tool(w, "apply", expect=2)           # 必须拒绝过期计划
    assert "拒绝执行旧计划" in r.stdout

    tool(w, "plan")                          # 重新计划，捕获新保留令
    tool(w, "apply")
    tool(w, "verify")
    r = tool(w, "status")
    assert "受限订单 O11" in r.stdout and "骨架客户 U01" in r.stdout
    print("s2 断言通过：过期计划被拒；新计划下 O11 受限、U01 骨架化。\n")


def scenario_s3():
    banner("场景 s3：文件替换阶段中断 -> 续办")
    w = fresh("s3-crash-export")
    tool(w, "plan")
    tool(w, "apply", "--crash-after", "export_tmp", expect=1)  # 替换中途崩溃
    r = tool(w, "status")
    assert "PENDING" in r.stdout and "in_progress" in r.stdout  # 不得报全部成功
    assert (w / "export.jsonl.erasure-tmp").exists()            # 中断遗留临时文件

    tool(w, "apply")                         # 续办：只补导出替换
    tool(w, "verify")
    r = tool(w, "status")
    assert "completed" in r.stdout
    print("s3 断言通过：中断期间状态为 in_progress，续办后 completed。\n")


def scenario_s4():
    banner("场景 s4：数据库提交后、写执行记录前崩溃 -> 凭库内标记续办")
    w = fresh("s4-crash-db")
    tool(w, "plan")
    tool(w, "apply", "--crash-after", "db_committed", expect=1)
    r = tool(w, "status")
    assert "事务标记" in r.stdout            # 真实状态：库已提交，记录滞后

    tool(w, "apply")                         # 恢复执行记录并继续导出步骤
    tool(w, "verify")
    r = tool(w, "status")
    assert "completed" in r.stdout
    print("s4 断言通过：库内事务标记驱动恢复，最终 completed。\n")


def main():
    RUN.mkdir(parents=True, exist_ok=True)
    scenario_s1()
    scenario_s2()
    scenario_s3()
    scenario_s4()
    print("ALL SCENARIOS OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
