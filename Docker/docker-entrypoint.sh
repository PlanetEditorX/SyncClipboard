#!/bin/sh
set -e

# 启动主服务于后台
python server/linux_run.py &
APP_PID=$!

# 确保至少有一个文件可供 tail（避免 glob 无匹配导致错误）
sh -c 'ls /app/log/* >/dev/null 2>&1 || touch /app/log/.placeholder'

# 将 log 目录下的所有文件输出到 stdout（跟随追加）
tail -F /app/log/* &
TAIL_PID=$!

# 正确转发 SIGTERM/SIGINT，优雅退出
trap 'echo "Stopping..."; kill -TERM "$APP_PID" 2>/dev/null; kill -TERM "$TAIL_PID" 2>/dev/null; wait' TERM INT

# 等待主进程退出，然后清理 tail
wait "$APP_PID"
STATUS=$?
kill "$TAIL_PID" 2>/dev/null || true
wait "$TAIL_PID" 2>/dev/null || true
exit $STATUS
