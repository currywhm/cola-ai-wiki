FROM python:3.12-slim
WORKDIR /app
ARG DEBIAN_MIRROR=https://mirrors.aliyun.com
RUN sed -i "s|http://deb.debian.org|${DEBIAN_MIRROR}|g" /etc/apt/sources.list.d/debian.sources \
    && apt-get -o Acquire::https::Timeout=60 -o Acquire::Retries=5 update \
    && apt-get -o Acquire::https::Timeout=60 -o Acquire::Retries=5 install -y --no-install-recommends tesseract-ocr tesseract-ocr-chi-sim \
    && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
ARG PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple
RUN pip install --no-cache-dir --timeout 60 --retries 5 --index-url "${PIP_INDEX_URL}" --extra-index-url https://pypi.org/simple -r requirements.txt
COPY app ./app
# 运营文案（使用技巧等）：镜像内自带一份，生产也可以挂载卷覆盖后热更新
COPY content ./content
# 官方 Harness profile patch（仅覆盖官方 row，不携带自研插件）
COPY harness_runtime ./harness_runtime
RUN useradd --uid 10001 --create-home api \
    && mkdir -p data uploads cert \
    && chown -R api:api /app
USER api
ENV PYTHONUNBUFFERED=1
EXPOSE 8000
ENV PORT=8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 CMD python -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:' + os.environ.get('PORT','8000') + '/ready', timeout=3)"
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
