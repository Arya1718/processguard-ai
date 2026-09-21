# ---------------------------------------------------------------------------
# Mock ERP/CMMS (Prompt 5) -- stand-in for Buckman's real ERP/CMMS.
# Multi-stage build, slim runtime, non-root user (same pattern as the
# agent-service image). Runs on an internal Docker network only -- no host
# port mapping -- see docker-compose.yml + docs/security-model.md.
# ---------------------------------------------------------------------------
FROM python:3.11-slim AS build

WORKDIR /build

COPY requirements.txt .
RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --no-cache-dir --upgrade pip \
    && /opt/venv/bin/pip install --no-cache-dir -r requirements.txt

# ---------------------------------------------------------------------------
FROM python:3.11-slim AS final

# Non-root user
RUN groupadd -r cmms && useradd -r -g cmms cmms

WORKDIR /app

COPY --from=build /opt/venv /opt/venv
COPY main.py ./main.py

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

USER cmms

EXPOSE 8100

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8100"]
