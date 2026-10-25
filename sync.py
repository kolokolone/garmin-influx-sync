"""Sync recent Garmin Connect Personal API metrics into InfluxDB 2.x.

This is a small homelab bridge, not a full Garmin archive. Garmin endpoints
can return missing data or change response shapes; unsupported values are
skipped instead of being fabricated.
"""

from __future__ import annotations

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
DEFAULT_POLL_SECONDS = 21_600
MIN_POLL_SECONDS = 900
DEFAULT_TZ = "Europe/Paris"
INFLUX_TIMEOUT_MS = 30_000
DAY_DELAY_SECONDS = 0.4
RETRY_SECONDS = 300

# Plausible epoch-second range for Garmin heart-rate samples (2000..2100).
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


def daily_steps(raw: Any, day: date) -> int | None:
    if isinstance(raw, dict):
        entries = raw.get("dailySteps") or raw.get("steps") or raw.get("items")
        if isinstance(entries, dict):
            entries = [entries]
        elif not isinstance(entries, list):
            entries = [raw]
    elif isinstance(raw, list):
        entries = raw
    else:
        return None

    day_iso = day.isoformat()
    for entry in entries:
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
        epoch = raw_time / 1000 if raw_time > 10**11 else raw_time
        if not MIN_EPOCH_SECONDS <= epoch <= MAX_EPOCH_SECONDS:
            continue
        stamp = datetime.fromtimestamp(epoch, tz=timezone.utc)
        points.append(Point("garmin_heart_rate").field("bpm", float(raw_hr)).time(stamp))
    return points


def sync_day(
    garmin: GarminPersonalApiClient,
    writer: Any,
    settings: Settings,
    day: date,
) -> None:
    points: list[Point] = []
    day_iso = day.isoformat()
    errors = 0

    try:
        hrv = garmin.get_daily_hrv(day_iso) or {}
        summary = hrv.get("hrvSummary") if isinstance(hrv, dict) else None
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
    except Exception as exc:
        errors += 1
        LOG.warning("HRV %s: %s", day_iso, describe_error(exc))

    try:
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
    except Exception as exc:
        errors += 1
        LOG.warning("Heart rate %s: %s", day_iso, describe_error(exc))

    try:
        steps = daily_steps(garmin.get_daily_steps(day_iso, day_iso), day)
        if steps is not None:
            points.append(
                to_daily_point("garmin_steps", day, settings.local_tz).field(
                    "steps", steps
                )
            )
    except Exception as exc:
        errors += 1
        LOG.warning("Steps %s: %s", day_iso, describe_error(exc))

    if points:
        writer.write(
            bucket=settings.influx_bucket,
            org=settings.influx_org,
            record=points,
        )
    LOG.info("%s: %d metric points written", day_iso, len(points))
    if errors == 3:
        raise RuntimeError("all Garmin metric requests failed")


def run_cycle(settings: Settings) -> None:
    today = datetime.now(settings.local_tz).date()
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
            failed_days = 0
            for index, delta in enumerate(range(settings.sync_days - 1, -1, -1)):
                day = today - timedelta(days=delta)
                try:
                    sync_day(garmin, writer, settings, day)
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
    configure_logging()
    try:
        settings = Settings.from_env()
    except ValueError as exc:
        LOG.error("Configuration error: %s", exc)
        return 2

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
