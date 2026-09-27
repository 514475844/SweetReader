# ============================================================
# SweetReader 镜像体积优化（#554，2026-09-26）
# 策略：python:3.11-alpine 基础镜像（~50MB，远小于 slim 的 158MB）
#       + 多阶段构建（构建期装 build 工具，运行期只留运行时原生库）
#       + .dockerignore 排除 docs/markdown/缓存等非运行时文件
# 说明：lxml / Pillow 已有 musllinux 预编译 wheel，alpine 下无需现场编译；
#       即便个别包回退到源码编译，构建阶段已具备 gcc/头文件，不影响最终镜像体积。
# ============================================================

# ---------- 构建阶段：安装 Python 依赖 ----------
FROM python:3.11-alpine AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# 构建期原生库与编译工具（仅用于 pip 可能的源码编译，不会进入最终镜像）
RUN apk add --no-cache build-base libxml2-dev libxslt-dev jpeg-dev zlib-dev freetype-dev

WORKDIR /app
COPY requirements.txt .
# 装到 /install 前缀，便于整体搬运到运行阶段
RUN pip install --prefix=/install --no-cache-dir -r requirements.txt

# ---------- 运行阶段：极简运行时 ----------
FROM python:3.11-alpine

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

# 仅保留运行时需要的原生库；libarchive-tools 提供 bsdtar（rarfile 解 RAR 的后端）。
# 装完即清掉 apk 缓存
RUN apk add --no-cache libxml2 libxslt libjpeg-turbo zlib freetype libarchive-tools \
    && rm -rf /var/cache/apk/*

WORKDIR /app

# 搬运构建阶段装好的依赖
COPY --from=builder /install /usr/local

# 复制应用代码（.dockerignore 已排除非运行时文件）
COPY . .

# 创建 instance 目录
RUN mkdir -p /app/instance

# 启动脚本
COPY docker-entrypoint.sh /docker-entrypoint.sh
RUN chmod +x /docker-entrypoint.sh

EXPOSE 5000

# 容器自愈：/health 挂了由 docker 自动重启（配合 --restart unless-stopped）
HEALTHCHECK --interval=60s --timeout=10s --start-period=60s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:5000/health', timeout=5).read()" || exit 1

ENTRYPOINT ["/docker-entrypoint.sh"]
