#!/bin/sh
set -e

# 确保日志目录存在
mkdir -p /app/log

# 强制 Python 无缓冲输出，避免日志积压
export PYTHONUNBUFFERED=1

# 直接运行主服务
exec python server/linux_run.py