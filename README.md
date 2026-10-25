# garmin-influx-sync

Small Docker service that pulls recent Garmin Connect health metrics through
Garmin's official **Personal API** and writes them to **InfluxDB 2.x** for
Grafana. It polls recent days and can backfill up to 365 days for range-based
metrics.

## Metrics

| Measurement | Fields | Timestamp |
| --- | --- | --- |
| `garmin_hrv` | `last_night_avg_ms`, `weekly_avg_ms`, `last_night_5min_high_ms` | Local midnight of the day |
| `garmin_daily_hr` | `resting_bpm`, `min_bpm`, `max_bpm` | Local midnight of the day |
| `garmin_heart_rate` | `bpm` | Original sample timestamp (UTC) |
| `garmin_steps` | `steps`, `step_goal` | Local midnight of the day |
| `garmin_sleep` | `sleep_seconds`, `deep_seconds`, `light_seconds`, `rem_seconds`, `awake_seconds`, `sleep_score`, `bedtime_local_ms`, `wake_local_ms` | Local midnight of the wake date |
| `garmin_daily_summary` | `calories_active`, `calories_resting`, `heart_rate_resting`, `intensity_moderate`, `intensity_vigorous`, `stress_avg`, `body_battery_latest`, `spo2_avg` | Local midnight of the day |
| `garmin_activity` | `duration_seconds`, `distance_m`, `calories`, `average_hr`, `max_hr`, `steps`; tag `activity_type` | Activity start timestamp (UTC) |

Daily points are stamped at midnight in the configured `TZ`. Re-syncing the
same day overwrites the same timestamps, so the sync is idempotent.
`garmin_activity` is the only tagged measurement; the others are designed for
one Garmin account per bucket.

## Requirements

- A Garmin Connect account with a **Personal API key**:
  <https://connect.garmin.com/app/settings/personalApiToken>
- InfluxDB 2.x with an organization, a bucket and a token that can write to it.
- Docker with Compose.

## Setup

Clone the repository first:

```bash
git clone https://github.com/kolokolone/garmin-influx-sync.git
cd garmin-influx-sync
```

### 1. Create the InfluxDB bucket and token

In InfluxDB, create a bucket (for example `garmin`) and an API token with
**write** permission on that bucket. Note the organization name. Grafana will
later need a separate token with **read** permission.

### 2. Create `garmin.env` (required)

The repository intentionally does **not** contain `garmin.env`: it is a local
secret file that you create and fill in yourself. Copy the template and edit it:

```bash
cp garmin.env.example garmin.env
```

Set these two values in `garmin.env`:

```dotenv
GARMIN_PERSONAL_API_KEY=your-own-garmin-personal-api-key
INFLUX_TOKEN=your-own-influxdb-write-token
```

Then restrict permissions:

```bash
chmod 600 garmin.env
```

`garmin.env` is listed in `.gitignore` and `.dockerignore`. Never commit it,
paste it into an issue, or include it in a screenshot.

### 3. Configure the Compose service

Edit `compose.yaml` and adapt:

```yaml
environment:
  TZ: Europe/Paris
  INFLUX_URL: http://influxdb:8086
  INFLUX_ORG: homelab
  INFLUX_BUCKET: garmin
```

- `INFLUX_URL` must be reachable from the container. The default
  `http://influxdb:8086` works when InfluxDB is a service on the same Compose
  network. For a standalone compose file, use its real hostname or address.
- Uncomment `depends_on: [influxdb]` only when InfluxDB is part of the same
  Compose project.
- To add this service to an existing monitoring stack, copy the `garmin-sync`
  service into that stack's Compose file and keep the `garmin_auth` volume.
  Adjust `build: .` to the directory where this repository is cloned, or use the
  published image instead. For Dockge, clone the repository next to the stack's
  `compose.yaml` and use `build: ./garmin-influx-sync`.

### 4. Start it

```bash
docker compose up -d --build
docker logs -f garmin-sync
```

The first cycle runs immediately, then every `POLL_SECONDS` (six hours by
default). If a cycle fails, the service retries after 5 minutes instead of
waiting the full interval, so a slow InfluxDB start does not cost six hours.
Garmin data only becomes available after your watch has synced to Garmin
Connect.

## Historical backfill

Polling refreshes only the last `SYNC_DAYS` days. To populate the year-long
panels, run a one-off backfill:

```bash
docker compose run --rm garmin-sync python sync.py --backfill 365
```

This imports sleep, daily summaries, HRV, steps and activities for 365 days,
plus 30 days of heart-rate samples and daily heart rate to stay rate-limit
friendly. The command is idempotent and can take several minutes. If Garmin
answers `429`, wait a few minutes and rerun it; already-written days are
overwritten, not duplicated.

## Configuration

| Variable | Required | Default | Description |
| --- | --- | --- | --- |
| `GARMIN_PERSONAL_API_KEY` | yes | — | Garmin Connect Personal API key |
| `INFLUX_TOKEN` | yes | — | InfluxDB token with write access to the bucket |
| `INFLUX_URL` | no | `http://influxdb:8086` | InfluxDB base URL |
| `INFLUX_ORG` | no | `homelab` | InfluxDB organization |
| `INFLUX_BUCKET` | no | `garmin` | Destination bucket |
| `SYNC_DAYS` | no | `3` | Days refreshed per cycle, from 1 to 31 |
| `POLL_SECONDS` | no | `21600` | Seconds between cycles, at least 900 |
| `TZ` | no | `Europe/Paris` | IANA timezone used for daily points and dates |

## Grafana

Create an InfluxDB (Flux) data source pointing at `http://influxdb:8086` with
the organization, bucket and a **read** token, then test a query:

```flux
from(bucket: "garmin")
  |> range(start: -7d)
  |> filter(fn: (r) => r._measurement == "garmin_hrv")
  |> filter(fn: (r) => r._field == "last_night_avg_ms")
```

Other useful measurements are `garmin_daily_hr`, `garmin_heart_rate`,
`garmin_steps`, `garmin_sleep`, `garmin_daily_summary` and `garmin_activity`.

## Docker image

The workflow in `.github/workflows/docker.yml` runs the unit tests and builds a
multi-architecture image (`linux/amd64`, `linux/arm64`). On pushes to `main` and
on `v*.*.*` tags it publishes to GitHub Container Registry:

```text
ghcr.io/kolokolone/garmin-influx-sync:latest
```

Build locally instead:

```bash
docker build -t garmin-influx-sync .
```

The container runs as a non-root user (UID 1000). The Garmin bearer token is
cached in the `garmin_auth` volume mounted at
`/home/app/.config/garmin`, never inside the image.

> The workflow publishes to `ghcr.io/<owner>/<repository>`. If you fork this
> project, update the image name in `compose.yaml` or use the local build.

## Security notes

- `garmin.env` is excluded from Git and from the Docker build context.
- The Garmin API key and InfluxDB token are injected as environment variables
  via `env_file`; they are visible to anyone who can inspect the container.
  Use Docker secrets or an external secret manager if that matters in your
  threat model.
- Logs contain dates, point counts and error types/status codes; parsed metric
  values are never logged. API error messages are truncated before logging.
- The Garmin Personal API is the only data source used; there is no password
  storage or Connect scraping.

## Limitations and troubleshooting

- **Recent polling, optional backfill:** each cycle refreshes only the last
  `SYNC_DAYS` days (1..31). Use `--backfill` for up to 365 days of range
  metrics; heart-rate samples are backfilled 30 days.
- **Not everything is imported:** FIT files, food/protein, lifting sets,
  daylight, sound and wrist temperature are not available through this bridge.
- **Evolving response shapes:** missing or unsupported fields are skipped with
  a warning instead of being invented.
- **`401` / `403`:** the API key may have been revoked. The client caches its
  bearer token in the `garmin_auth` volume and normally refreshes it before
  expiry; to force a fresh token, remove that volume (for example
  `docker volume rm <compose-project>_garmin_auth`) and restart.
- **`429`:** increase `POLL_SECONDS`.
- **InfluxDB connection errors:** check `INFLUX_URL`, the Compose network and
  whether InfluxDB is running.
- **No points for a day:** the watch may not have uploaded yet; check the
  Garmin Connect app and the container logs.
- **Old root-owned volume:** if you ran an earlier prototype as root, remove the
  old `garmin_auth` volume before starting this non-root image.

## Development

```bash
python -m venv .venv
. .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python -m unittest discover -s tests -v
```

The tests exercise configuration parsing and point building without calling
Garmin or InfluxDB.

## Sources

- Garmin Personal API FAQ:
  <https://developer.garmin.com/gc-developer-program/personal/personal-faq/>
- Garmin official Python client:
  <https://github.com/garmin/connect-personal-api-python>
- InfluxDB Python client:
  <https://github.com/influxdata/influxdb-client-python>
