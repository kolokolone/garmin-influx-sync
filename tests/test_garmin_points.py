"""Tests for payload → Point conversion."""

from __future__ import annotations

import unittest

from influxdb_client import Point
from support import ACTIVITY_ENTRY, DAILY_SUMMARY_ENTRY, DAY, SLEEP_ENTRY, TZ

from garmin.points import (
    activity_point,
    daily_steps,
    daily_summary_point,
    heart_rate_points,
    scalar_fields,
    sleep_point,
    to_daily_point,
)


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


if __name__ == "__main__":
    unittest.main()
