"""Convert Garmin API payloads into InfluxDB points.

Garmin endpoints can return missing data or change response shapes; unsupported
values are skipped instead of being fabricated.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, date, datetime
from datetime import time as datetime_time
from typing import Any
from zoneinfo import ZoneInfo

from influxdb_client import Point, WritePrecision

# Plausible epoch-second range for Garmin heart-rate and activity samples (2000..2100).
MIN_EPOCH_SECONDS = 946_684_800
MAX_EPOCH_SECONDS = 4_102_444_800


def is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def scalar_fields(point: Point, source: Any, names: Mapping[str, str]) -> bool:
    if not isinstance(source, dict):
        return False
    found = False
    for source_name, dest_name in names.items():
        value = source.get(source_name)
        if is_number(value):
            point.field(dest_name, float(value))
            found = True
    return found


def to_daily_point(measurement: str, day: date, local_tz: ZoneInfo) -> Point:
    stamp = datetime.combine(day, datetime_time.min, tzinfo=local_tz)
    return Point(measurement).time(stamp, WritePrecision.NS)


def date_from_value(value: Any) -> date | None:
    if isinstance(value, str):
        try:
            return date.fromisoformat(value[:10])
        except ValueError:
            return None
    if isinstance(value, datetime):
        return value.date()
    return None


def epoch_datetime(value: Any) -> datetime | None:
    if not is_number(value):
        return None
    seconds = value / 1000 if value > 10**11 else value
    if not MIN_EPOCH_SECONDS <= seconds <= MAX_EPOCH_SECONDS:
        return None
    return datetime.fromtimestamp(seconds, tz=UTC)


def parse_timestamp(value: Any, fallback_tz: ZoneInfo) -> datetime | None:
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


def items(response: Any, *keys: str) -> list[Any]:
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
            if is_number(value):
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
        if not is_number(raw_time) or not is_number(raw_hr):
            continue
        if not 20 <= raw_hr <= 250:
            continue
        stamp = epoch_datetime(raw_time)
        if stamp is None:
            continue
        points.append(Point("garmin_heart_rate").field("bpm", float(raw_hr)).time(stamp))
    return points


def sleep_point(entry: Any, local_tz: ZoneInfo) -> Point | None:
    if not isinstance(entry, dict):
        return None
    day = date_from_value(entry.get("calendarDate"))
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
            if is_number(value):
                point.field("sleep_score", float(value))
                found = True

    need = entry.get("sleepNeed")
    if isinstance(need, dict):
        value = need.get("actual")
        if is_number(value):
            point.field("sleep_need_minutes", float(value))
            found = True

    for source, destination in (
        ("sleepStartTimestampLocal", "bedtime_local_ms"),
        ("sleepEndTimestampLocal", "wake_local_ms"),
    ):
        value = entry.get(source)
        if is_number(value):
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
    day = date_from_value(entry.get("calendarDate"))
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
        if is_number(value):
            point.field(name, float(value))
            found = True
    return point if found else None


def activity_point(entry: Any, local_tz: ZoneInfo) -> Point | None:
    if not isinstance(entry, dict):
        return None

    stamp = epoch_datetime(entry.get("beginTimestamp"))
    if stamp is None:
        stamp = parse_timestamp(entry.get("startTimeGMT"), UTC)
    if stamp is None:
        stamp = parse_timestamp(entry.get("startTimeLocal"), local_tz)
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
    if is_number(activity_id):
        point.field("activity_id", int(activity_id))
        found = True
    return point if found else None
