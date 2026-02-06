# 金价实时监控系统 - Dockerfile (纯 Web 模式)

# ============ 构建阶段 ============
FROM python:3.11-slim as builder

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends gcc && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml .

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

RUN pip install --no-cache-dir --upgrade pip && \
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

COPY src/ src/
COPY README.md .

RUN pip install --no-cache-dir .


# ============ 运行阶段 ============
FROM python:3.11-slim as runtime

WORKDIR /app

RUN useradd --create-home --shell /bin/bash appuser

COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY --from=builder /app/src /app/src
COPY --from=builder /app/README.md /app/

RUN mkdir -p /app/data && chown -R appuser:appuser /app

USER appuser

ENV PYTHONUNBUFFERED=1
ENV GOLD_DATABASE_URL=sqlite:///data/gold_prices.db
ENV GOLD_DATA_SOURCE=mock
ENV GOLD_LLM_PROVIDER=mock
ENV GOLD_FETCH_INTERVAL=30

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=10s --start-period=10s --retries=3 \
    CMD python -c "import httpx; r=httpx.get('http://localhost:8000/health'); r.raise_for_status(); print(r.json())"

# 直接启动 Web 服务（内置数据采集）
CMD ["gold-monitor", "--host", "0.0.0.0", "--port", "8000"]
