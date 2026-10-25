from __future__ import annotations

import unittest
from datetime import date
from zoneinfo import ZoneInfo

from influxdb_client import Point

from sync import (
    Settings,
    daily_steps,
    heart_rate_points,
    scalar_fields,
    sync_day,
    to_daily_point,
)


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
        point = to_daily_point(
            "garmin_test",
            date(2026, 10, 25),
            ZoneInfo("Europe/Paris"),
        )
        point.field("value", 1.0)
        self.assertIn("garmin_test value=1", point.to_line_protocol())

    def test_daily_steps_shapes(self) -> None:
        day = date(2026, 10, 25)
        self.assertEqual(
            daily_steps(
                [{"calendarDate": "2026-10-25", "totalSteps": 1234}],
                day,
            ),
            1234,
        )
        self.assertEqual(
            daily_steps(
                {"dailySteps": [{"calendarDate": "2026-10-25", "totalSteps": 42}]},
                day,
            ),
            42,
        )
        self.assertEqual(
            daily_steps({"calendarDate": "2026-10-25", "steps": 7}, day),
            7,
        )
        self.assertIsNone(
            daily_steps(
                [{"calendarDate": "2026-10-24", "totalSteps": 1}],
                day,
            )
        )
        self.assertIsNone(daily_steps(None, day))
        self.assertIsNone(
            daily_steps(
                {"dailySteps": [{"calendarDate": "2026-10-25", "totalSteps": True}]},
                day,
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


class SyncDayTests(unittest.TestCase):
    def test_sync_day_writes_all_available_metrics(self) -> None:
        day = date(2026, 10, 25)

        class FakeGarmin:
            def get_daily_hrv(self, calendar_date: str) -> dict:
                return {
                    "hrvSummary": {
                        "lastNightAvg": 42,
                        "weeklyAvg": 40,
                        "lastNight5MinHigh": 80,
                    }
                }

            def get_daily_heart_rate(self, calendar_date: str) -> dict:
                return {
                    "restingHeartRate": 50,
                    "minHeartRate": 40,
                    "maxHeartRate": 120,
                    "heartRateValues": [[1_700_000_000_000, 60]],
                }

            def get_daily_steps(self, start_date: str, end_date: str) -> list:
                return [{"calendarDate": day.isoformat(), "totalSteps": 1234}]

        class FakeWriter:
            def __init__(self) -> None:
                self.calls: list[dict] = []

            def write(self, **kwargs) -> None:
                self.calls.append(kwargs)

        settings = Settings.from_env(
            {
                "GARMIN_PERSONAL_API_KEY": "key",
                "INFLUX_TOKEN": "token",
                "INFLUX_BUCKET": "test-bucket",
                "INFLUX_ORG": "test-org",
            }
        )
        writer = FakeWriter()
        sync_day(FakeGarmin(), writer, settings, day)  # type: ignore[arg-type]

        self.assertEqual(len(writer.calls), 1)
        self.assertEqual(writer.calls[0]["bucket"], "test-bucket")
        self.assertEqual(writer.calls[0]["org"], "test-org")
        lines = [point.to_line_protocol() for point in writer.calls[0]["record"]]
        self.assertEqual(len(lines), 4)
        self.assertTrue(any(line.startswith("garmin_hrv ") for line in lines))
        self.assertTrue(any(line.startswith("garmin_daily_hr ") for line in lines))
        self.assertTrue(any(line.startswith("garmin_heart_rate ") for line in lines))
        self.assertTrue(any(line.startswith("garmin_steps ") for line in lines))

    def test_sync_day_raises_when_all_requests_fail(self) -> None:
        class FailingGarmin:
            def get_daily_hrv(self, calendar_date: str) -> dict:
                raise RuntimeError("boom")

            def get_daily_heart_rate(self, calendar_date: str) -> dict:
                raise RuntimeError("boom")

            def get_daily_steps(self, start_date: str, end_date: str) -> list:
                raise RuntimeError("boom")

        class FakeWriter:
            def write(self, **kwargs) -> None:
                raise AssertionError("nothing should be written")

        settings = Settings.from_env(
            {
                "GARMIN_PERSONAL_API_KEY": "key",
                "INFLUX_TOKEN": "token",
            }
        )
        with self.assertRaisesRegex(RuntimeError, "all Garmin metric requests failed"):
            sync_day(  # type: ignore[arg-type]
                FailingGarmin(),
                FakeWriter(),
                settings,
                date(2026, 10, 25),
            )


if __name__ == "__main__":
    unittest.main()
