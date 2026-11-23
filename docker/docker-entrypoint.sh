#!/bin/sh
set -e

# 确保运行期目录存在（-volume 挂载时目录由 Docker 创建，直跑镜像时靠这里）
mkdir -p /app/config /app/log /app/latest /app/downloads

# 强制 Python 无缓冲输出，避免日志积压
export PYTHONUNBUFFERED=1

# 直接运行主服务
exec python server/linux_run.py