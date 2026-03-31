# ─────────────────────────────────────────────────────────────
# templates/api-fastapi/Dockerfile
# FastAPI — Python 3.11 Alpine, non-root, /health 엔드포인트
#
# 서버는 PORT=8000 환경변수를 주입합니다.
# app/main.py에 /health 엔드포인트 필수
# ─────────────────────────────────────────────────────────────

# ── Stage 1: Builder ──────────────────────────────────────────
FROM python:3.11-alpine AS builder
WORKDIR /app

# 빌드 의존성 설치
RUN apk add --no-cache \
    gcc \
    musl-dev \
    libffi-dev \
    postgresql-dev \
    curl

# 의존성 설치 (prefix 방식으로 분리)
COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

# ── Stage 2: Runner ───────────────────────────────────────────
FROM python:3.11-alpine AS runner
WORKDIR /app

# 런타임 라이브러리
RUN apk add --no-cache libpq wget curl \
 && addgroup -g 1001 -S appgroup \
 && adduser  -u 1001 -S appuser -G appgroup

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
