FROM python:3.13-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
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
