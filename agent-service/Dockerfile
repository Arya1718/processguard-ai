# ---------------------------------------------------------------------------
# ProcessGuard AI -- agent service (Python / FastAPI) image
# Multi-stage: builder installs dependencies into a venv, then a slim
# non-root runtime stage copies only the venv + application code.
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
RUN groupadd -r pgai && useradd -r -g pgai pgai

WORKDIR /app

COPY --from=build /opt/venv /opt/venv
COPY app ./app

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

USER pgai

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
