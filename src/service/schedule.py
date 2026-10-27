"""Refresh scheduling: when the bridge polls, retries and starts up."""

from __future__ import annotations

import logging
import time
from datetime import date, datetime, timedelta

from config import Settings
from garmin.connection import garmin_session
from influx.connection import influx_session, writer_for
from influx.writer import sync_heart_rate_days, sync_range_metrics
from service.backfill import maybe_backfill
from util import describe_error

LOG = logging.getLogger("garmin-influx-sync")

RETRY_SECONDS = 300
# Extra overlap added to one refresh interval, so a late cycle (or a retry)
# still re-reads the daily endpoints across the midnight boundary.
REFRESH_OVERLAP_SECONDS = 3 * 3_600


def refresh_window(settings: Settings, now: datetime) -> tuple[date, date]:
    """Return the inclusive local date range refreshed by one cycle.

    ``SYNC_DAYS`` is the floor. The window is widened to cover at least the
    whole ``REFRESH_INTERVAL`` plus ``REFRESH_OVERLAP_SECONDS`` before ``now``,
    so two consecutive cycles always overlap and no date is skipped when a
    cycle starts late.
    """
    today = now.date()
    sync_start = today - timedelta(days=settings.sync_days - 1)
    overlap_start = (
        now - timedelta(seconds=settings.refresh_seconds + REFRESH_OVERLAP_SECONDS)
    ).date()
    return min(sync_start, overlap_start), today


def run_cycle(settings: Settings, now: datetime | None = None) -> None:
    """Refresh one cycle's local date range once."""
    now = now or datetime.now(settings.local_tz)
    start, today = refresh_window(settings, now)

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
