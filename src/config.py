"""Configuration read from environment variables."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DEFAULT_INFLUX_URL = "http://influxdb:8086"
DEFAULT_INFLUX_ORG = "homelab"
DEFAULT_INFLUX_BUCKET = "garmin"
DEFAULT_SYNC_DAYS = 3
MIN_SYNC_DAYS = 1
MAX_SYNC_DAYS = 31
MAX_BACKFILL_DAYS = 365
DEFAULT_BACKFILL_MODE = "auto"
BACKFILL_MODES = ("auto", "on", "off")
DEFAULT_BACKFILL_HR_DAYS = 30
DEFAULT_REFRESH_INTERVAL = "6h"
MIN_REFRESH_SECONDS = 900
DEFAULT_TZ = "Europe/Paris"
INFLUX_TIMEOUT_MS = 30_000

_DURATION_RE = re.compile(r"^(?P<value>[0-9]+)\s*(?P<unit>[smhdw])?$")
_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3_600, "d": 86_400, "w": 604_800}


@dataclass(frozen=True)
class Settings:
    garmin_api_key: str = field(repr=False)
    influx_token: str = field(repr=False)
    influx_url: str
    influx_org: str
    influx_bucket: str
    sync_days: int
    refresh_seconds: int
    backfill: str
    backfill_days: int
    backfill_hr_days: int
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

        sync_days = int_from_env(env, "SYNC_DAYS", DEFAULT_SYNC_DAYS)
        if not MIN_SYNC_DAYS <= sync_days <= MAX_SYNC_DAYS:
            raise ValueError(
                f"SYNC_DAYS must be between {MIN_SYNC_DAYS} and {MAX_SYNC_DAYS}"
            )

        refresh_seconds = parse_duration(
            env.get("REFRESH_INTERVAL", DEFAULT_REFRESH_INTERVAL),
            "REFRESH_INTERVAL",
        )
        if refresh_seconds < MIN_REFRESH_SECONDS:
            raise ValueError(
                f"REFRESH_INTERVAL must be at least {MIN_REFRESH_SECONDS} seconds"
            )

        backfill = env.get("BACKFILL", DEFAULT_BACKFILL_MODE).strip().lower()
        backfill = backfill or DEFAULT_BACKFILL_MODE
        if backfill not in BACKFILL_MODES:
            raise ValueError(
                f"BACKFILL must be one of {', '.join(BACKFILL_MODES)}, got {backfill!r}"
            )

        backfill_days = int_from_env(env, "BACKFILL_DAYS", MAX_BACKFILL_DAYS)
        if not 1 <= backfill_days <= MAX_BACKFILL_DAYS:
            raise ValueError(f"BACKFILL_DAYS must be between 1 and {MAX_BACKFILL_DAYS}")

        backfill_hr_days = int_from_env(
            env, "BACKFILL_HR_DAYS", DEFAULT_BACKFILL_HR_DAYS
        )
        if not 1 <= backfill_hr_days <= MAX_BACKFILL_DAYS:
            raise ValueError(
                f"BACKFILL_HR_DAYS must be between 1 and {MAX_BACKFILL_DAYS}"
            )

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
            refresh_seconds=refresh_seconds,
            backfill=backfill,
            backfill_days=backfill_days,
            backfill_hr_days=backfill_hr_days,
            local_tz=local_tz,
        )


def parse_duration(raw: str, name: str) -> int:
    """Parse ``6h``, ``12h``, ``1d``, ``90m`` or a plain number of seconds."""
    match = _DURATION_RE.fullmatch(raw.strip().lower())
    if match is None or int(match["value"]) <= 0:
        raise ValueError(
            f"{name} must be a positive duration such as 6h, 12h, 1d "
            f"or a number of seconds, got {raw!r}"
        )
    return int(match["value"]) * _UNIT_SECONDS[match["unit"] or "s"]


def int_from_env(env: Mapping[str, str], name: str, default: int) -> int:
    raw = env.get(name, str(default)).strip()
    try:
        return int(raw)
    except ValueError:
        raise ValueError(f"{name} must be an integer, got {raw!r}") from None
