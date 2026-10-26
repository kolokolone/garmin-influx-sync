# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.0] - 2026-10-26

First minor release: automatic history download, configurable refresh interval,
and a documented layout, endpoint inventory and dashboard audit.

### Added

- `BACKFILL` (`auto` | `on` | `off`, default `auto`): a fresh, empty bucket
  downloads its history by itself; a bucket that already holds Garmin data is
  left alone. Decisions are logged with their reason.
- Backfill marker `garmin_sync_state` (`backfill_completed`, `backfill_days`) so
  restarting the service, or enabling backfill twice, never re-downloads a year.
- `BACKFILL_DAYS` (default 365) to size the history window.
- `BACKFILL_HR_DAYS` (default 30) to size the heart-rate sample window, which is
  the expensive part (one request per day).
- `REFRESH_INTERVAL` accepting human durations (`15m`, `6h`, `12h`, `1d`) as well
  as plain seconds; minimum 15 minutes.
- `--version` and a `garmin-influx-sync` console script.
- `docs/architecture.md`: layout, runtime flow, first-sync decision table,
  storage schema, failure model and configuration.
- `docs/garmin-endpoints.md`: all 24 Personal API endpoints with windows,
  ingestion status and download capability.
- `docs/dashboard-heart-rate-heatmap.md`: audit of the year-long heart-rate
  heatmap panel — data gap, rendering options, Flux query, backfill cost and
  pitfalls.

### Changed

- **Breaking:** the source tree is now a package under `src/` run through
  `pyproject.toml`; `sync.py` at the repository root is gone. Use
  `python -m cli` or the `garmin-influx-sync` console script instead of
  `python sync.py`.
- **Breaking:** `POLL_SECONDS` is replaced by `REFRESH_INTERVAL`. Update
  `compose.yaml`/your env file (`POLL_SECONDS: "21600"` → `REFRESH_INTERVAL: 6h`).
- **Breaking:** `requirements.txt` is replaced by dependencies declared in
  `pyproject.toml`.
- Per-dataset heart-rate day loops are shared between backfill and refresh, so
  both report failed days identically.
- The container entrypoint is the console script rather than `python sync.py`.

### Fixed

- A backfill that finishes with missing heart-rate days now says so explicitly
  and points at the command that fills the gaps, instead of silently writing the
  completion marker.

## [0.0.1] - 2026-10-25

Initial release.

### Added

- Garmin Connect Personal API sync into InfluxDB 2.x: heart-rate samples, daily
  heart rate, HRV, steps, sleep, daily summaries and activities.
- Polling of the last `SYNC_DAYS` days every `POLL_SECONDS`, with a 5-minute
  retry after a failed cycle.
- One-off backfill of up to 365 days for range metrics plus 30 days of heart
  rate (`python sync.py --backfill DAYS`).
- Bearer-token caching in the `garmin_auth` volume, non-root container image,
  multi-architecture CI publish to GHCR and unit tests.

[0.1.0]: https://github.com/kolokolone/garmin-influx-sync/releases/tag/v0.1.0
[0.0.1]: https://github.com/kolokolone/garmin-influx-sync/releases/tag/v0.0.1
