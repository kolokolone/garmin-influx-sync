"""Tests for the backfill decision and the refresh schedule."""

from __future__ import annotations

import unittest
from contextlib import contextmanager
from unittest import mock

from support import FakeWriter

from config import Settings
from influx import writer
from service import backfill, schedule
from service.backfill import (
    backfill_decision,
    maybe_backfill,
    query_has_records,
    run_backfill,
)
from service.schedule import next_delay

writer.RANGE_DELAY_SECONDS = 0
writer.DAY_DELAY_SECONDS = 0


class _FakeTable:
    def __init__(self, records: list[object]) -> None:
        self.records = records


class _FakeQueryApi:
    """Answer the marker and data probes from two booleans."""

    def __init__(self, marker: bool, has_data: bool) -> None:
        self.marker = marker
        self.has_data = has_data
        self.queries: list[str] = []

    def query(self, query: str, org: str | None = None) -> list[_FakeTable]:
        self.queries.append(query)
        probe = (
            self.marker
            if backfill.BACKFILL_STATE_MEASUREMENT in query
            else self.has_data
        )
        return [_FakeTable([object()] if probe else [])]


class _FakeInflux:
    def __init__(self, query_api: _FakeQueryApi, writer: FakeWriter | None = None) -> None:
        self._query_api = query_api
        self.writer = writer or FakeWriter()

    def query_api(self) -> _FakeQueryApi:
        return self._query_api

    def write_api(self, write_options: object = None) -> FakeWriter:
        return self.writer


@contextmanager
def _session(fake: object):
    yield fake


def _settings(**env: str) -> Settings:
    return Settings.from_env(
        {"GARMIN_PERSONAL_API_KEY": "key", "INFLUX_TOKEN": "token", **env}
    )


class BackfillDecisionTests(unittest.TestCase):
    def test_decision_table(self) -> None:
        for mode, marker, has_data, expected in (
            ("off", False, False, False),
            ("off", True, True, False),
            ("auto", True, True, False),
            ("auto", False, True, False),
            ("auto", False, False, True),
            ("on", True, True, False),
            ("on", False, True, True),
            ("on", False, False, True),
        ):
            with self.subTest(mode=mode, marker=marker, has_data=has_data):
                needed, reason = backfill_decision(
                    mode,
                    marker=marker,
                    has_data=has_data,
                )
                self.assertEqual(needed, expected)
                self.assertTrue(reason)

    def test_query_has_records(self) -> None:
        class Api:
            def __init__(self, tables: list[_FakeTable]) -> None:
                self.tables = tables

            def query(self, query: str, org: str | None = None) -> list[_FakeTable]:
                return self.tables

        self.assertFalse(query_has_records(Api([]), "homelab", "q"))
        self.assertFalse(query_has_records(Api([_FakeTable([])]), "homelab", "q"))
        self.assertTrue(query_has_records(Api([_FakeTable([1])]), "homelab", "q"))

    def test_bucket_names_are_escaped_in_flux(self) -> None:
        query = backfill.bucket_data_query('gar"min')
        self.assertIn('from(bucket: "gar\\"min")', query)


class MaybeBackfillTests(unittest.TestCase):
    def _run(
        self,
        settings: Settings,
        *,
        marker: bool,
        has_data: bool,
    ) -> tuple[_FakeQueryApi, mock.MagicMock]:
        api = _FakeQueryApi(marker, has_data)
        influx = _FakeInflux(api)
        with mock.patch.object(
            backfill,
            "influx_session",
            lambda settings: _session(influx),
        ), mock.patch.object(backfill, "run_backfill") as run:
            maybe_backfill(settings)
        return api, run

    def test_first_start_on_an_empty_bucket_backfills(self) -> None:
        api, run = self._run(_settings(), marker=False, has_data=False)

        run.assert_called_once()
        self.assertEqual(run.call_args.args[0].backfill_days, 365)
        self.assertEqual(len(api.queries), 2)

    def test_existing_data_is_not_downloaded_again(self) -> None:
        _, run = self._run(_settings(), marker=False, has_data=True)

        run.assert_not_called()

    def test_marker_prevents_a_second_backfill(self) -> None:
        for mode in ("auto", "on"):
            with self.subTest(mode=mode):
                api, run = self._run(
                    _settings(BACKFILL=mode),
                    marker=True,
                    has_data=True,
                )
                run.assert_not_called()
                self.assertEqual(len(api.queries), 1)

    def test_backfill_on_forces_a_populated_bucket(self) -> None:
        _, run = self._run(_settings(BACKFILL="on"), marker=False, has_data=True)

        run.assert_called_once()

    def test_off_never_touches_influx(self) -> None:
        api, run = self._run(_settings(BACKFILL="off"), marker=False, has_data=False)

        run.assert_not_called()
        self.assertEqual(api.queries, [])


class RunBackfillTests(unittest.TestCase):
    class FakeGarmin:
        def __init__(self) -> None:
            self.hr_days: list[str] = []

        def get_daily_hrv_summaries(self, start_date, end_date):
            return []

        def get_daily_steps(self, start_date, end_date):
            return []

        def get_daily_sleep_summaries(self, start_date, end_date):
            return []

        def get_daily_summaries(self, start_date, end_date):
            return []

        def search_activities(self, **kwargs):
            return []

        def get_daily_heart_rate(self, calendar_date: str) -> dict:
            self.hr_days.append(calendar_date)
            return {"restingHeartRate": 50}

    def test_writes_marker_and_bounds_heart_rate_days(self) -> None:
        settings = _settings(BACKFILL_HR_DAYS="2")
        garmin = self.FakeGarmin()
        fake_writer = FakeWriter()
        influx = _FakeInflux(_FakeQueryApi(False, False), fake_writer)

        with mock.patch.object(
            backfill,
            "garmin_session",
            lambda settings: _session(garmin),
        ), mock.patch.object(
            backfill,
            "influx_session",
            lambda settings: _session(influx),
        ):
            run_backfill(settings, 30)

        self.assertEqual(len(garmin.hr_days), 2)
        marker = next(
            line
            for line in fake_writer.lines()
            if line.startswith(f"{backfill.BACKFILL_STATE_MEASUREMENT} ")
        )
        self.assertIn("backfill_completed=1i", marker)
        self.assertIn("backfill_days=30i", marker)


class RefreshScheduleTests(unittest.TestCase):
    def test_next_delay_uses_interval_and_fast_retry(self) -> None:
        daily = _settings(REFRESH_INTERVAL="1d")
        self.assertEqual(next_delay(daily, succeeded=True), 86_400)
        self.assertEqual(next_delay(daily, succeeded=False), schedule.RETRY_SECONDS)

        quick = _settings(REFRESH_INTERVAL="15m")
        self.assertEqual(next_delay(quick, succeeded=True), 900)
        self.assertEqual(next_delay(quick, succeeded=False), schedule.RETRY_SECONDS)


if __name__ == "__main__":
    unittest.main()
