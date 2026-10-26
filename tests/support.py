"""Fixtures shared across the test modules."""

from __future__ import annotations

from datetime import date
from zoneinfo import ZoneInfo

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
    """Collect InfluxDB write calls instead of sending them."""

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
