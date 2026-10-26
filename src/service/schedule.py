"""Refresh scheduling: when the bridge polls, retries and starts up."""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta

from config import Settings
from garmin.connection import garmin_session
from influx.connection import influx_session, writer_for
from influx.writer import sync_heart_rate_days, sync_range_metrics
from service.backfill import maybe_backfill
from util import describe_error

LOG = logging.getLogger("garmin-influx-sync")

RETRY_SECONDS = 300


def run_cycle(settings: Settings) -> None:
    """Refresh the last ``SYNC_DAYS`` days once."""
    today = datetime.now(settings.local_tz).date()
    start = today - timedelta(days=settings.sync_days - 1)

    with garmin_session(settings) as garmin, influx_session(settings) as influx:
        writer = writer_for(influx)
        sync_range_metrics(garmin, writer, settings, start, today)
        failed_days = sync_heart_rate_days(garmin, writer, settings, start, today)
        if failed_days:
            raise RuntimeError(f"{failed_days} day(s) failed")


def next_delay(settings: Settings, succeeded: bool) -> int:
    """Seconds to wait before the next refresh."""
    if succeeded:
        return settings.refresh_seconds
    return min(settings.refresh_seconds, RETRY_SECONDS)


def run_service(settings: Settings) -> None:
    """Backfill once when needed, then refresh on the configured interval."""
    try:
        maybe_backfill(settings)
    except Exception as exc:  # noqa: BLE001 - polling must start even if the probe fails
        LOG.error(
            "Initial backfill skipped, refresh continues: %s",
            describe_error(exc),
        )

    delay = settings.refresh_seconds
    while True:
        try:
            run_cycle(settings)
        except Exception as exc:  # noqa: BLE001 - retry instead of exiting the service
            LOG.error("Synchronization failed: %s", describe_error(exc))
            delay = next_delay(settings, succeeded=False)
        else:
            delay = next_delay(settings, succeeded=True)
        time.sleep(delay)
