"""Tests for environment configuration."""

from __future__ import annotations

import unittest

from config import Settings, parse_duration


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
        self.assertEqual(settings.refresh_seconds, 21_600)
        self.assertEqual(settings.backfill, "auto")
        self.assertEqual(settings.backfill_days, 365)
        self.assertEqual(settings.backfill_hr_days, 30)
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
            ("REFRESH_INTERVAL", "899"),
            ("REFRESH_INTERVAL", "14m"),
            ("BACKFILL_DAYS", "0"),
            ("BACKFILL_DAYS", "366"),
            ("BACKFILL_HR_DAYS", "0"),
            ("BACKFILL_HR_DAYS", "366"),
            ("BACKFILL", "sometimes"),
        ):
            with self.subTest(name=name, value=value), self.assertRaises(ValueError):
                Settings.from_env({**base, name: value})
        with self.assertRaisesRegex(ValueError, "integer"):
            Settings.from_env({**base, "SYNC_DAYS": "three"})
        with self.assertRaisesRegex(ValueError, "Unknown TZ"):
            Settings.from_env({**base, "TZ": "Mars/Olympus"})

    def test_backfill_modes_and_interval_formats(self) -> None:
        base = {"GARMIN_PERSONAL_API_KEY": "key", "INFLUX_TOKEN": "token"}
        for raw, seconds in (
            ("6h", 21_600),
            ("12h", 43_200),
            ("1d", 86_400),
            ("90m", 5_400),
            ("21600", 21_600),
            (" 1w ", 604_800),
        ):
            with self.subTest(raw=raw):
                self.assertEqual(parse_duration(raw, "REFRESH_INTERVAL"), seconds)
        for mode in ("auto", "on", "off"):
            with self.subTest(mode=mode):
                settings = Settings.from_env({**base, "BACKFILL": mode.upper()})
                self.assertEqual(settings.backfill, mode)
        settings = Settings.from_env(
            {**base, "REFRESH_INTERVAL": "12h", "BACKFILL_DAYS": "90"}
        )
        self.assertEqual(settings.refresh_seconds, 43_200)
        self.assertEqual(settings.backfill_days, 90)

    def test_secrets_are_hidden_from_repr(self) -> None:
        settings = Settings.from_env(
            {
                "GARMIN_PERSONAL_API_KEY": "secret-key",
                "INFLUX_TOKEN": "secret-token",
            }
        )
        self.assertNotIn("secret-key", repr(settings))
        self.assertNotIn("secret-token", repr(settings))


if __name__ == "__main__":
    unittest.main()
