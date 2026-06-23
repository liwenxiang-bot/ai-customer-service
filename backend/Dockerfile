# syntax=docker/dockerfile:1
# Build context = repository root (so both backend/ and widget/ are available).

# ---- Stage 1: build the embeddable widget ----
FROM node:20-alpine AS widget-build
WORKDIR /widget
RUN npm i -g pnpm@10
COPY widget/package.json widget/pnpm-lock.yaml* ./
RUN pnpm install --no-frozen-lockfile
COPY widget/ ./
RUN pnpm build

# ---- Stage 2: python runtime (backend + worker share this image) ----
FROM python:3.12-slim AS runtime
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1
WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

COPY backend/requirements.txt .
RUN pip install -r requirements.txt

COPY backend/ /app/
# main.py serves the widget from /widget/dist (../../widget/dist relative to app/).
COPY --from=widget-build /widget/dist /widget/dist

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://localhost:8000/health/live || exit 1

# Default: API server. The worker overrides the command in compose.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "2"]
