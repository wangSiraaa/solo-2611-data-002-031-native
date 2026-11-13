#!/bin/sh
# pitrdrill 自动生成: archive_command WAL 推送包装
exec "/usr/bin/python3" "/workspace/pitrdrill/pitrdrill.py" --root "/workspace/pitrdrill-work" archive-push "$1" "$2"
