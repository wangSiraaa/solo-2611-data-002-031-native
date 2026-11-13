#!/bin/sh
# pitrdrill 自动生成: restore_command WAL 拉取包装
echo "[pitrdrill-restore-fetch] request=$1 dest=$2" >> "/workspace/pitrdrill-work/restores/F-gap-running/restore.log"
src="/workspace/pitrdrill-work/archive-sets/set-gap/$1"
if [ -f "$src" ]; then
  cp -- "$src" "$2" && echo "[pitrdrill-restore-fetch] HIT $1" >> "/workspace/pitrdrill-work/restores/F-gap-running/restore.log" && exit 0
fi
echo "[pitrdrill-restore-fetch] MISS $1" >> "/workspace/pitrdrill-work/restores/F-gap-running/restore.log"
exit 1
