# Panel audit — “Heart rate by hour of day, every day for a year”

Reference: `docs/dashboard-grafana_a_reproduire.jpg`, panel *The year → Heart
rate by hour of day, every day for a year*.

```text
        x = calendar date (≈365 columns, Nov 2025 → Oct 2026)
        y = hour of day (12am … 11pm, 24 rows)
   colour = heart rate in bpm (continuous, magenta → yellow)
     gaps = no watch on the wrist, drawn as empty cells
```

Status: **not reproducible today**, for one reason only — the history window.
Everything else is a Grafana/plugin problem, not a data problem.

## 1. What the bridge stores today

| Fact | Where | Value |
| --- | --- | --- |
| Raw samples per day | `src/influx/writer.py` → `sync_heart_rate_day` | `heartRateValues` from the daily payload, ~2-min cadence (≈720 samples) |
| Measurement | `src/garmin/points.py` → `heart_rate_points` | `garmin_heart_rate`, field `bpm`, timestamp = **UTC** sample time |
| Filtering | same | samples outside `20..250` bpm or outside 2000–2100 are dropped |
| Daily aggregate | same file | `garmin_daily_hr` (`resting_bpm`, `min_bpm`, `max_bpm`) at local midnight |
| Refresh depth | `SYNC_DAYS` (default 3) | enough to keep the right edge fresh |
| Backfill depth for samples | `BACKFILL_HR_DAYS` (default 30) in `src/config.py` | **one HTTP request per day**, hence the cap |
| Backfill depth for range datasets | `BACKFILL_DAYS` (default 365) | HRV, steps, sleep, summaries, activities |
| Backfill is once | `src/service/backfill.py` → `garmin_sync_state.backfill_completed` | marker; `--backfill` forces a re-run |

So the panel currently has **30 days of samples out of 365**: one twelfth of the
picture.

## 2. Volume check

| Granularity | Points per year | Comment |
| --- | --- | --- |
| Raw samples (2 min) | ≈ 263 000 | what exists after a 365-day backfill |
| Raw samples (1 min, some watches) | ≈ 525 000 | still trivial for InfluxDB |
| Hourly means | 8 760 | what the panel should actually read |
| Matrix shown | 365 × 24 = 8 760 cells | same number, coincidence |

Memory and storage are not the constraint. **Garmin request budget** is.

## 3. Gap analysis

| Requirement | Have it? | Action |
| --- | --- | --- |
| 365 days of HR samples | **no** (30) | `BACKFILL_HR_DAYS=365` + one `--backfill 365` run |
| Local hour of day | data yes, conversion no | compute in Flux/panel (`Europe/Paris`), never UTC |
| Per-cell value | yes (mean of the hour) | query-time `aggregateWindow(every: 1h, fn: mean)` |
| Empty-cell semantics | yes (absent samples) | `createEmpty: false` + transparent "no data" colour |
| Continuous colour = bpm | no native Grafana panel | ECharts plugin, or banded fallback |
| Stable query cost at 30 s refresh | conditional | pre-aggregate hourly if p95 > 2 s |

Nothing new needs to be parsed from Garmin: the raw samples already contain the
information. The panel is a **window + rendering** problem.

## 4. Rendering options

| Option | Colour encodes bpm? | Extra plugin | Verdict |
| --- | --- | --- | --- |
| **ECharts heatmap** (volkovlabs `echarts-panel`) | yes, continuous `visualMap` | yes | **recommended** — only faithful reproduction |
| Native **Heatmap** panel + `histogram()` | no: colour = density of samples per bpm bucket, every cell count ≈ 1 | no | wrong visual, dismiss |
| Native **State timeline** + 24 pivoted hour fields + bpm thresholds | banded (5–6 colours) | no | good fallback, still readable |
| 24 trend series (one per hour) | n/a | no | spaghetti, dismiss |
| Canvas/HTML custom panel | yes | yes | overkill once ECharts exists |

Decision: **ECharts heatmap** for parity; **state timeline** documented as the
no-plugin path so the dashboard still works on a bare Grafana.

## 5. Query

Hourly means, local-aligned windows, one row per (hour, instant):

```flux
import "date"
import "timezone"

option location = timezone.location(name: "Europe/Paris")

from(bucket: "garmin")
  |> range(start: -365d)
  |> filter(fn: (r) => r._measurement == "garmin_heart_rate" and r._field == "bpm")
  |> aggregateWindow(every: 1h, fn: mean, createEmpty: false)
  |> map(fn: (r) => ({r with hour: date.hour(t: r._time, location: {zone: "Europe/Paris"})}))
  |> keep(columns: ["_time", "_value", "hour"])
```

- `option location` shifts hourly window boundaries to local hours, so a cell is
  “07:00–08:00 Paris”, not “07:00–08:00 UTC”.
- `date.hour(..., location:)` keeps the label correct across DST. Without it the
  whole matrix slides by 1 h (winter) or 2 h (summer).
- The date key can stay `_time` (UTC instant of the local hour) and be
  formatted in the panel; that avoids relying on `date.truncate` semantics.
- **Validate this query against the deployed InfluxDB version** before building
  the panel: `timezone` requires Flux ≥ 0.179 (InfluxDB 2.7+).

ECharts needs `[xIndex, yIndex, bpm]` triples. Build them in the panel's custom
code from the query result: `x` = unique local dates, `y` = `hour`, and map
`_time` to a local date with `toLocaleDateString('fr-FR', {timeZone: 'Europe/Paris'})`.

State-timeline fallback: pivot the 24 hourly columns, then apply bpm thresholds:

```flux
  |> pivot(rowKey: ["_time"], columnKey: ["hour"], valueColumn: "_value")
```

## 6. Cost of the backfill (the real work)

365 days × 1 request/day at 0.4 s pacing plus latency ≈ **4 to 6 minutes**, and
it is rate-limit exposed (`429`). Current behaviour on a failed day: logged,
skipped, and the run continues; the marker is still written, so those days are
not retried automatically.

Plan:

1. `BACKFILL_HR_DAYS=365` (config only, already supported in v0.1.0).
2. Run once: `docker compose run --rm garmin-sync garmin-influx-sync --backfill 365`
   (`--backfill` ignores the marker, so it also works after the marker exists).
3. Watch the logs for `Unable to backfill heart rate …` and rerun once if 429s
   appear.

Alternative rejected: fetching a year of HR through `SYNC_DAYS=365` — that
would re-download a year on every cycle, every 6 hours.

## 7. Panel performance

| Scenario | Read per refresh | Verdict |
| --- | --- | --- |
| Query raw samples + `aggregateWindow(1h)` | ≈ 263 k points | fine at 1 refresh/min, measure at 30 s |
| InfluxDB task writing `garmin_heart_rate_hourly` | 8 760 points | recommended if p95 > 2 s |

If materialising, remember that widening the backfill later requires recomputing
the task range (`option task` with `offset`, or a one-off backfill query), which
is why query-time aggregation is the default in this audit.

## 8. Pitfalls to check

1. **DST days** — 2026-03-29 (23 h) and 2026-10-25 (25 h): the matrix must not
   show 24 phantom/filled hours. Check both explicitly.
2. **Watch-off gaps** — do not colour empty cells as 0 bpm; leave them
   transparent and mention it in the legend.
3. **UTC leakage** — a panel built on raw UTC hours looks plausible and is
   wrong by the offset; verify one known sample against Garmin Connect.
4. **`bpm` 0 rows** — Garmin sometimes reports `null`/`0`; already filtered
   upstream, but the panel must not interpolate.
5. **Time range drift** — “-365d” rolls; for a fixed “year” view pin the range
   in the panel instead.
6. **Single series** — no tags on `garmin_heart_rate`, so no `group()` needed;
   keep it that way (one account per bucket).

## 9. Effort and acceptance

| Step | Effort |
| --- | --- |
| Set `BACKFILL_HR_DAYS=365`, run one `--backfill` | minutes (wall clock), 5 lines of config |
| ECharts plugin install + heatmap option + transform | 2–4 h, iterating in a panel |
| State-timeline fallback | 1 h |
| Validation (DST, gaps, one sample cross-check) | 30 min |

Acceptance criteria for “panel reproduced”:

- 365 days × 24 hours visible, one cell per (local day, local hour).
- Colour continuous over bpm; empty cells transparent.
- Local hour labels correct on both DST dates of the year.
- Panel renders in < 2 s at the dashboard refresh interval.
- Values spot-checked against Garmin Connect for three random hours.

## 10. Open decisions

1. **365 vs. fixed calendar year** — rolling `-365d` or `2026-01-01 → today`?
   The reference screenshot groups by month, which suggests fixed calendar
   boundaries.
2. **ECharts dependency** — accept a third-party panel plugin on the Grafana
   instance, or stick to the banded native fallback?
3. **Backfill window policy** — keep the “once, marker-guarded” rule (cheap,
   explicit re-run needed) or add a periodic re-scan of gaps (more requests,
   self-healing)?
