# hermes-health

Automated Google Health / Fitbit data pipeline for Hermes Agent.

Pulls your wearable data (heart rate, HRV, sleep, steps, exercise) from the
Google Health API v4 daily, stores it locally in SQLite, and surfaces trends
in Telegram via cron.

## Setup

### 1. Google Cloud project

Create a project at https://console.cloud.google.com/ → enable **Google Health API**
(APIs & Services → Library) → configure OAuth consent screen (External, add scopes
`googlehealth.activity_and_fitness.readonly`, `googlehealth.health_metrics_and_measurements.readonly`,
`googlehealth.sleep.readonly`, add your Gmail as test user) → create **Desktop app** OAuth
client. Copy the Client ID and Client Secret.

### 2. Store credentials in macOS keychain

```bash
security add-generic-password -s health-google-client-id     -a hermes -w "YOUR_CLIENT_ID" -U
security add-generic-password -s health-google-client-secret -a hermes -w "GOCSPX-..."     -U
```

### 3. One-time OAuth consent

```bash
health-pull --auth
```

Open the printed URL, approve the scopes, paste the authorization code back.

### 4. Daily cron

`crontab -e` → add:

```
0 23 * * * /Users/jay/.local/bin/health-pull && /Users/jay/.local/bin/health-summarize >> ~/health-data/logs/summarize.log 2>&1
```

7am HKT = 23:00 UTC the previous day. Runs after the daily sync window.

## Usage

```bash
health-pull                    # pull yesterday
health-pull --days 7          # backfill last 7 days
health-pull --verbose         # see what''s happening
health-pull --auth            # one-time OAuth consent
```

## Data layout

Data lands in `~/health-data/`:

```
db/health.db         SQLite — daily_metrics, heart_rate_intraday, exercise
raw/*.json           raw API responses per pull
logs/pull.log        pull history
scripts/             this codebase
```

## What gets tracked

- Daily aggregate: steps, distance, calories, RHR, HRV (RMSSD), sleep stages (deep/REM/light/awake), SpO2, breathing rate
- Intraday heart rate (1-min)
- Exercise sessions (type, duration, HR, calories)

## Privacy

- Credentials live in macOS keychain — never on disk.
- Data lives in `~/health-data/` on your machine — never uploaded.
- Refresh token rotated by Google — you''ll see a re-consent prompt every ~6 months.

## Sources

- [Google Health API v4 docs](https://developers.google.com/health)
- [Migration from Fitbit Web API](https://developers.google.com/health/migration)
- [HRV-guided training research](https://pmc.ncbi.nlm.nih.gov/articles/PMC12787763/)
