FROM python:3.12-slim-bookworm

LABEL org.opencontainers.image.title="garmin-influx-sync" \
      org.opencontainers.image.description="Sync Garmin Connect Personal API metrics to InfluxDB 2.x" \
      org.opencontainers.image.version="0.1.0" \
      org.opencontainers.image.source="https://github.com/kolokolone/garmin-influx-sync"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    HOME=/home/app

WORKDIR /app

COPY pyproject.toml README.md /app/
COPY src /app/src

RUN pip install --no-cache-dir .

RUN groupadd --gid 1000 app \
    && useradd --create-home --uid 1000 --gid 1000 app \
    && mkdir -p /home/app/.config/garmin \
    && chown -R app:app /home/app

USER 1000:1000

CMD ["garmin-influx-sync"]
