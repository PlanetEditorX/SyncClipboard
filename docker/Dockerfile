FROM python:3.12-slim

# 使用非交互安装并清理缓存
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1
# python:3.12-slim 不带时区数据；不装 tzdata 时 TZ=Asia/Shanghai 不生效，
# datetime.now() 会一直按 UTC 记账，日志与共享 TTL 都比本地时间差 8 小时。
ENV TZ=Asia/Shanghai
RUN apt-get update && \
    apt-get install -y --no-install-recommends tzdata && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY docker/requirements-server.txt ./
RUN python -m pip install --upgrade pip && \
    pip install --no-cache-dir -r requirements-server.txt

# 复制项目源代码（根目录的 .dockerignore 在这里生效，排除 .git/.venv/状态文件）
COPY . /app

# 复制并添加入口脚本（位于 docker/ 目录下，相对于构建上下文使用 `-f docker/Dockerfile .`）
COPY docker/docker-entrypoint.sh /app/docker-entrypoint.sh

# 创建运行时目录
RUN mkdir -p /app/config /app/latest /app/log /app/downloads
RUN chmod +x /app/docker-entrypoint.sh

# 挂载点（便于宿主挂载配置、日志、latest 数据目录和收到的文件）
# /app/downloads 一定要挂：手机上传的文件默认落在这里，不挂则容器重启即丢。
VOLUME ["/app/config", "/app/log", "/app/latest", "/app/downloads"]

# 默认环境变量（可在运行时覆盖）
ENV CONFIG_DIR=/app/config
ENV LOG_DIR=/app/log
ENV LATEST_DIR=/app/latest
ENV SAVE_PATH=/app/downloads
ENV PORT=8000

# EXPOSE 只是声明；用 -p 映射到别的宿主端口时，PORT 环境变量也要同步改，
# linux_run.py 会把 PORT 同时写回 app.config，保证下发的 URL 端口正确。
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD python -c "import os,urllib.request;urllib.request.urlopen('http://127.0.0.1:%s/status' % os.environ.get('PORT','8000'),timeout=3)" || exit 1

CMD ["/app/docker-entrypoint.sh"]
