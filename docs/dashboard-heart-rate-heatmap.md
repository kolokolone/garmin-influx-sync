# Panel audit — “Heart rate by hour of day, every day for a year”

Reference: `docs/dashboard-grafana_a_reproduire.jpg`, panel *The year → Heart
rate by hour of day, every day for a year*.

```text
        x = calendar date (≈365 columns, Nov 2025 → Oct 2026)
        y = hour of day (12am … 11pm, 24 rows)
   colour = heart rate in bpm (continuous, magenta → yellow)
     gaps = no watch on the wrist, drawn as empty cells
```

Status: **implemented in v0.1.1** as panel 23 of
[`dashboards/garmin-body-ops.json`](../dashboards/garmin-body-ops.json), using
the Business Charts (ECharts) plugin. The year grid renders, but Garmin's
Personal API only returns intraday `heartRateValues` for about the last 150
days: this account has samples from **2026-06-01** onward, while older dates
return an empty sample list (the daily resting/min/max values still exist).
Seven months of the reference are therefore empty, not missing from the bridge.
The reference's full year is not obtainable through this API; check the raw
payload before widening any backfill.

## 1. What the bridge stores today

| Fact | Where | Value |
| --- | --- | --- |
| Raw samples per day | `src/influx/writer.py` → `sync_heart_rate_day` | `heartRateValues` from the daily payload, ~2-min cadence (≈715 samples); **empty for dates older than ~150 days** |
| Measurement | `src/garmin/points.py` → `heart_rate_points` | `garmin_heart_rate`, field `bpm`, timestamp = **UTC** sample time |
| Filtering | same | samples outside `20..250` bpm or outside 2000–2100 are dropped |
| Daily aggregate | same file | `garmin_daily_hr` (`resting_bpm`, `min_bpm`, `max_bpm`) at local midnight; available for the full backfill |
| Refresh depth | `SYNC_DAYS` (default 3) | widened to `REFRESH_INTERVAL + 3 h` when that is wider; enough to keep the right edge fresh |
| Backfill depth for samples | `BACKFILL_HR_DAYS` (default 30) in `src/config.py` | **one HTTP request per day**; set to 365 for the year panel |
| Backfill depth for range datasets | `BACKFILL_DAYS` (default 365) | HRV, steps, sleep, summaries, activities |
| Backfill is once | `src/service/backfill.py` → `garmin_sync_state.backfill_completed` | marker; `--backfill` forces a re-run |

The v0.1.1 backfill ran 365 requests: it produced samples for **147 days,
2026-06-01 → 2026-10-27** (~105 000 points). Garmin returned an empty
`heartRateValues` list for earlier dates, so the panel shows five populated
months and seven empty ones. That boundary is server-side retention, not the
bridge's window.

## 2. Volume check

| Granularity | Points per year | Comment |
| --- | --- | --- |
| Raw samples (2 min) | ≈ 263 000 | theoretical; the API retains ≈ 105 000 (147 days) |
| Raw samples (1 min, some watches) | ≈ 525 000 | still trivial for InfluxDB |
| Hourly means | 8 760 | what the panel actually reads (≈ 3 500 here) |
| Matrix shown | 365 × 24 = 8 760 cells | empty cells before 2026-06-01 |

Memory and storage are not the constraint. **Garmin's intraday retention** and
request budget are.

## 3. Gap analysis

| Requirement | Have it? | Action |
| --- | --- | --- |
| 365 days of HR samples | **no — API-limited** (147 days) | `BACKFILL_HR_DAYS=365` already ran; earlier dates return `heartRateValues: []` |
| Local hour of day | yes | `option location` + `date.hour`/`date.truncate` in Flux, never UTC |
| Per-cell value | yes (mean of the hour) | query-time `aggregateWindow(every: 1h, fn: mean)` |
| Empty-cell semantics | yes (absent samples) | `createEmpty: false`; the panel still emits a calendar cell for every day |
| Continuous colour = bpm | yes | Business Charts (ECharts) + `visualMap` |
| Stable query cost at 30 s refresh | yes | ≈ 3 500 hourly points; no pre-aggregation needed |

Nothing new needs to be parsed from Garmin: the raw samples already contain the
information. The panel is a **window + rendering** problem; the missing months
are a source-retention limit, not a query bug.

## 4. Rendering options

| Option | Colour encodes bpm? | Extra plugin | Verdict |
| --- | --- | --- | --- |
| **ECharts heatmap** (volkovlabs `echarts-panel`) | yes, continuous `visualMap` | yes | **recommended** — only faithful reproduction |
| Native **Heatmap** panel + `histogram()` | no: colour = density of samples per bpm bucket, every cell count ≈ 1 | no | wrong visual, dismiss |
| Native **State timeline** + 24 pivoted hour fields + bpm thresholds | banded (5–6 colours) | no | good fallback, still readable |
| 24 trend series (one per hour) | n/a | no | spaghetti, dismiss |
| Canvas/HTML custom panel | yes | yes | overkill once ECharts exists |

Decision: **ECharts heatmap** for parity; it is implemented as panel 23 with
the **Business Charts** plugin (`volkovlabs-echarts-panel` 7.2.5, requires
Grafana ≥ 12.3). The native **state timeline** remains the no-plugin fallback,
but is not what the dashboard uses.

## 5. Query

Hourly means, local-aligned windows, one row per (hour, instant):

```flux
import "date"
import "timezone"

option location = timezone.location(name: "Europe/Paris")

from(bucket: "garmin")
  |> range(start: v.timeRangeStart, stop: v.timeRangeStop)
  |> filter(fn: (r) => r._measurement == "garmin_heart_rate" and r._field == "bpm")
  |> aggregateWindow(every: 1h, fn: mean, createEmpty: false)
  |> map(fn: (r) => ({r with hour: date.hour(t: r._time), day: date.truncate(t: r._time, unit: 1d)}))
  |> keep(columns: ["_time", "_value", "hour", "day"])
```

- `option location` shifts hourly window boundaries **and** the `date.*`
  conversions to local hours, so a cell is “07:00–08:00 Paris”, not “07:00 UTC”.
- Do **not** pass a literal `location: {zone: "Europe/Paris"}` to
  `date.hour`: Flux also requires `offset` there, and a fixed offset is wrong
  on DST days. The first version of this document did that and the query failed
  with `record is missing label offset`. The `option location` form is correct.
- `day` is the local-midnight instant. The panel formats it in Paris for the
  x-axis, so the matrix does not slide by an hour.
- The panel builds the calendar from the dashboard time range, not from the
  first data point, so months without samples remain visible as empty cells.
- **Validate against the deployed InfluxDB version**: `timezone` requires
  Flux ≥ 0.179 (InfluxDB 2.7+); the deployment runs InfluxDB 2.

ECharts needs `[xIndex, yIndex, bpm]` triples. They are built in panel 23's
`options.getOption` from the `day`, `hour` and `_value` fields; the calendar is
filled with every local date in the range, and dates outside the API's
retention get no point (transparent cell). The full code is in
[`dashboards/garmin-body-ops.json`](../dashboards/garmin-body-ops.json).

State-timeline fallback: pivot the 24 hourly columns, then apply bpm thresholds:

```flux
  |> pivot(rowKey: ["_time"], columnKey: ["hour"], valueColumn: "_value")
```

## 6. Cost of the backfill (the real work)

365 days × 1 request/day at 0.4 s pacing plus latency ≈ **4 to 5 minutes**
(measured: ~4 min on 2026-10-27), and it is rate-limit exposed (`429`). On a
failed day the error is logged, the day is skipped and the run continues; the
marker is still written, so that day is not retried automatically.

Run once for the year panel:

1. `BACKFILL_HR_DAYS=365` (config only, already supported in v0.1.0).
2. `docker compose run --rm garmin-sync garmin-influx-sync --backfill 365`
   (`--backfill` ignores the marker, so it also works after the marker exists).
3. Watch the logs for `Unable to backfill heart rate …` and rerun once if 429s
   appear.

A log line such as `2026-03-16: 1 heart-rate points written` means the day has
only `garmin_daily_hr`; its `heartRateValues` list was empty. Do not widen the
backfill hoping to recover those cells — the server no longer returns them.
365 requests still buy a year of daily resting/min/max values, which is why the
run is useful even though the heatmap starts in June.

Alternative rejected: fetching a year of HR through `SYNC_DAYS=365` — that
would re-download a year on every cycle, every 6 hours.

## 7. Panel performance

| Scenario | Read per refresh | Verdict |
| --- | --- | --- |
| Query 147 days of samples + `aggregateWindow(1h)` | ≈ 105 k raw points, ≈ 3.5 k cells | fine at the dashboard's 5 min refresh |
| InfluxDB task writing `garmin_heart_rate_hourly` | 3 528 points | unnecessary at this volume |

Query-time aggregation is enough. Materialising would only complicate widening
the window later, because the task range would have to be recomputed.

## 8. Pitfalls to check

1. **API retention** — empty months before 2026-06-01 are not missing watch
   data. Verify a date with the raw endpoint: `heartRateValues` is `[]` while
   `restingHeartRate` still exists. Widening the backfill cannot recover them.
2. **DST days** — 2026-10-25 (25 h) is inside the sample window; 2026-03-29 is
   not. The matrix must not show 24 phantom/filled hours on the 25-hour day.
3. **Watch-off gaps** — do not colour empty cells as 0 bpm; leave them
   transparent and mention it in the legend.
4. **UTC leakage** — a panel built on raw UTC hours looks plausible and is
   wrong by the offset; verify one known sample against Garmin Connect.
5. **`bpm` 0 rows** — Garmin sometimes reports `null`/`0`; already filtered
   upstream, but the panel must not interpolate.
6. **Time range drift** — the panel follows the dashboard's rolling `-365d`
   range; for a fixed calendar year, pin the range in the dashboard.
7. **Single series** — no tags on `garmin_heart_rate`, so no `group()` needed;
   keep it that way (one account per bucket).

## 9. Effort and acceptance

| Step | Status |
| --- | --- |
| Set `BACKFILL_HR_DAYS=365`, run one `--backfill` | done on 2026-10-27 (~4 min, 147 days of samples) |
| ECharts plugin install + heatmap option + calendar transform | done (Business Charts 7.2.5, panel 23) |
| Year axis, empty cells, local hours | done |
| Validation (retention boundary, gaps, one sample cross-check) | done |

Acceptance criteria for “panel reproduced”:

- The year axis is visible (2025-10-27 → 2026-10-27), with one cell per local
  hour where the API returns samples; cells before 2026-06-01 are empty.
- Colour is continuous over bpm; empty cells are transparent.
- Local hour labels are correct on 2026-10-25 (25-hour DST day).
- The panel renders in < 2 s at the dashboard's 5 min refresh.
- One hourly mean was spot-checked: 2026-10-26 12:00 Paris = 30 raw samples,
  mean **74.5 bpm** in Garmin and **74.5 bpm** in InfluxDB.

## 10. Decisions taken

1. **Rolling `-365d`** — the panel follows the dashboard's rolling range. A
   fixed calendar year would hide the fact that the source has no samples
   before June.
2. **ECharts** — the Business Charts plugin is installed; the panel is faithful
   to the reference. The state-timeline fallback is documented but unused.
3. **Backfill once, marker-guarded** — a periodic re-scan would spend requests
   on dates the API no longer serves; the marker stays.
