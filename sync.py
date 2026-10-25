"""Sync recent Garmin Connect Personal API metrics into InfluxDB 2.x.

This is a small homelab bridge, not a full Garmin archive. Garmin endpoints
can return missing data or change response shapes; unsupported values are
skipped instead of being fabricated.
"""

from __future__ import annotations

import argparse
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import date, datetime, time as datetime_time, timedelta, timezone
from typing import Any, Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from garmin_personal_api import GarminPersonalApiClient, PersonalApiAuthenticator
from influxdb_client import InfluxDBClient, Point, WritePrecision
from influxdb_client.client.write_api import SYNCHRONOUS

LOG = logging.getLogger("garmin-influx-sync")

DEFAULT_INFLUX_URL = "http://influxdb:8086"
DEFAULT_INFLUX_ORG = "homelab"
DEFAULT_INFLUX_BUCKET = "garmin"
DEFAULT_SYNC_DAYS = 3
MIN_SYNC_DAYS = 1
MAX_SYNC_DAYS = 31
MAX_BACKFILL_DAYS = 365
DEFAULT_POLL_SECONDS = 21_600
MIN_POLL_SECONDS = 900
DEFAULT_TZ = "Europe/Paris"
INFLUX_TIMEOUT_MS = 30_000
DAY_DELAY_SECONDS = 0.4
RANGE_DELAY_SECONDS = 0.6
RETRY_SECONDS = 300
RANGE_CHUNK_DAYS = 28
SUMMARY_CHUNK_DAYS = 90
ACTIVITY_PAGE_SIZE = 20
MAX_ACTIVITY_PAGES = 50
BACKFILL_HR_DAYS = 30

# Plausible epoch-second range for Garmin heart-rate and activity samples (2000..2100).
MIN_EPOCH_SECONDS = 946_684_800
MAX_EPOCH_SECONDS = 4_102_444_800


@dataclass(frozen=True)
class Settings:
    garmin_api_key: str = field(repr=False)
    influx_token: str = field(repr=False)
    influx_url: str
    influx_org: str
    influx_bucket: str
    sync_days: int
    poll_seconds: int
    local_tz: ZoneInfo

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> Settings:
        env = os.environ if environ is None else environ

        garmin_api_key = env.get("GARMIN_PERSONAL_API_KEY", "").strip()
        if not garmin_api_key:
            raise ValueError("GARMIN_PERSONAL_API_KEY is required")

        influx_token = env.get("INFLUX_TOKEN", "").strip()
        if not influx_token:
            raise ValueError("INFLUX_TOKEN is required")

        influx_url = env.get("INFLUX_URL", DEFAULT_INFLUX_URL).strip().rstrip("/")
        if not influx_url:
            raise ValueError("INFLUX_URL must not be empty")

        influx_org = env.get("INFLUX_ORG", DEFAULT_INFLUX_ORG).strip()
        if not influx_org:
            raise ValueError("INFLUX_ORG must not be empty")

        influx_bucket = env.get("INFLUX_BUCKET", DEFAULT_INFLUX_BUCKET).strip()
        if not influx_bucket:
            raise ValueError("INFLUX_BUCKET must not be empty")

        sync_days = _int_from_env(env, "SYNC_DAYS", DEFAULT_SYNC_DAYS)
        if not MIN_SYNC_DAYS <= sync_days <= MAX_SYNC_DAYS:
            raise ValueError(
                f"SYNC_DAYS must be between {MIN_SYNC_DAYS} and {MAX_SYNC_DAYS}"
            )

        poll_seconds = _int_from_env(env, "POLL_SECONDS", DEFAULT_POLL_SECONDS)
        if poll_seconds < MIN_POLL_SECONDS:
            raise ValueError(f"POLL_SECONDS must be at least {MIN_POLL_SECONDS}")

        tz_name = env.get("TZ", DEFAULT_TZ).strip() or DEFAULT_TZ
        try:
            local_tz = ZoneInfo(tz_name)
        except ZoneInfoNotFoundError:
            raise ValueError(f"Unknown TZ {tz_name!r}") from None

        return cls(
            garmin_api_key=garmin_api_key,
            influx_token=influx_token,
            influx_url=influx_url,
            influx_org=influx_org,
            influx_bucket=influx_bucket,
            sync_days=sync_days,
            poll_seconds=poll_seconds,
            local_tz=local_tz,
        )


def _int_from_env(env: Mapping[str, str], name: str, default: int) -> int:
    raw = env.get(name, str(default)).strip()
    try:
        return int(raw)
    except ValueError:
        raise ValueError(f"{name} must be an integer, got {raw!r}") from None


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )


def describe_error(exc: BaseException) -> str:
    """Return a useful one-line error without dumping unbounded API bodies."""
    message = str(exc).strip()
    if len(message) > 300:
        message = message[:297] + "..."
    status = getattr(exc, "status_code", None)
    label = type(exc).__name__
    if status:
        label = f"{label} {status}"
    return f"{label}: {message}" if message else label


def scalar_fields(point: Point, source: Any, names: Mapping[str, str]) -> bool:
    if not isinstance(source, dict):
        return False
    found = False
    for source_name, dest_name in names.items():
        value = source.get(source_name)
        if _is_number(value):
            point.field(dest_name, float(value))
            found = True
    return found


def to_daily_point(measurement: str, day: date, local_tz: ZoneInfo) -> Point:
    stamp = datetime.combine(day, datetime_time.min, tzinfo=local_tz)
    return Point(measurement).time(stamp, WritePrecision.NS)


def _date_from_value(value: Any) -> date | None:
    if isinstance(value, str):
        try:
            return date.fromisoformat(value[:10])
        except ValueError:
            return None
    if isinstance(value, datetime):
        return value.date()
    return None


def _epoch_datetime(value: Any) -> datetime | None:
    if not _is_number(value):
        return None
    seconds = value / 1000 if value > 10**11 else value
    if not MIN_EPOCH_SECONDS <= seconds <= MAX_EPOCH_SECONDS:
        return None
    return datetime.fromtimestamp(seconds, tz=timezone.utc)


def _parse_timestamp(value: Any, fallback_tz: ZoneInfo) -> datetime | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        stamp = datetime.fromisoformat(text)
    except ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=fallback_tz)
    return stamp


def _items(response: Any, *keys: str) -> list[Any]:
    if isinstance(response, list):
        return response
    if isinstance(response, dict):
        for key in keys:
            value = response.get(key)
            if isinstance(value, list):
                return value
        data = response.get("data")
        if isinstance(data, list):
            return data
    return []


def _date_chunks(start: date, end: date, chunk_days: int):
    current = start
    while current <= end:
        chunk_end = min(current + timedelta(days=chunk_days - 1), end)
        yield current, chunk_end
        current = chunk_end + timedelta(days=1)


def step_entries(raw: Any) -> list[Any]:
    if isinstance(raw, dict):
        entries = raw.get("dailySteps") or raw.get("steps") or raw.get("items")
        if isinstance(entries, dict):
            return [entries]
        if isinstance(entries, list):
            return entries
        return [raw]
    if isinstance(raw, list):
        return raw
    return []


def daily_steps(raw: Any, day: date) -> int | None:
    day_iso = day.isoformat()
    for entry in step_entries(raw):
        if not isinstance(entry, dict):
            continue
        if entry.get("calendarDate", day_iso) != day_iso:
            continue
        for key in ("totalSteps", "steps", "value"):
            value = entry.get(key)
            if _is_number(value):
                return int(value)
    return None


def heart_rate_points(payload: Any) -> list[Point]:
    if not isinstance(payload, dict):
        return []
    samples = payload.get("heartRateValues")
    if not isinstance(samples, list):
        return []

    points: list[Point] = []
    for sample in samples:
        if not isinstance(sample, (list, tuple)) or len(sample) < 2:
            continue
        raw_time, raw_hr = sample[0], sample[1]
        if not _is_number(raw_time) or not _is_number(raw_hr):
            continue
        if not 20 <= raw_hr <= 250:
            continue
        stamp = _epoch_datetime(raw_time)
        if stamp is None:
            continue
        points.append(Point("garmin_heart_rate").field("bpm", float(raw_hr)).time(stamp))
    return points


def sleep_point(entry: Any, local_tz: ZoneInfo) -> Point | None:
    if not isinstance(entry, dict):
        return None
    day = _date_from_value(entry.get("calendarDate"))
    if day is None:
        return None

    point = to_daily_point("garmin_sleep", day, local_tz)
    found = scalar_fields(
        point,
        entry,
        {
            "sleepTimeSeconds": "sleep_seconds",
            "napTimeSeconds": "nap_seconds",
            "unmeasurableSleepSeconds": "unmeasurable_seconds",
            "deepSleepSeconds": "deep_seconds",
            "lightSleepSeconds": "light_seconds",
            "remSleepSeconds": "rem_seconds",
            "awakeSleepSeconds": "awake_seconds",
            "awakeCount": "awake_count",
            "avgSleepStress": "average_stress",
            "avgHeartRate": "average_heart_rate",
            "averageSpO2Value": "average_spo2",
            "lowestSpO2Value": "lowest_spo2",
            "averageRespirationValue": "average_respiration",
        },
    )

    scores = entry.get("sleepScores")
    if isinstance(scores, dict):
        overall = scores.get("overall")
        if isinstance(overall, dict):
            value = overall.get("value")
            if _is_number(value):
                point.field("sleep_score", float(value))
                found = True

    need = entry.get("sleepNeed")
    if isinstance(need, dict):
        value = need.get("actual")
        if _is_number(value):
            point.field("sleep_need_minutes", float(value))
            found = True

    for source, destination in (
        ("sleepStartTimestampLocal", "bedtime_local_ms"),
        ("sleepEndTimestampLocal", "wake_local_ms"),
    ):
        value = entry.get(source)
        if _is_number(value):
            point.field(destination, int(value))
            found = True

    return point if found else None


def _nested_number(source: Any, group: str, key: str) -> Any:
    if not isinstance(source, dict):
        return None
    nested = source.get(group)
    if not isinstance(nested, dict):
        return None
    return nested.get(key)


def daily_summary_point(entry: Any, local_tz: ZoneInfo) -> Point | None:
    if not isinstance(entry, dict):
        return None
    day = _date_from_value(entry.get("calendarDate"))
    if day is None:
        return None

    values = {
        "calories_resting": _nested_number(entry, "calories", "burnedResting"),
        "calories_active": _nested_number(entry, "calories", "burnedActive"),
        "calories_total": _nested_number(entry, "calories", "burnedTotal"),
        "heart_rate_min": _nested_number(entry, "heartRate", "minValue"),
        "heart_rate_max": _nested_number(entry, "heartRate", "maxValue"),
        "heart_rate_resting": _nested_number(entry, "heartRate", "restingValue"),
        "intensity_moderate": _nested_number(entry, "intensityMinutes", "moderate"),
        "intensity_vigorous": _nested_number(entry, "intensityMinutes", "vigorous"),
        "intensity_goal": _nested_number(entry, "intensityMinutes", "goal"),
        "stress_avg": _nested_number(entry, "stress", "avgLevel"),
        "stress_max": _nested_number(entry, "stress", "maxLevel"),
        "body_battery_min": _nested_number(entry, "bodyBattery", "minValue"),
        "body_battery_max": _nested_number(entry, "bodyBattery", "maxValue"),
        "body_battery_charged": _nested_number(entry, "bodyBattery", "chargedValue"),
        "body_battery_drained": _nested_number(entry, "bodyBattery", "drainedValue"),
        "body_battery_latest": _nested_number(entry, "bodyBattery", "latestValue"),
        "respiration_avg": _nested_number(entry, "respiration", "avgValue"),
        "respiration_min": _nested_number(entry, "respiration", "minValue"),
        "respiration_max": _nested_number(entry, "respiration", "maxValue"),
        "spo2_avg": _nested_number(entry, "pulseOx", "avgValue"),
        "spo2_min": _nested_number(entry, "pulseOx", "minValue"),
    }

    point = to_daily_point("garmin_daily_summary", day, local_tz)
    found = False
    for name, value in values.items():
        if _is_number(value):
            point.field(name, float(value))
            found = True
    return point if found else None


def activity_point(entry: Any, local_tz: ZoneInfo) -> Point | None:
    if not isinstance(entry, dict):
        return None

    stamp = _epoch_datetime(entry.get("beginTimestamp"))
    if stamp is None:
        stamp = _parse_timestamp(entry.get("startTimeGMT"), timezone.utc)
    if stamp is None:
        stamp = _parse_timestamp(entry.get("startTimeLocal"), local_tz)
    if stamp is None:
        return None

    activity_type = entry.get("activityType")
    type_key = activity_type.get("typeKey") if isinstance(activity_type, dict) else None
    type_key = str(type_key or "unknown").strip() or "unknown"
    point = Point("garmin_activity").tag("activity_type", type_key[:80]).time(stamp)

    found = scalar_fields(
        point,
        entry,
        {
            "duration": "duration_seconds",
            "elapsedDuration": "elapsed_seconds",
            "movingDuration": "moving_seconds",
            "distance": "distance_m",
            "calories": "calories",
            "averageHR": "average_hr",
            "maxHR": "max_hr",
            "steps": "steps",
            "activityTrainingLoad": "training_load",
            "moderateIntensityMinutes": "moderate_intensity_minutes",
            "vigorousIntensityMinutes": "vigorous_intensity_minutes",
        },
    )
    activity_id = entry.get("activityId")
    if _is_number(activity_id):
        point.field("activity_id", int(activity_id))
        found = True
    return point if found else None


def write_points(writer: Any, settings: Settings, points: list[Point]) -> int:
    if points:
        writer.write(
            bucket=settings.influx_bucket,
            org=settings.influx_org,
            record=points,
        )
    return len(points)


def sync_heart_rate_day(
    garmin: GarminPersonalApiClient,
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


def sync_hrv_range(
    garmin: GarminPersonalApiClient,
    writer: Any,
    settings: Settings,
    start: date,
    end: date,
) -> None:
    total = 0
    for chunk_start, chunk_end in _date_chunks(start, end, RANGE_CHUNK_DAYS):
        response = garmin.get_daily_hrv_summaries(chunk_start, chunk_end)
        points: list[Point] = []
        for entry in _items(response, "hrvSummaries"):
            if not isinstance(entry, dict):
                continue
            day = _date_from_value(entry.get("calendarDate"))
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
    garmin: GarminPersonalApiClient,
    writer: Any,
    settings: Settings,
    start: date,
    end: date,
) -> None:
    total = 0
    for chunk_start, chunk_end in _date_chunks(start, end, RANGE_CHUNK_DAYS):
        response = garmin.get_daily_steps(chunk_start, chunk_end)
        points: list[Point] = []
        for entry in step_entries(response):
            if not isinstance(entry, dict):
                continue
            day = _date_from_value(entry.get("calendarDate"))
            if day is None:
                continue
            point = to_daily_point("garmin_steps", day, settings.local_tz)
            found = False
            value = entry.get("totalSteps")
            if not _is_number(value):
                value = entry.get("steps")
            if _is_number(value):
                point.field("steps", int(value))
                found = True
            goal = entry.get("stepGoal")
            if _is_number(goal):
                point.field("step_goal", int(goal))
                found = True
            if found:
                points.append(point)
        total += write_points(writer, settings, points)
        time.sleep(RANGE_DELAY_SECONDS)
    LOG.info("Steps %s..%s: %d points written", start, end, total)


def sync_sleep_range(
    garmin: GarminPersonalApiClient,
    writer: Any,
    settings: Settings,
    start: date,
    end: date,
) -> None:
    total = 0
    for chunk_start, chunk_end in _date_chunks(start, end, RANGE_CHUNK_DAYS):
        response = garmin.get_daily_sleep_summaries(chunk_start, chunk_end)
        points = [
            point
            for entry in _items(response, "sleepSummaries")
            if (point := sleep_point(entry, settings.local_tz)) is not None
        ]
        total += write_points(writer, settings, points)
        time.sleep(RANGE_DELAY_SECONDS)
    LOG.info("Sleep %s..%s: %d points written", start, end, total)


def sync_daily_summaries_range(
    garmin: GarminPersonalApiClient,
    writer: Any,
    settings: Settings,
    start: date,
    end: date,
) -> None:
    total = 0
    for chunk_start, chunk_end in _date_chunks(start, end, SUMMARY_CHUNK_DAYS):
        response = garmin.get_daily_summaries(chunk_start, chunk_end)
        points = [
            point
            for entry in _items(response, "data")
            if (point := daily_summary_point(entry, settings.local_tz)) is not None
        ]
        total += write_points(writer, settings, points)
        time.sleep(RANGE_DELAY_SECONDS)
    LOG.info("Daily summaries %s..%s: %d points written", start, end, total)


def sync_activities_range(
    garmin: GarminPersonalApiClient,
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
        entries = _items(response, "activityList", "activities")
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
    garmin: GarminPersonalApiClient,
    writer: Any,
    settings: Settings,
    start: date,
    end: date,
) -> None:
    for name, function in (
        ("HRV", sync_hrv_range),
        ("Steps", sync_steps_range),
        ("Sleep", sync_sleep_range),
        ("Daily summaries", sync_daily_summaries_range),
        ("Activities", sync_activities_range),
    ):
        try:
            function(garmin, writer, settings, start, end)
        except Exception as exc:
            LOG.warning("%s %s..%s: %s", name, start, end, describe_error(exc))


def run_backfill(settings: Settings, days: int) -> None:
    today = datetime.now(settings.local_tz).date()
    start = today - timedelta(days=days - 1)
    hr_start = max(start, today - timedelta(days=BACKFILL_HR_DAYS - 1))
    LOG.info(
        "Backfill %s..%s (%d days); heart-rate days from %s",
        start,
        today,
        days,
        hr_start,
    )

    with PersonalApiAuthenticator() as auth:
        token = auth.obtain_token(settings.garmin_api_key)
    with GarminPersonalApiClient(token.access_token) as garmin:
        with InfluxDBClient(
            url=settings.influx_url,
            token=settings.influx_token,
            org=settings.influx_org,
            timeout=INFLUX_TIMEOUT_MS,
        ) as influx:
            writer = influx.write_api(write_options=SYNCHRONOUS)
            sync_range_metrics(garmin, writer, settings, start, today)

            day = hr_start
            while day <= today:
                try:
                    sync_heart_rate_day(garmin, writer, settings, day)
                except Exception as exc:
                    LOG.error(
                        "Unable to backfill heart rate %s: %s",
                        day.isoformat(),
                        describe_error(exc),
                    )
                day += timedelta(days=1)
                time.sleep(DAY_DELAY_SECONDS)


def run_cycle(settings: Settings) -> None:
    today = datetime.now(settings.local_tz).date()
    start = today - timedelta(days=settings.sync_days - 1)

    with PersonalApiAuthenticator() as auth:
        token = auth.obtain_token(settings.garmin_api_key)
    with GarminPersonalApiClient(token.access_token) as garmin:
        with InfluxDBClient(
            url=settings.influx_url,
            token=settings.influx_token,
            org=settings.influx_org,
            timeout=INFLUX_TIMEOUT_MS,
        ) as influx:
            writer = influx.write_api(write_options=SYNCHRONOUS)
            sync_range_metrics(garmin, writer, settings, start, today)

            failed_days = 0
            for index, delta in enumerate(range(settings.sync_days - 1, -1, -1)):
                day = today - timedelta(days=delta)
                try:
                    sync_heart_rate_day(garmin, writer, settings, day)
                except Exception as exc:
                    failed_days += 1
                    LOG.error(
                        "Unable to sync %s: %s",
                        day.isoformat(),
                        describe_error(exc),
                    )
                if index + 1 < settings.sync_days:
                    time.sleep(DAY_DELAY_SECONDS)
            if failed_days:
                raise RuntimeError(f"{failed_days} day(s) failed")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Sync Garmin Connect Personal API metrics into InfluxDB 2.x.",
    )
    parser.add_argument(
        "--backfill",
        type=int,
        metavar="DAYS",
        help=f"backfill the last DAYS days (1..{MAX_BACKFILL_DAYS}) and exit",
    )
    args = parser.parse_args()

    configure_logging()
    try:
        settings = Settings.from_env()
    except ValueError as exc:
        LOG.error("Configuration error: %s", exc)
        return 2

    if args.backfill is not None:
        if not 1 <= args.backfill <= MAX_BACKFILL_DAYS:
            LOG.error(
                "--backfill must be between 1 and %d",
                MAX_BACKFILL_DAYS,
            )
            return 2
        try:
            run_backfill(settings, args.backfill)
        except Exception as exc:
            LOG.error("Backfill failed: %s", describe_error(exc))
            return 1
        return 0

    LOG.info(
        "Starting Garmin -> InfluxDB sync: %d day(s), poll %ds, timezone %s, bucket %s",
        settings.sync_days,
        settings.poll_seconds,
        settings.local_tz.key,
        settings.influx_bucket,
    )
    delay = settings.poll_seconds
    while True:
        try:
            run_cycle(settings)
        except Exception as exc:
            LOG.error("Synchronization failed: %s", describe_error(exc))
            delay = min(settings.poll_seconds, RETRY_SECONDS)
        else:
            delay = settings.poll_seconds
        time.sleep(delay)


if __name__ == "__main__":
    raise SystemExit(main())
