"""Command-line entry point."""

from __future__ import annotations

import argparse
import logging

from config import MAX_BACKFILL_DAYS, Settings
from service.backfill import run_backfill
from service.schedule import run_service
from util import configure_logging, describe_error
from version import __version__

LOG = logging.getLogger("garmin-influx-sync")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="garmin-influx-sync",
        description="Sync Garmin Connect Personal API metrics into InfluxDB 2.x.",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    parser.add_argument(
        "--backfill",
        type=int,
        metavar="DAYS",
        help=(
            f"backfill the last DAYS days (1..{MAX_BACKFILL_DAYS}), "
            "ignore the backfill marker and exit"
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
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
        except Exception as exc:  # noqa: BLE001 - report and exit, never traceback
            LOG.error("Backfill failed: %s", describe_error(exc))
            return 1
        return 0

    LOG.info(
        "Starting Garmin -> InfluxDB sync v%s: %d day(s) every %ds, "
        "backfill=%s (%d days, %d heart-rate days), timezone %s, bucket %s",
        __version__,
        settings.sync_days,
        settings.refresh_seconds,
        settings.backfill,
        settings.backfill_days,
        settings.backfill_hr_days,
        settings.local_tz.key,
        settings.influx_bucket,
    )
    run_service(settings)
    return 0
