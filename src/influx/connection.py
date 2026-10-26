"""InfluxDB 2.x connections."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from influxdb_client import InfluxDBClient
from influxdb_client.client.write_api import SYNCHRONOUS

from config import INFLUX_TIMEOUT_MS, Settings


@contextmanager
def influx_session(settings: Settings) -> Iterator[InfluxDBClient]:
    """Yield a client for one unit of work."""
    with InfluxDBClient(
        url=settings.influx_url,
        token=settings.influx_token,
        org=settings.influx_org,
        timeout=INFLUX_TIMEOUT_MS,
    ) as client:
        yield client


def writer_for(client: InfluxDBClient) -> Any:
    """Return the synchronous write API used for every metric write."""
    return client.write_api(write_options=SYNCHRONOUS)
