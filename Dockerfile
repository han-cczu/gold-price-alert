# 金价实时监控系统 - Dockerfile
# 多阶段构建，优化镜像大小

# ============ 构建阶段 ============
FROM python:3.11-slim as builder

WORKDIR /app

# 安装构建依赖
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# 复制依赖文件
COPY pyproject.toml .

# 安装 Python 依赖到虚拟环境
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# 先安装依赖（利用缓存）
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir build && \
    pip install --no-cache-dir \
    sqlalchemy>=2.0 \
    apscheduler>=3.10 \
    rich>=13.0 \
    httpx>=0.25 \
    pydantic>=2.0 \
    pydantic-settings>=2.0 \
    aiohttp>=3.9 \
    anthropic>=0.18 \
    openai>=1.10 \
    fastapi>=0.109 \
    uvicorn>=0.27

# 复制源代码
COPY src/ src/
COPY README.md .

# 安装项目
RUN pip install --no-cache-dir .


# ============ 运行阶段 ============
FROM python:3.11-slim as runtime

WORKDIR /app

# 创建非 root 用户
RUN useradd --create-home --shell /bin/bash appuser

# 从构建阶段复制虚拟环境
COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# 复制应用代码
COPY --from=builder /app/src /app/src
COPY --from=builder /app/README.md /app/

# 创建数据目录
RUN mkdir -p /app/data && chown -R appuser:appuser /app

# 切换到非 root 用户
USER appuser

# 环境变量
ENV PYTHONUNBUFFERED=1
ENV GOLD_DATABASE_URL=sqlite:///data/gold_prices.db
ENV GOLD_DATA_SOURCE=mock
ENV GOLD_LLM_PROVIDER=mock

# 暴露端口
EXPOSE 8000

# 健康检查
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD python -c "import httpx; httpx.get('http://localhost:8000/health').raise_for_status()"

# 默认启动 Web 服务
CMD ["gold-monitor", "web", "--host", "0.0.0.0", "--port", "8000"]
