FROM python:3.12-slim-bookworm
WORKDIR /app
ARG DEBIAN_MIRROR=https://mirrors.cloud.tencent.com
RUN sed -i "s|http://deb.debian.org|${DEBIAN_MIRROR}|g" /etc/apt/sources.list.d/debian.sources \
    && apt-get -o Acquire::https::Timeout=60 -o Acquire::Retries=5 update \
    && apt-get -o Acquire::https::Timeout=60 -o Acquire::Retries=5 install -y --no-install-recommends tesseract-ocr tesseract-ocr-chi-sim \
    && rm -rf /var/lib/apt/lists/*
COPY server/requirements.txt ./requirements.txt
ARG PIP_INDEX_URL=https://mirrors.cloud.tencent.com/pypi/simple
RUN pip install --no-cache-dir --timeout 60 --retries 5 --index-url "${PIP_INDEX_URL}" -r requirements.txt
COPY server/app ./app
COPY server/content ./content
COPY server/harness_runtime ./harness_runtime
RUN useradd --uid 10001 --create-home api \
    && mkdir -p data uploads cert \
    && chown -R api:api /app
USER api
ENV PYTHONUNBUFFERED=1
ENV APP_ENV=production \
    APP_NAME=cola知识库 \
    PORT=8000 \
    UPLOAD_DIR=/app/uploads \
    CONTENT_DIR=/app/content \
    CORS_ORIGINS=* \
    STORAGE_BACKEND=cos \
    COS_PREFIX=cola \
    WECHAT_API_BASE=https://api.weixin.qq.com \
    WECHAT_OPENAPI_BASE=http://api.weixin.qq.com \
    LLM_BASE_URL=https://api.deepseek.com \
    LLM_MODEL=deepseek-chat \
    HARNESS_ENABLED=true \
    HARNESS_HOME=/app/harness-home \
    HARNESS_PROFILE=sdk \
    HARNESS_PROVIDER=deepseek-official \
    HARNESS_MODEL=deepseek-v4-flash \
    HARNESS_MAX_TOKENS=4096 \
    DSH_PERMISSION_MODE=workspace-write \
    HARNESS_SHELL_AVAILABLE=false \
    HARNESS_REASONING_EFFORT=low \
    HARNESS_WORKSPACES=/app/harness-workspaces \
    HARNESS_MAX_RUNTIMES=6 \
    HARNESS_IDLE_SECONDS=1800 \
    HARNESS_QUICK_REASONING_EFFORT=low \
    HARNESS_DEEP_REASONING_EFFORT=high
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 CMD python -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:' + os.environ.get('PORT','8000') + '/ready', timeout=3)"
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
