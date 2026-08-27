FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY pyproject.toml README.md LICENSE ./
COPY src ./src
COPY deploy/config.server.example.toml ./deploy/config.server.example.toml

RUN apt-get update \
    && apt-get install --no-install-recommends -y postgresql-client \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir ".[postgresql]" \
    && useradd --create-home --uid 10001 --shell /usr/sbin/nologin agentchatroom \
    && mkdir -p /var/lib/agentchatroom \
    && chown -R agentchatroom:agentchatroom /app /var/lib/agentchatroom

USER agentchatroom

ENV AGENTCHATROOM_DATA_DIR=/var/lib/agentchatroom \
    AGENTCHATROOM_CONFIG=/app/config.server.toml

EXPOSE 8765

CMD ["python", "-m", "agentchatroom", "serve", "--config", "/app/config.server.toml"]
