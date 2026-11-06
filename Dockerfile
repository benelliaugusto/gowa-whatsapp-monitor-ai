# syntax=docker/dockerfile:1
# Imagem do monitor-ia (FastAPI + SQLite). Build: docker build -t monitor-ia .
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=America/Sao_Paulo \
    PORT=8000 \
    DATABASE_PATH=/data/monitor.db \
    WHISPER_MODEL_DIR=/data/models

WORKDIR /app

RUN apt-get update \
 && apt-get install -y --no-install-recommends tzdata libgomp1 \
 && rm -rf /var/lib/apt/lists/* \
 && ln -snf "/usr/share/zoneinfo/${TZ}" /etc/localtime \
 && echo "${TZ}" > /etc/timezone

COPY requirements.txt requirements-whisper.txt ./
RUN pip install --no-cache-dir -r requirements.txt \
 && (pip install --no-cache-dir -r requirements-whisper.txt \
     || echo "AVISO: faster-whisper nao instalado; transcricao de audio desativada")

COPY app ./app
COPY docs ./docs
COPY README.md ./

RUN mkdir -p /data /data/models /data/backups
VOLUME ["/data"]
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import sys, urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=4).status == 200 else 1)"

# 1 worker: SQLite tem um único escritor (WAL permite leituras concorrentes).
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT} --proxy-headers --forwarded-allow-ips='*' --workers 1"]