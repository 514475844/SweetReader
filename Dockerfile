FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# 创建 instance 目录
RUN mkdir -p /app/instance

# 设置启动脚本
COPY docker-entrypoint.sh /docker-entrypoint.sh
RUN chmod +x /docker-entrypoint.sh

EXPOSE 5000

# 容器自愈：/health 挂了由 docker 自动重启（配合 --restart unless-stopped）
HEALTHCHECK --interval=60s --timeout=10s --start-period=45s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:5000/health', timeout=5).read()" || exit 1

ENTRYPOINT ["/docker-entrypoint.sh"]
