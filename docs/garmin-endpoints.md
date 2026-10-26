# Garmin Connect Personal API — accessible endpoints

Inventory of everything the bridge can reach through Garmin's official
[Personal API](https://developer.garmin.com/gc-developer-program/personal/personal-faq/),
using `garmin-connect-personal-api-python` 1.0.0 (pinned in
[pyproject.toml](../pyproject.toml)).

- Base URL: `https://personal.connectapi.garmin.com` (override:
  `GARMIN_PERSONAL_API_BASE_URL`).
- Authentication: `POST /personalapitoken-service/personal-api/personal-api-key/token`
  with `Authorization: ApiKey <personal-api-key>` returns a JWT. The client
  caches it in `~/.config/garmin/connect-personal-api-bearer-token.json`
  (`garmin_auth` volume) and renews it when less than one hour remains.
- Every data endpoint is a `GET` returning JSON, except the activity download
  which returns a ZIP. A successful response with an empty body is `None`.
- Dates are `YYYY-MM-DD`; ranges are inclusive.

The client exposes a dedicated wrapper per endpoint (`client.<name>`), and
`client.get(path, params={...})` for anything else. Wrappers are added by
Garmin as a convenience and may lag behind the API.

## Endpoint table

`Ingested` says whether v0.1.0 stores it. Measurements live in
[architecture.md](architecture.md#measurements).

| # | Dataset | Wrapper | HTTP endpoint | Window | Ingested | Notes |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | Daily stress | `get_daily_stress` | `/usersummary-service/stats/stress/daily/{start}/{end}` | range | no | `stress_avg` only arrives via daily summaries |
| 2 | Daily steps | `get_daily_steps` | `/usersummary-service/stats/steps/daily/{start}/{end}` | range | **`garmin_steps`** | `totalSteps`, `stepGoal` |
| 3 | Hydration | `get_daily_hydration` | `/usersummary-service/stats/hydration/daily/{start}/{end}` | range | no | |
| 4 | Weight goals | `get_effective_weight_goals` | `/goal-service/goal/user/effective/weightgoal/{start}/{end}` | range | no | |
| 5 | Weight measurements | `get_weight_measurements` | `/weight-service/weight/range/{start}/{end}?includeAll=true` | range | no | body composition available here |
| 6 | Food logs | `get_daily_food_logs` | `/nutrition-service/food/logs/{date}` | day | no | one request per day, heavy payload |
| 7 | Profile settings | `get_user_profile_settings` | `/userprofile-service/userprofile/settings` | — | no | useful for timezone/unit sanity checks |
| 8 | Sleep (detailed) | `get_daily_sleep` | `/sleep-service/sleep/dailySleep?date={date}` | day | no | per-night stages/hypnogram |
| 9 | Sleep summaries | `get_daily_sleep_summaries` | `/sleep-service/sleep/dailySleepSummariesByDate?startDate=&endDate=` | range | **`garmin_sleep`** | what backfill uses |
| 10 | Body Battery | `get_daily_body_battery` | `/wellness-service/wellness/bodyBattery/reports/daily?startDate=&endDate=[&chunks=]` | range | no | only min/max/latest via daily summaries |
| 11 | Daily summaries | `get_daily_summaries` | `/usersummary-service/v2/dailysummaries?startDate=&endDate=` | range | **`garmin_daily_summary`** | calories, intensity, stress, body battery, respiration, pulse ox |
| 12 | Daily heart rate | `get_daily_heart_rate` | `/wellness-service/wellness/dailyHeartRate?date={date}` | day | **`garmin_heart_rate`**, **`garmin_daily_hr`** | `heartRateValues` = `[[epoch_ms, bpm], …]` ≈ 2-min cadence; **one request per day** |
| 13 | HRV (detailed) | `get_daily_hrv` | `/hrv-service/hrv/{date}` | day | no | 5-minute HRV readings |
| 14 | HRV summaries | `get_daily_hrv_summaries` | `/hrv-service/hrv/daily/{start}/{end}` | range | **`garmin_hrv`** | `lastNightAvg`, `weeklyAvg`, `lastNight5MinHigh` |
| 15 | Heart-rate zones | `get_heart_rate_zones` | `/biometric-service/heartRateZones/v2` | — | no | static-ish, good for banding panel colours |
| 16 | Menstrual cycle summary | `get_menstrual_cycle_summary` | `/periodichealth-service/menstrualcycle/summary/{date}` | day | no | |
| 17 | Menstrual cycle day view | `get_menstrual_cycle_day_view` | `/periodichealth-service/menstrualcycle/dayview/{date}?todayDate=` | day | no | |
| 18 | Fitness age | `get_fitness_age` | `/fitnessage-service/fitnessage/v2/{date}` | day | no | |
| 19 | SpO2 / acclimation | `get_daily_spo2` | `/wellness-service/wellness/daily/spo2acclimation/{date}` | day | no | averages already come from daily summaries |
| 20 | Wellness activity summaries | `get_wellness_activity_summaries` | `/wellnessactivity-service/activity/summary/range/{start}/{end}` | range | no | overlaps daily summaries |
| 21 | Activities (search) | `search_activities` | `/activitylist-service/activities/search/activities?start=&limit=[&startDate=&endDate=&activityType=]` | range | **`garmin_activity`** | pagination: `start` offset, `limit` page size (bridge uses 20 × 50 pages) |
| 22 | Activity details | `get_activity` | `/activity-service/activity/{id}` | id | no | per-lap/split details, activity-level metrics |
| 23 | Activity workouts | `get_activity_workouts` | `/activity-service/activity/{id}/workouts` | id | no | |
| 24 | **Activity file download** | `download_activity` | `/download-service/files/activity/{id}` | id | no | **binary ZIP** (original FIT), the only downloadable artefact |

## What can be downloaded as files

Only #24 returns a file: the original activity archive as ZIP bytes
(`download_activity(activity_id)`). The client holds it in memory and does not
stream, so large files are bounded by RAM. The CLI equivalent is
`gc-personal activity-download <id> -o run.zip` (it refuses to overwrite
without `--force`).

Everything else is JSON: the bridge downloads *data*, not files.

## Practical constraints

- **No bulk heart-rate endpoint.** Samples come one calendar day at a time,
  which is why the bridge keeps `BACKFILL_HR_DAYS` (default 30) separate from
  `BACKFILL_DAYS` (default 365).
- **Pagination.** Activity search is the only paged endpoint; the bridge stops
  at `MAX_ACTIVITY_PAGES` (50 pages × 20 = 1000 activities per range).
- **Pacing.** Garmin answers `429` when hammered. The bridge spaces range
  chunks 0.6 s apart and heart-rate days 0.4 s apart, and treats a failed day
  or chunk as a gap instead of aborting the whole run.
- **Partial data.** Days the watch slept in the drawer produce empty or
  incomplete payloads; missing fields are skipped, never invented.
- **Shapes drift.** Field names are read opportunistically (`dailySteps`,
  `steps`, `items`, `data`, `heartRateValues`, …) and unknown shapes are
  logged, not guessed.

## Endpoints worth adding next

Ranked by panel value versus API cost:

1. **Weight measurements** (#5) and **body composition** — absent from the
   current dashboard dataset.
2. **Body Battery timeline** (#10) — the daily summary only keeps
   min/max/charged/drained, so intraday charge curves need this endpoint.
3. **Detailed sleep** (#8) — hypnogram stage timeline per night.
4. **Heart-rate zones** (#15) — static thresholds for panel banding.
5. **Activity details** (#22) — laps and splits for per-activity panels.
