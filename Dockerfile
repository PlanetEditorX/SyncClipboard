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

EXPOSE 8000

CMD ["python", "server/linux_run.py"]
