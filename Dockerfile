# Multi-arch index digest (amd64+arm64); bump deliberately. apt packages and the build backend are not locked.
FROM python:3.13-slim@sha256:bb2988715db2cf7ace7b53f38f3cffbef7c7046a656bee66245eb0ed386e2e81

RUN apt-get update \
    && apt-get install -y --no-install-recommends git openssh-client \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
# Pin the installer and use the committed dependency lock.
RUN pip install --no-cache-dir uv==0.12.17
ENV UV_PYTHON_DOWNLOADS=never \
    UV_LINK_MODE=copy \
    PATH="/app/.venv/bin:$PATH"
COPY pyproject.toml uv.lock README.md LICENSE ./
COPY gateway ./gateway
RUN uv sync --frozen --no-dev --no-editable

ENV VAULT_DIR=/vault
ENV DB_PATH=/data/gateway.db
EXPOSE 8000

CMD ["uvicorn", "gateway.app:app", "--host", "0.0.0.0", "--port", "8000"]
