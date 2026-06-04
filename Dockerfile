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

# 创建运行时目录
RUN mkdir -p /app/config /app/latest /app/log

# 挂载点（便于宿主挂载配置与日志文件）
VOLUME ["/app/config", "/app/log"]

# 默认环境变量（可在运行时覆盖）
ENV SERVER_CONFIG_FILE=/app/config/server_config.json
ENV LOG_FILE=/app/log/server_linux.log
ENV PORT=8000

EXPOSE 8000

CMD ["python", "server/linux_run.py"]
