import argparse
import json
import os
import sqlite3
import sys
from decimal import Decimal, InvalidOperation
from pathlib import Path

ap=argparse.ArgumentParser()
ap.add_argument("--db",default="legacy.sqlite")
ap.add_argument("--faults",default="faults-run.json")
args=ap.parse_args()
if not sys.stdin.isatty() or not sys.stdout.isatty():
    raise SystemExit("This fixture requires a real PTY")
db=sqlite3.connect(args.db)
db.execute("CREATE TABLE IF NOT EXISTS items(code TEXT PRIMARY KEY,name TEXT,unit TEXT,price TEXT,revision INTEGER NOT NULL)")
db.execute("CREATE TABLE IF NOT EXISTS history(seq INTEGER PRIMARY KEY AUTOINCREMENT,code TEXT,name TEXT,unit TEXT,price TEXT,revision INTEGER)")
db.executemany("INSERT OR IGNORE INTO items VALUES (?,?,?,?,1)",[("A001","铅笔","支","1.20"),("A002","茶包","盒","18.00"),("A003","米","袋","32.00")])
db.commit()

def fault(name):
    path=Path(args.faults)
    if not path.exists(): return False
    config=json.loads(path.read_text())
    if not config.get(name): return False
    config[name]=False
    path.write_text(json.dumps(config))
    return True

def line(row,text):
    print("\033[%d;1H\033[2K%s"%(row,text),end="",flush=True)

def screen(title,record=None):
    print("\033[2J\033[H",end="",flush=True)
    line(1,"资料终端 | "+title)
    if record:
        for row,text in enumerate(["编码: "+record[0],"名称: "+record[1],"单位: "+record[2],"价格: "+record[3],"修订号: "+str(record[4])],start=4): line(row,text)

def ask(text):
    line(22,text)
    print("\033[23;1H\033[2K",end="",flush=True)
    answer=sys.stdin.readline()
    if not answer: raise SystemExit(0)
    return answer.rstrip("\r\n")

def get(code):
    return db.execute("SELECT code,name,unit,price,revision FROM items WHERE code=?",(code,)).fetchone()

def edit(record):
    code,name,unit,price,rev=record
    screen("编辑",record)
    name=ask("请输入名称（空行保留）") or name
    line(5,"名称: "+name+" [尚未保存]")
    chosen=ask("选择单位: 1=支 2=盒 3=袋；空行保留")
    if chosen:
        if chosen not in {"1","2","3"}: line(19,"字段拒绝: 单位无效"); ask("回车返回查询"); return
        unit={"1":"支","2":"盒","3":"袋"}[chosen]
    line(6,"单位: "+unit+" [尚未保存]")
    price=ask("请输入价格（空行保留）") or price
    try:
        amount=Decimal(price)
        if not amount.is_finite() or amount<0 or amount.as_tuple().exponent < -2: raise ValueError()
        price=format(amount,".2f")
    except (InvalidOperation,ValueError): line(19,"字段拒绝: 价格无效"); ask("回车返回查询"); return
    line(7,"价格: "+price+" [尚未保存]")
    if fault("unknown_prompt_once"):
        line(18,"异常弹窗: E97 操作条件需要人工确认")
        ask("只允许 X 退出本次操作")
        return
    action=ask("S=保存 B=放弃")
    if action.lower()!="s": return
    if fault("reject_save_once"):
        line(19,"保存被业务规则拒绝"); ask("回车返回查询"); return
    if fault("conflict_before_save_once"):
        db.execute("UPDATE items SET name=name||'-外部修改',revision=revision+1 WHERE code=?",(code,)); db.commit()
    cur=db.execute("UPDATE items SET name=?,unit=?,price=?,revision=revision+1 WHERE code=? AND revision=?",(name,unit,price,code,rev))
    if cur.rowcount!=1:
        db.rollback(); line(19,"保存冲突: 修订号已变化，请重新查询"); ask("回车返回查询"); return
    db.execute("INSERT INTO history(code,name,unit,price,revision) VALUES (?,?,?,?,?)",(code,name,unit,price,rev+1))
    db.commit()
    if fault("drop_after_save_once"): os._exit(0)
    line(19,"保存成功；请重新查询确认")
    ask("回车返回查询")

while True:
    screen("主菜单")
    command=ask("Q=查询 X=退出").lower()
    if command=="x": break
    if command!="q": continue
    code=ask("输入资料编码")
    if fault("query_unavailable_once"):
        screen("查询暂不可用"); ask("回车返回主菜单"); continue
    record=get(code)
    if record is None:
        screen("没有此编码"); ask("回车返回主菜单"); continue
    screen("查询结果",record)
    if ask("E=编辑 B=返回").lower()=="e": edit(record)
db.close()
