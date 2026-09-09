# Vox-Orchestrator — production image.
# The voice extra (pipecat) is installed here because the deployed service is
# what actually terminates Twilio Media Streams; set INSTALL_VOICE=false for a
# slim API-only image (webhooks + reports + eval).
FROM python:3.11-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt pyproject.toml README.md ./
RUN pip install --upgrade pip && pip install -r requirements.txt

ARG INSTALL_VOICE=false
COPY . .
RUN pip install --no-deps -e . \
    && if [ "$INSTALL_VOICE" = "true" ]; then pip install ".[voice]"; fi \
    && apt-get purge -y build-essential && apt-get autoremove -y

RUN useradd --create-home --uid 10001 vox && chown -R vox:vox /app
USER vox

ENV HOST=0.0.0.0 \
    PORT=8000 \
    APP_ENV=production \
    LOG_JSON=true

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS "http://127.0.0.1:${PORT}/health" || exit 1

# One worker: each call holds a stateful websocket + pipeline; scale out with
# more instances, not more workers in the same container.
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT} --workers 1 --timeout-keep-alive 75"]
