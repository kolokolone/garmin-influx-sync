"""Initial backfill: detect the first sync and download history exactly once."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from influxdb_client import Point, WritePrecision

from config import Settings
from garmin.connection import garmin_session
from influx.connection import influx_session, writer_for
from influx.writer import sync_heart_rate_days, sync_range_metrics, write_points

LOG = logging.getLogger("garmin-influx-sync")

BACKFILL_STATE_MEASUREMENT = "garmin_sync_state"
BACKFILL_MARKER_FIELD = "backfill_completed"
# Small, daily measurements probed to decide whether the bucket is populated.
PRESENCE_CHECK_MEASUREMENTS = ("garmin_daily_summary", "garmin_steps", "garmin_sleep")


def _flux_string(value: str) -> str:
    """Quote a Flux string literal."""
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def backfill_marker_query(bucket: str) -> str:
    """Flux query that finds any completed-backfill marker in the bucket."""
    return (
        f"from(bucket: {_flux_string(bucket)})\n"
        "  |> range(start: 0)\n"
        f'  |> filter(fn: (r) => r._measurement == "{BACKFILL_STATE_MEASUREMENT}"'
        f' and r._field == "{BACKFILL_MARKER_FIELD}")\n'
        "  |> limit(n: 1)"
    )


def bucket_data_query(bucket: str) -> str:
    """Flux query that finds Garmin data already stored in the bucket."""
    condition = " or ".join(
        f'r._measurement == "{name}"' for name in PRESENCE_CHECK_MEASUREMENTS
    )
    return (
        f"from(bucket: {_flux_string(bucket)})\n"
        "  |> range(start: 0)\n"
        f"  |> filter(fn: (r) => {condition})\n"
        "  |> group()\n"
        "  |> limit(n: 1)"
    )


def query_has_records(query_api: Any, org: str, query: str) -> bool:
    tables = query_api.query(query, org=org)
    return any(getattr(table, "records", None) for table in tables)


def backfill_marker(days: int) -> Point:
    return (
        Point(BACKFILL_STATE_MEASUREMENT)
        .field(BACKFILL_MARKER_FIELD, 1)
        .field("backfill_days", int(days))
        .time(datetime.now(UTC), WritePrecision.NS)
    )


def backfill_decision(mode: str, *, marker: bool, has_data: bool) -> tuple[bool, str]:
    """Decide whether the initial backfill should run, and why."""
    if mode == "off":
        return False, "BACKFILL=off"
    if marker:
        return False, "a backfill already completed in this bucket"
    if mode == "on":
        return True, "BACKFILL=on"
    if has_data:
        return False, "the bucket already holds Garmin data; set BACKFILL=on to force it"
    return True, "empty bucket and no backfill marker"


def maybe_backfill(settings: Settings) -> None:
    """Run the initial backfill once, detected from the bucket contents."""
    if settings.backfill == "off":
        LOG.info("Initial backfill disabled (BACKFILL=off)")
        return

    with influx_session(settings) as influx:
        query_api = influx.query_api()
        marker = query_has_records(
            query_api,
            settings.influx_org,
            backfill_marker_query(settings.influx_bucket),
        )
        has_data = marker or query_has_records(
            query_api,
            settings.influx_org,
            bucket_data_query(settings.influx_bucket),
        )

    needed, reason = backfill_decision(
        settings.backfill,
        marker=marker,
        has_data=has_data,
    )
    if not needed:
        LOG.info("Skipping initial backfill: %s", reason)
        return

    LOG.info(
        "Running initial backfill of %d day(s): %s",
        settings.backfill_days,
        reason,
    )
    run_backfill(settings, settings.backfill_days)


def run_backfill(settings: Settings, days: int) -> None:
    today = datetime.now(settings.local_tz).date()
    start = today - timedelta(days=days - 1)
    hr_days = min(days, settings.backfill_hr_days)
    hr_start = max(start, today - timedelta(days=hr_days - 1))
    LOG.info(
        "Backfill %s..%s (%d days); heart-rate days from %s",
        start,
        today,
        days,
        hr_start,
    )

    with garmin_session(settings) as garmin, influx_session(settings) as influx:
        writer = writer_for(influx)
        sync_range_metrics(garmin, writer, settings, start, today)
        failed_days = sync_heart_rate_days(garmin, writer, settings, hr_start, today)

        write_points(writer, settings, [backfill_marker(days)])
        if failed_days:
            LOG.warning(
                "Backfill complete with %d heart-rate day(s) missing; "
                "rerun `garmin-influx-sync --backfill %d` to fill them",
                failed_days,
                days,
            )
        else:
            LOG.info("Backfill complete; marker written to garmin_sync_state")
