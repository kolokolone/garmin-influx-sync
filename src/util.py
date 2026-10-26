"""Logging setup and error formatting shared by the bridge."""

from __future__ import annotations

import logging


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
