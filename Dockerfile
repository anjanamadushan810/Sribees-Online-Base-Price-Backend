# Sribees Supply API (same layout as Sribees Finance's image).
FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Dependencies first, so a code-only change reuses this layer.
COPY pyproject.toml ./
RUN mkdir app && touch app/__init__.py \
    && pip install . \
    && pip uninstall -y sribees-supply \
    && rm -rf app

COPY alembic.ini ./
COPY alembic ./alembic
COPY app ./app

# A fixed unprivileged user.
RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin supply
USER supply

EXPOSE 8000

HEALTHCHECK --interval=15s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4)"

# No --proxy-headers: app/api/deps.client_ip reads X-Forwarded-For itself (last hop =
# Caddy's), and uvicorn must not rewrite the client address from a forgeable header.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
