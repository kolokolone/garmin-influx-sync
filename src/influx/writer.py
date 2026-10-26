"""Write Garmin datasets into InfluxDB."""

from __future__ import annotations

import logging
import time
from datetime import date, timedelta
from typing import Any

from influxdb_client import Point

from config import Settings
from garmin.points import (
    activity_point,
    daily_summary_point,
    date_from_value,
    heart_rate_points,
    is_number,
    items,
    scalar_fields,
    sleep_point,
    step_entries,
    to_daily_point,
)
from util import describe_error

LOG = logging.getLogger("garmin-influx-sync")

RANGE_CHUNK_DAYS = 28
SUMMARY_CHUNK_DAYS = 90
ACTIVITY_PAGE_SIZE = 20
MAX_ACTIVITY_PAGES = 50
# Request pacing: Garmin answers 429 when hammered, InfluxDB writes are cheap.
RANGE_DELAY_SECONDS = 0.6
DAY_DELAY_SECONDS = 0.4


def date_chunks(start: date, end: date, chunk_days: int):
    current = start
    while current <= end:
        chunk_end = min(current + timedelta(days=chunk_days - 1), end)
        yield current, chunk_end
        current = chunk_end + timedelta(days=1)


def write_points(writer: Any, settings: Settings, points: list[Point]) -> int:
    if points:
        writer.write(
            bucket=settings.influx_bucket,
            org=settings.influx_org,
            record=points,
        )
    return len(points)


def sync_heart_rate_day(
    garmin: Any,
    writer: Any,
    settings: Settings,
    day: date,
) -> None:
    points: list[Point] = []
    day_iso = day.isoformat()

    hr = garmin.get_daily_heart_rate(day_iso) or {}
    if isinstance(hr, dict):
        point = to_daily_point("garmin_daily_hr", day, settings.local_tz)
        if scalar_fields(
            point,
            hr,
            {
                "restingHeartRate": "resting_bpm",
                "minHeartRate": "min_bpm",
                "maxHeartRate": "max_bpm",
            },
        ):
            points.append(point)
        points.extend(heart_rate_points(hr))

    write_points(writer, settings, points)
    LOG.info("%s: %d heart-rate points written", day_iso, len(points))


def sync_heart_rate_days(
    garmin: Any,
    writer: Any,
    settings: Settings,
    start: date,
    end: date,
) -> int:
    """Sync whole days of heart rate with pacing; return the failed day count."""
    failed = 0
    day = start
    while day <= end:
        try:
            sync_heart_rate_day(garmin, writer, settings, day)
        except Exception as exc:  # noqa: BLE001 - one bad day must not stop the range
            failed += 1
            LOG.error(
                "Unable to sync heart rate %s: %s",
                day.isoformat(),
                describe_error(exc),
            )
        if day < end:
            time.sleep(DAY_DELAY_SECONDS)
        day += timedelta(days=1)
    return failed


def sync_hrv_range(
    garmin: Any,
    writer: Any,
    settings: Settings,
    start: date,
    end: date,
) -> None:
    total = 0
    for chunk_start, chunk_end in date_chunks(start, end, RANGE_CHUNK_DAYS):
        response = garmin.get_daily_hrv_summaries(chunk_start, chunk_end)
        points: list[Point] = []
        for entry in items(response, "hrvSummaries"):
            if not isinstance(entry, dict):
                continue
            day = date_from_value(entry.get("calendarDate"))
            if day is None:
                continue
            summary = entry.get("hrvSummary")
            if not isinstance(summary, dict):
                summary = entry
            point = to_daily_point("garmin_hrv", day, settings.local_tz)
            if scalar_fields(
                point,
                summary,
                {
                    "lastNightAvg": "last_night_avg_ms",
                    "weeklyAvg": "weekly_avg_ms",
                    "lastNight5MinHigh": "last_night_5min_high_ms",
                },
            ):
                points.append(point)
        total += write_points(writer, settings, points)
        time.sleep(RANGE_DELAY_SECONDS)
    LOG.info("HRV %s..%s: %d points written", start, end, total)


def sync_steps_range(
    garmin: Any,
    writer: Any,
    settings: Settings,
    start: date,
    end: date,
) -> None:
    total = 0
    for chunk_start, chunk_end in date_chunks(start, end, RANGE_CHUNK_DAYS):
        response = garmin.get_daily_steps(chunk_start, chunk_end)
        points: list[Point] = []
        for entry in step_entries(response):
            if not isinstance(entry, dict):
                continue
            day = date_from_value(entry.get("calendarDate"))
            if day is None:
                continue
            point = to_daily_point("garmin_steps", day, settings.local_tz)
            found = False
            value = entry.get("totalSteps")
            if not is_number(value):
                value = entry.get("steps")
            if is_number(value):
                point.field("steps", int(value))
                found = True
            goal = entry.get("stepGoal")
            if is_number(goal):
                point.field("step_goal", int(goal))
                found = True
            if found:
                points.append(point)
        total += write_points(writer, settings, points)
        time.sleep(RANGE_DELAY_SECONDS)
    LOG.info("Steps %s..%s: %d points written", start, end, total)


def sync_sleep_range(
    garmin: Any,
    writer: Any,
    settings: Settings,
    start: date,
    end: date,
) -> None:
    total = 0
    for chunk_start, chunk_end in date_chunks(start, end, RANGE_CHUNK_DAYS):
        response = garmin.get_daily_sleep_summaries(chunk_start, chunk_end)
        points = [
            point
            for entry in items(response, "sleepSummaries")
            if (point := sleep_point(entry, settings.local_tz)) is not None
        ]
        total += write_points(writer, settings, points)
        time.sleep(RANGE_DELAY_SECONDS)
    LOG.info("Sleep %s..%s: %d points written", start, end, total)


def sync_daily_summaries_range(
    garmin: Any,
    writer: Any,
    settings: Settings,
    start: date,
    end: date,
) -> None:
    total = 0
    for chunk_start, chunk_end in date_chunks(start, end, SUMMARY_CHUNK_DAYS):
        response = garmin.get_daily_summaries(chunk_start, chunk_end)
        points = [
            point
            for entry in items(response, "data")
            if (point := daily_summary_point(entry, settings.local_tz)) is not None
        ]
        total += write_points(writer, settings, points)
        time.sleep(RANGE_DELAY_SECONDS)
    LOG.info("Daily summaries %s..%s: %d points written", start, end, total)


def sync_activities_range(
    garmin: Any,
    writer: Any,
    settings: Settings,
    start: date,
    end: date,
) -> None:
    total = 0
    for page in range(MAX_ACTIVITY_PAGES):
        response = garmin.search_activities(
            start=page * ACTIVITY_PAGE_SIZE,
            limit=ACTIVITY_PAGE_SIZE,
            start_date=start,
            end_date=end,
        )
        entries = items(response, "activityList", "activities")
        if not entries:
            break
        points = [
            point
            for entry in entries
            if (point := activity_point(entry, settings.local_tz)) is not None
        ]
        total += write_points(writer, settings, points)
        if len(entries) < ACTIVITY_PAGE_SIZE:
            break
        time.sleep(RANGE_DELAY_SECONDS)
    LOG.info("Activities %s..%s: %d points written", start, end, total)


def sync_range_metrics(
    garmin: Any,
    writer: Any,
    settings: Settings,
    start: date,
    end: date,
) -> None:
    """Sync every range-based metric, isolating one failure from the others."""
    for name, function in (
        ("HRV", sync_hrv_range),
        ("Steps", sync_steps_range),
        ("Sleep", sync_sleep_range),
        ("Daily summaries", sync_daily_summaries_range),
        ("Activities", sync_activities_range),
    ):
        try:
            function(garmin, writer, settings, start, end)
        except Exception as exc:  # noqa: BLE001 - one dataset failure must not stop the others
            LOG.warning("%s %s..%s: %s", name, start, end, describe_error(exc))
