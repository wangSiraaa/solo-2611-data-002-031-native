#!/bin/sh
# pitrdrill 自动生成: 门控 restore_command(归档迟到演练)
echo "[pitrdrill-gated] request=$1 dest=$2" >> "/workspace/pitrdrill-work/restores/F-wait-released/restore.log"
if [ ! -f "/workspace/pitrdrill-work/tmp/set-waiting.gate" ]; then
  echo "[pitrdrill-gated] GATED $1 (归档尚未到位)" >> "/workspace/pitrdrill-work/restores/F-wait-released/restore.log"
  exit 1
fi
src="/workspace/pitrdrill-work/archive-sets/set-waiting/$1"
if [ -f "$src" ]; then
  cp -- "$src" "$2" && echo "[pitrdrill-gated] HIT $1" >> "/workspace/pitrdrill-work/restores/F-wait-released/restore.log" && exit 0
fi
echo "[pitrdrill-gated] MISS $1" >> "/workspace/pitrdrill-work/restores/F-wait-released/restore.log"
exit 1
