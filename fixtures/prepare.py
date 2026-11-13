import shutil
import sqlite3
import sys
from pathlib import Path

target=Path(sys.argv[1] if len(sys.argv)>1 else "work-input")
target.mkdir(parents=True,exist_ok=False)
root=Path(__file__).parent
with sqlite3.connect(target/"business.sqlite") as db:
    db.executescript((root/"seed.sql").read_text())
for name in ("export.jsonl","manifest.json","holds.json","request.json"):
    shutil.copy2(root/name,target/name)
print(target.resolve())
