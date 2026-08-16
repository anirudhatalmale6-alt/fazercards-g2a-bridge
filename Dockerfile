FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Dependencies first so a code change does not re-install them.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY migrations ./migrations

# Run as a non-root user: this container holds API credentials and game keys.
RUN useradd --create-home --uid 10001 bridge && chown -R bridge:bridge /app
USER bridge

# Overridden per service in docker-compose.yml (api / worker).
CMD ["python", "-m", "app.cli", "worker"]
