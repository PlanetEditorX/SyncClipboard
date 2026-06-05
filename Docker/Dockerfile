FROM python:3.12-slim

# 使用非交互安装并清理缓存
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

COPY requirements-server.txt ./
RUN python -m pip install --upgrade pip && \
    pip install --no-cache-dir -r requirements-server.txt

# 复制项目源代码
COPY . /app

# 复制并添加入口脚本（位于 Docker/ 目录下，相对于构建上下文使用 `-f Docker/Dockerfile .`）
COPY Docker/docker-entrypoint.sh /app/docker-entrypoint.sh

# 创建运行时目录
RUN mkdir -p /app/config /app/latest /app/log
RUN chmod +x /app/docker-entrypoint.sh

# 挂载点（便于宿主挂载配置、日志和 latest 数据目录）
VOLUME ["/app/config", "/app/log", "/app/latest"]

# 默认环境变量（可在运行时覆盖）
ENV CONFIG_DIR=/app/config
ENV LOG_DIR=/app/log
ENV LATEST_DIR=/app/latest
ENV PORT=8000

EXPOSE 8000

CMD ["/app/docker-entrypoint.sh"]
