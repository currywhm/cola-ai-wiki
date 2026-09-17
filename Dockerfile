FROM python:3.12-slim
WORKDIR /app
ARG DEBIAN_MIRROR=https://deb.debian.org
RUN sed -i "s|http://deb.debian.org|${DEBIAN_MIRROR}|g" /etc/apt/sources.list.d/debian.sources \
    && apt-get -o Acquire::https::Timeout=30 -o Acquire::Retries=2 update \
    && apt-get -o Acquire::https::Timeout=30 -o Acquire::Retries=2 install -y --no-install-recommends tesseract-ocr tesseract-ocr-chi-sim \
    && rm -rf /var/lib/apt/lists/*
COPY server/requirements.txt ./requirements.txt
ARG PIP_INDEX_URL=https://pypi.org/simple
RUN pip install --no-cache-dir --timeout 30 --retries 2 --index-url "${PIP_INDEX_URL}" -r requirements.txt
COPY server/app ./app
COPY server/content ./content
COPY server/harness_runtime ./harness_runtime
RUN useradd --uid 10001 --create-home api \
    && mkdir -p data uploads cert \
    && chown -R api:api /app
USER api
ENV PYTHONUNBUFFERED=1
ENV PORT=80
EXPOSE 80
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 CMD python -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:' + os.environ.get('PORT','80') + '/ready', timeout=3)"
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-80}"]
