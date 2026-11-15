# 填报助手：FastAPI + Playwright(Chromium) + SQLite
FROM python:3.12-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    DATA_DIR=/data \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

# Chromium 运行所需系统库
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl \
        libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 libatspi2.0-0 \
        libcups2 libdrm2 libxkbcommon0 libxcomposite1 libxdamage1 \
        libxfixes3 libxrandr2 libgbm1 libpango-1.0-0 libcairo2 \
        libasound2 libdbus-1-3 libxi6 libwayland-server0 \
        fonts-noto-cjk \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY app/requirements.txt /app/requirements.txt
RUN pip install -r /app/requirements.txt

# 构建期下载浏览器；可用构建参数切换镜像源
ARG PLAYWRIGHT_DOWNLOAD_HOST=https://cdn.npmmirror.com/binaries/playwright
RUN PLAYWRIGHT_DOWNLOAD_HOST=${PLAYWRIGHT_DOWNLOAD_HOST} \
    python -m playwright install chromium

COPY app /app/app

RUN mkdir -p /data && \
    adduser --disabled-password --gecos "" appuser && \
    chown -R appuser:appuser /data /app /ms-playwright
USER appuser

EXPOSE 8000
CMD ["uvicorn", "app.webapp:app", "--host", "0.0.0.0", "--port", "8000"]
