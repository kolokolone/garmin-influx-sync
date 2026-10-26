"""Tests for InfluxDB writes."""

from __future__ import annotations

import unittest

from support import ACTIVITY_ENTRY, DAILY_SUMMARY_ENTRY, DAY, SLEEP_ENTRY, FakeWriter

from config import Settings
from influx import writer
from influx.writer import sync_heart_rate_day, sync_range_metrics

writer.RANGE_DELAY_SECONDS = 0
writer.DAY_DELAY_SECONDS = 0


def settings() -> Settings:
    return Settings.from_env(
        {
            "GARMIN_PERSONAL_API_KEY": "key",
            "INFLUX_TOKEN": "token",
        }
    )


class HeartRateDayTests(unittest.TestCase):
    def test_sync_heart_rate_day_writes_daily_and_samples(self) -> None:
        class FakeGarmin:
            def get_daily_heart_rate(self, calendar_date: str) -> dict:
                return {
                    "restingHeartRate": 50,
                    "minHeartRate": 40,
                    "maxHeartRate": 120,
                    "heartRateValues": [[1_700_000_000_000, 60]],
                }

        fake_writer = FakeWriter()
        sync_heart_rate_day(  # type: ignore[arg-type]
            FakeGarmin(),
            fake_writer,
            settings(),
            DAY,
        )

        self.assertEqual(len(fake_writer.calls), 1)
        lines = fake_writer.lines()
        self.assertEqual(len(lines), 2)
        self.assertTrue(any(line.startswith("garmin_daily_hr ") for line in lines))
        self.assertTrue(any(line.startswith("garmin_heart_rate ") for line in lines))

    def test_sync_heart_rate_day_propagates_failure(self) -> None:
        class FailingGarmin:
            def get_daily_heart_rate(self, calendar_date: str) -> dict:
                raise RuntimeError("boom")

        with self.assertRaisesRegex(RuntimeError, "boom"):
            sync_heart_rate_day(  # type: ignore[arg-type]
                FailingGarmin(),
                FakeWriter(),
                settings(),
                DAY,
            )


class RangeSyncTests(unittest.TestCase):
    def test_sync_range_metrics_writes_new_measurements(self) -> None:
        class FakeGarmin:
            def get_daily_hrv_summaries(self, start_date, end_date):
                return {
                    "hrvSummaries": [
                        {
                            "calendarDate": DAY.isoformat(),
                            "lastNightAvg": 42,
                            "weeklyAvg": 40,
                            "lastNight5MinHigh": 80,
                        }
                    ]
                }

            def get_daily_steps(self, start_date, end_date):
                return [
                    {
                        "calendarDate": DAY.isoformat(),
                        "totalSteps": 1234,
                        "stepGoal": 2000,
                    }
                ]

            def get_daily_sleep_summaries(self, start_date, end_date):
                return [SLEEP_ENTRY]

            def get_daily_summaries(self, start_date, end_date):
                return {"data": [DAILY_SUMMARY_ENTRY]}

            def search_activities(self, *, start, limit, start_date, end_date):
                return [ACTIVITY_ENTRY] if start == 0 else []

        fake_writer = FakeWriter()
        sync_range_metrics(  # type: ignore[arg-type]
            FakeGarmin(),
            fake_writer,
            settings(),
            DAY,
            DAY,
        )

        lines = fake_writer.lines()
        measurements = {line.split(" ", 1)[0].split(",", 1)[0] for line in lines}
        self.assertEqual(
            measurements,
            {
                "garmin_hrv",
                "garmin_steps",
                "garmin_sleep",
                "garmin_daily_summary",
                "garmin_activity",
            },
        )
        self.assertTrue(
            any("garmin_steps " in line and "steps=1234i" in line for line in lines)
        )
        self.assertTrue(
            any(
                "garmin_hrv " in line and "last_night_avg_ms=42" in line
                for line in lines
            )
        )
        self.assertTrue(
            any("garmin_activity,activity_type=running " in line for line in lines)
        )

    def test_sync_range_metrics_is_isolated_per_metric(self) -> None:
        class FakeGarmin:
            def get_daily_hrv_summaries(self, start_date, end_date):
                return {
                    "hrvSummaries": [
                        {
                            "calendarDate": DAY.isoformat(),
                            "lastNightAvg": 42,
                        }
                    ]
                }

            def get_daily_steps(self, start_date, end_date):
                raise RuntimeError("steps down")

            def get_daily_sleep_summaries(self, start_date, end_date):
                return [SLEEP_ENTRY]

            def get_daily_summaries(self, start_date, end_date):
                return {"data": [DAILY_SUMMARY_ENTRY]}

            def search_activities(self, *, start, limit, start_date, end_date):
                return [ACTIVITY_ENTRY]

        fake_writer = FakeWriter()
        sync_range_metrics(  # type: ignore[arg-type]
            FakeGarmin(),
            fake_writer,
            settings(),
            DAY,
            DAY,
        )

        measurements = {
            line.split(" ", 1)[0].split(",", 1)[0] for line in fake_writer.lines()
        }
        self.assertNotIn("garmin_steps", measurements)
        self.assertIn("garmin_sleep", measurements)
        self.assertIn("garmin_daily_summary", measurements)
        self.assertIn("garmin_activity", measurements)


if __name__ == "__main__":
    unittest.main()
