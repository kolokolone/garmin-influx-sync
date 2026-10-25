from __future__ import annotations

import unittest
from datetime import date
from zoneinfo import ZoneInfo

from influxdb_client import Point

import sync
from sync import (
    Settings,
    activity_point,
    daily_steps,
    daily_summary_point,
    heart_rate_points,
    scalar_fields,
    sleep_point,
    sync_heart_rate_day,
    sync_range_metrics,
    to_daily_point,
)

sync.RANGE_DELAY_SECONDS = 0
sync.DAY_DELAY_SECONDS = 0

DAY = date(2026, 10, 25)
TZ = ZoneInfo("Europe/Paris")

SLEEP_ENTRY = {
    "calendarDate": "2026-10-25",
    "sleepTimeSeconds": 28800,
    "napTimeSeconds": 0,
    "unmeasurableSleepSeconds": 0,
    "deepSleepSeconds": 7200,
    "lightSleepSeconds": 14400,
    "remSleepSeconds": 7200,
    "awakeSleepSeconds": 1800,
    "awakeCount": 2,
    "avgSleepStress": 20,
    "avgHeartRate": 50,
    "averageSpO2Value": 96.5,
    "lowestSpO2Value": 92,
    "averageRespirationValue": 14.5,
    "sleepStartTimestampLocal": 1_792_882_800_000,
    "sleepEndTimestampLocal": 1_792_911_600_000,
    "sleepScores": {"overall": {"value": 85}},
    "sleepNeed": {"actual": 480},
}

DAILY_SUMMARY_ENTRY = {
    "calendarDate": "2026-10-25",
    "calories": {
        "burnedResting": 1600,
        "burnedActive": 500,
        "burnedTotal": 2100,
    },
    "heartRate": {"minValue": 45, "maxValue": 120, "restingValue": 48},
    "intensityMinutes": {"moderate": 30, "vigorous": 10, "goal": 150},
    "stress": {"avgLevel": 30, "maxLevel": 70},
    "bodyBattery": {
        "minValue": 20,
        "maxValue": 90,
        "chargedValue": 50,
        "drainedValue": 40,
        "latestValue": 60,
    },
    "respiration": {"avgValue": 14, "minValue": 12, "maxValue": 18},
    "pulseOx": {"avgValue": 96, "minValue": 92},
}

ACTIVITY_ENTRY = {
    "activityId": 12345,
    "beginTimestamp": 1_792_882_800_000,
    "activityType": {"typeKey": "running"},
    "duration": 1800,
    "elapsedDuration": 2000,
    "movingDuration": 1700,
    "distance": 5000,
    "calories": 400,
    "averageHR": 140,
    "maxHR": 170,
    "steps": 6000,
    "activityTrainingLoad": 80,
    "moderateIntensityMinutes": 20,
    "vigorousIntensityMinutes": 10,
}


class FakeWriter:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def write(self, **kwargs) -> None:
        self.calls.append(kwargs)

    def lines(self) -> list[str]:
        return [
            point.to_line_protocol()
            for call in self.calls
            for point in call["record"]
        ]


class SettingsTests(unittest.TestCase):
    def test_defaults(self) -> None:
        settings = Settings.from_env(
            {
                "GARMIN_PERSONAL_API_KEY": " key ",
                "INFLUX_TOKEN": " token ",
            }
        )
        self.assertEqual(settings.garmin_api_key, "key")
        self.assertEqual(settings.influx_token, "token")
        self.assertEqual(settings.influx_url, "http://influxdb:8086")
        self.assertEqual(settings.influx_org, "homelab")
        self.assertEqual(settings.influx_bucket, "garmin")
        self.assertEqual(settings.sync_days, 3)
        self.assertEqual(settings.poll_seconds, 21_600)
        self.assertEqual(settings.local_tz.key, "Europe/Paris")

    def test_required_credentials(self) -> None:
        with self.assertRaisesRegex(ValueError, "GARMIN_PERSONAL_API_KEY"):
            Settings.from_env({"INFLUX_TOKEN": "token"})
        with self.assertRaisesRegex(ValueError, "INFLUX_TOKEN"):
            Settings.from_env({"GARMIN_PERSONAL_API_KEY": "key"})

    def test_ranges(self) -> None:
        base = {"GARMIN_PERSONAL_API_KEY": "key", "INFLUX_TOKEN": "token"}
        for name, value in (
            ("SYNC_DAYS", "0"),
            ("SYNC_DAYS", "32"),
            ("POLL_SECONDS", "899"),
        ):
            with self.subTest(name=name, value=value):
                with self.assertRaises(ValueError):
                    Settings.from_env({**base, name: value})
        with self.assertRaisesRegex(ValueError, "integer"):
            Settings.from_env({**base, "SYNC_DAYS": "three"})
        with self.assertRaisesRegex(ValueError, "Unknown TZ"):
            Settings.from_env({**base, "TZ": "Mars/Olympus"})

    def test_secrets_are_hidden_from_repr(self) -> None:
        settings = Settings.from_env(
            {
                "GARMIN_PERSONAL_API_KEY": "secret-key",
                "INFLUX_TOKEN": "secret-token",
            }
        )
        self.assertNotIn("secret-key", repr(settings))
        self.assertNotIn("secret-token", repr(settings))


class PointBuildingTests(unittest.TestCase):
    def test_scalar_fields_skips_bools_and_strings(self) -> None:
        point = Point("test")
        found = scalar_fields(
            point,
            {"a": 1, "b": True, "c": "2"},
            {"a": "a", "b": "b", "c": "c"},
        )
        self.assertTrue(found)
        line = point.to_line_protocol()
        self.assertIn("test a=1", line)
        self.assertNotIn("b=", line)
        self.assertNotIn("c=", line)

    def test_daily_point_uses_local_timezone(self) -> None:
        point = to_daily_point("garmin_test", DAY, TZ)
        point.field("value", 1.0)
        self.assertIn("garmin_test value=1", point.to_line_protocol())

    def test_daily_steps_shapes(self) -> None:
        self.assertEqual(
            daily_steps(
                [{"calendarDate": "2026-10-25", "totalSteps": 1234}],
                DAY,
            ),
            1234,
        )
        self.assertEqual(
            daily_steps(
                {"dailySteps": [{"calendarDate": "2026-10-25", "totalSteps": 42}]},
                DAY,
            ),
            42,
        )
        self.assertEqual(
            daily_steps({"calendarDate": "2026-10-25", "steps": 7}, DAY),
            7,
        )
        self.assertIsNone(
            daily_steps(
                [{"calendarDate": "2026-10-24", "totalSteps": 1}],
                DAY,
            )
        )
        self.assertIsNone(daily_steps(None, DAY))
        self.assertIsNone(
            daily_steps(
                {"dailySteps": [{"calendarDate": "2026-10-25", "totalSteps": True}]},
                DAY,
            )
        )

    def test_heart_rate_points_filter_and_timestamps(self) -> None:
        payload = {
            "heartRateValues": [
                [1_700_000_000_000, 60],
                [1_700_000_060, 61],
                [1_700_000_060, None],
                [0, 60],
                [1_700_000_000_000, 300],
            ]
        }
        points = heart_rate_points(payload)
        self.assertEqual(len(points), 2)
        lines = [point.to_line_protocol() for point in points]
        self.assertIn("garmin_heart_rate bpm=60", lines[0])
        self.assertTrue(lines[0].endswith("1700000000000000000"))
        self.assertIn("garmin_heart_rate bpm=61", lines[1])
        self.assertTrue(lines[1].endswith("1700000060000000000"))

    def test_heart_rate_rejects_invalid_payloads(self) -> None:
        self.assertEqual(heart_rate_points({"heartRateValues": "nope"}), [])
        self.assertEqual(heart_rate_points(None), [])

    def test_sleep_point(self) -> None:
        point = sleep_point(SLEEP_ENTRY, TZ)
        self.assertIsNotNone(point)
        line = point.to_line_protocol()  # type: ignore[union-attr]
        self.assertTrue(line.startswith("garmin_sleep "))
        self.assertIn("sleep_seconds=28800", line)
        self.assertIn("sleep_score=85", line)
        self.assertIn("sleep_need_minutes=480", line)
        self.assertIn("bedtime_local_ms=1792882800000i", line)

    def test_daily_summary_point(self) -> None:
        point = daily_summary_point(DAILY_SUMMARY_ENTRY, TZ)
        self.assertIsNotNone(point)
        line = point.to_line_protocol()  # type: ignore[union-attr]
        self.assertTrue(line.startswith("garmin_daily_summary "))
        self.assertIn("calories_active=500", line)
        self.assertIn("heart_rate_resting=48", line)
        self.assertIn("intensity_moderate=30", line)
        self.assertIn("body_battery_latest=60", line)

    def test_activity_point(self) -> None:
        point = activity_point(ACTIVITY_ENTRY, TZ)
        self.assertIsNotNone(point)
        line = point.to_line_protocol()  # type: ignore[union-attr]
        self.assertTrue(line.startswith("garmin_activity,activity_type=running "))
        self.assertIn("activity_id=12345i", line)
        self.assertIn("duration_seconds=1800", line)
        self.assertIn("average_hr=140", line)


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

        settings = Settings.from_env(
            {
                "GARMIN_PERSONAL_API_KEY": "key",
                "INFLUX_TOKEN": "token",
              }
        )
        writer = FakeWriter()
        sync_heart_rate_day(FakeGarmin(), writer, settings, DAY)  # type: ignore[arg-type]

        self.assertEqual(len(writer.calls), 1)
        lines = writer.lines()
        self.assertEqual(len(lines), 2)
        self.assertTrue(any(line.startswith("garmin_daily_hr ") for line in lines))
        self.assertTrue(any(line.startswith("garmin_heart_rate ") for line in lines))

    def test_sync_heart_rate_day_propagates_failure(self) -> None:
        class FailingGarmin:
            def get_daily_heart_rate(self, calendar_date: str) -> dict:
                raise RuntimeError("boom")

        settings = Settings.from_env(
            {
                "GARMIN_PERSONAL_API_KEY": "key",
                "INFLUX_TOKEN": "token",
            }
        )
        with self.assertRaisesRegex(RuntimeError, "boom"):
            sync_heart_rate_day(  # type: ignore[arg-type]
                FailingGarmin(),
                FakeWriter(),
                settings,
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

        settings = Settings.from_env(
            {
                "GARMIN_PERSONAL_API_KEY": "key",
                "INFLUX_TOKEN": "token",
            }
        )
        writer = FakeWriter()
        sync_range_metrics(  # type: ignore[arg-type]
            FakeGarmin(),
            writer,
            settings,
            DAY,
            DAY,
        )

        lines = writer.lines()
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
        self.assertTrue(any("garmin_steps " in line and "steps=1234i" in line for line in lines))
        self.assertTrue(any("garmin_hrv " in line and "last_night_avg_ms=42" in line for line in lines))
        self.assertTrue(any("garmin_activity,activity_type=running " in line for line in lines))

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

        settings = Settings.from_env(
            {
                "GARMIN_PERSONAL_API_KEY": "key",
                "INFLUX_TOKEN": "token",
            }
        )
        writer = FakeWriter()
        sync_range_metrics(  # type: ignore[arg-type]
            FakeGarmin(),
            writer,
            settings,
            DAY,
            DAY,
        )

        measurements = {line.split(" ", 1)[0].split(",", 1)[0] for line in writer.lines()}
        self.assertNotIn("garmin_steps", measurements)
        self.assertIn("garmin_sleep", measurements)
        self.assertIn("garmin_daily_summary", measurements)
        self.assertIn("garmin_activity", measurements)


if __name__ == "__main__":
    unittest.main()
