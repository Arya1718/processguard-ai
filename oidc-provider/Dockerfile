# ---------------------------------------------------------------------------
# ProcessGuard AI -- local OIDC provider (stand-in for Microsoft Entra ID)
# Multi-stage build + non-root runtime, matching the other service images.
# DEV/DEMO ONLY: this container must never ship beyond local development.
# ---------------------------------------------------------------------------
FROM python:3.11-slim AS build

WORKDIR /build

COPY requirements.txt .
RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --no-cache-dir --upgrade pip \
    && /opt/venv/bin/pip install --no-cache-dir -r requirements.txt

# ---------------------------------------------------------------------------
FROM python:3.11-slim AS final

RUN groupadd -r oidc && useradd -r -g oidc oidc

WORKDIR /app

COPY --from=build /opt/venv /opt/venv
COPY app ./app

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

USER oidc

EXPOSE 8090

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8090"]
