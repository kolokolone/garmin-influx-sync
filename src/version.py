"""Distribution version, read once from the installed package metadata."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

DISTRIBUTION = "garmin-influx-sync"

try:
    __version__ = version(DISTRIBUTION)
except PackageNotFoundError:  # source checkout that was never installed
    __version__ = "0.0.0+source"
