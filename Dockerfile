# ─────────────────────────────────────────────────────────────
# templates/api-fastapi/Dockerfile
# FastAPI — Python 3.11 Debian slim, non-root, /health 엔드포인트
#
# 서버는 PORT=8000 환경변수를 주입합니다.
# app/main.py에 /health 엔드포인트 필수
#
# 원래 Alpine(musl) 기반이었으나 Debian slim(glibc)으로 전환했다 — xgboost 등
# ETA 예측(app/services/eta_model.py)에 쓰는 ML 패키지가 musllinux wheel을
# 배포하지 않아 Alpine에서는 설치가 안 되거나 소스 빌드가 필요해질 수 있어서다.
# ─────────────────────────────────────────────────────────────

# ── Stage 1: Builder ──────────────────────────────────────────
FROM python:3.11-slim-bookworm AS builder
WORKDIR /app

# 빌드 의존성 설치
RUN apt-get update && apt-get install --no-install-recommends -y \
    build-essential \
    libffi-dev \
    libpq-dev \
    curl \
 && rm -rf /var/lib/apt/lists/*

# 의존성 설치 (prefix 방식으로 분리)
COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

# ── Stage 2: Runner ───────────────────────────────────────────
FROM python:3.11-slim-bookworm AS runner
WORKDIR /app

# 런타임 라이브러리
RUN apt-get update && apt-get install --no-install-recommends -y \
    libpq5 \
    wget \
    curl \
 && rm -rf /var/lib/apt/lists/* \
 && groupadd -g 1001 appgroup \
 && useradd -u 1001 -g appgroup -s /usr/sbin/nologin appuser

# 설치된 패키지 복사
COPY --from=builder /install /usr/local

# 소스 복사
COPY --chown=appuser:appgroup . .

ENV PORT=8000
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app

USER appuser
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
    CMD wget -qO- http://localhost:8000/health || exit 1

CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000} --workers 2"]
