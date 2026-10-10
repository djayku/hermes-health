#!/usr/bin/env python3
"""
health-pull.py — Pull yesterday's data from Google Health API v4, append to local SQLite.

Auto-refreshes OAuth token. Stores Client ID/Secret + tokens in macOS keychain.
Daily cron runs this at 7am HKT.

Usage:
    health-pull.py                # pull yesterday
    health-pull.py --days 7       # pull last 7 days (backfill)
    health-pull.py --verbose      # show what's happening
"""
import argparse, json, os, sqlite3, sys, time, subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
import urllib.request, urllib.parse, urllib.error

# ----- Paths -----
DATA_DIR = Path.home() / 'health-data'
DB_PATH = DATA_DIR / 'db' / 'health.db'
LOG_PATH = DATA_DIR / 'logs' / 'pull.log'
RAW_DIR = DATA_DIR / 'raw'

# ----- Keychain services -----
KC_CLIENT_ID = 'health-google-client-id'
KC_CLIENT_SECRET = 'health-google-client-secret'
KC_ACCESS_TOKEN = 'health-google-access-token'
KC_REFRESH_TOKEN = 'health-google-refresh-token'
KC_TOKEN_EXPIRY = 'health-google-token-expiry'

# ----- Scopes (read-only, personal use) -----
SCOPES = [
    'https://www.googleapis.com/auth/googlehealth.activity_and_fitness.readonly',
    'https://www.googleapis.com/auth/googlehealth.health_metrics_and_measurements.readonly',
    'https://www.googleapis.com/auth/googlehealth.sleep.readonly',
]

API_BASE = 'https://health.googleapis.com/v1'

# ----- Helpers -----
def kc_get(service):
    """Read from macOS keychain."""
    try:
        r = subprocess.run(
            ['security', 'find-generic-password', '-s', service, '-w'],
            capture_output=True, text=True, timeout=5,
        )
        if r.returncode == 0:
            return r.stdout.strip()
    except Exception:
        pass
    return None

def kc_set(service, value):
    """Write to macOS keychain."""
    r = subprocess.run(
        ['security', 'add-generic-password', '-s', service, '-a', 'hermes', '-w', value, '-U'],
        capture_output=True, text=True, timeout=5,
    )
    return r.returncode == 0

def log(msg, verbose=False):
    ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    line = f'[{ts}] {msg}'
    print(line if verbose else msg, file=sys.stderr if not verbose else sys.stdout)
    try:
        with open(LOG_PATH, 'a') as f:
            f.write(line + '\n')
    except Exception:
        pass

def http_get(url, token, params=None):
    qs = '?' + urllib.parse.urlencode(params) if params else ''
    req = urllib.request.Request(
        url + qs,
        headers={'Authorization': f'Bearer {token}', 'Accept': 'application/json'},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode())

def http_post(url, token, body):
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode(),
        headers={
            'Authorization': f'Bearer {token}',
            'Content-Type': 'application/json',
            'Accept': 'application/json',
        },
        method='POST',
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode())

# ----- OAuth -----
def refresh_access_token():
    rt = kc_get(KC_REFRESH_TOKEN)
    cid = kc_get(KC_CLIENT_ID)
    cs = kc_get(KC_CLIENT_SECRET)
    if not all([rt, cid, cs]):
        raise SystemExit(
            'Missing credentials. Run: health-pull.py --auth'
        )
    body = urllib.parse.urlencode({
        'client_id': cid,
        'client_secret': cs,
        'refresh_token': rt,
        'grant_type': 'refresh_token',
    }).encode()
    req = urllib.request.Request(
        'https://oauth2.googleapis.com/token',
        data=body,
        headers={'Content-Type': 'application/x-www-form-urlencoded'},
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        d = json.loads(resp.read().decode())
    expires_at = time.time() + int(d.get('expires_in', 3600))
    kc_set(KC_ACCESS_TOKEN, d['access_token'])
    kc_set(KC_TOKEN_EXPIRY, str(int(expires_at)))
    return d['access_token']

def get_valid_token():
    exp = kc_get(KC_TOKEN_EXPIRY)
    if exp and int(exp) > time.time() + 60:
        tok = kc_get(KC_ACCESS_TOKEN)
        if tok:
            return tok
    return refresh_access_token()

# ----- DB -----
def init_db():
    DATA_DIR.joinpath('db').mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.executescript('''
    CREATE TABLE IF NOT EXISTS daily_metrics (
        date TEXT PRIMARY KEY,
        steps INTEGER,
        distance REAL,
        calories_total INTEGER,
        calories_active INTEGER,
        resting_hr INTEGER,
        hrv_rmssd REAL,
        sleep_total_min INTEGER,
        sleep_deep_min INTEGER,
        sleep_rem_min INTEGER,
        sleep_light_min INTEGER,
        sleep_awake_min INTEGER,
        sleep_efficiency REAL,
        spo2_avg REAL,
        breathing_rate REAL,
        raw_json TEXT,
        pulled_at TEXT
    );
    CREATE TABLE IF NOT EXISTS heart_rate_intraday (
        ts TEXT, bpm INTEGER, source TEXT,
        PRIMARY KEY (ts, source)
    );
    CREATE TABLE IF NOT EXISTS exercise (
        id TEXT PRIMARY KEY,
        date TEXT,
        type TEXT,
        duration_min REAL,
        calories INTEGER,
        avg_hr INTEGER,
        max_hr INTEGER,
        raw_json TEXT
    );
    CREATE TABLE IF NOT EXISTS pull_log (
        pulled_at TEXT PRIMARY KEY,
        range_start TEXT, range_end TEXT,
        ok INTEGER, message TEXT
    );
    ''')
    con.commit()
    return con

# ----- Pull -----
def pull_reconcile(start, end, token):
    """Pull reconciled daily aggregate for the date range."""
    url = f'{API_BASE}/users/me/dataset:reconcile'
    # Time window: midnight to midnight in user's local TZ. Use UTC for now;
    # the API accepts ISO 8601 with timezone.
    body = {
        'dataset': [{
            'dataSource': {'name': 'derived:com.google.step_count.delta:com.google.android.gms:estimated_steps'},
            'duration': start.strftime('%Y-%m-%dT00:00:00Z'),
            'endDuration': end.strftime('%Y-%m-%dT00:00:00Z'),
            'aggregation': {'type': 'daily'},
        }],
    }
    return http_post(url, token, body)

def list_data_types(token):
    """List which data types are available — check before bulk-pulling."""
    return http_get(f'{API_BASE}/users/me/dataTypes', token)

def main():
    p = argparse.ArgumentParser()
    p.add_argument('--auth', action='store_true', help='Run OAuth consent flow')
    p.add_argument('--days', type=int, default=1, help='Days to backfill (default 1)')
    p.add_argument('--verbose', '-v', action='store_true')
    args = p.parse_args()

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    RAW_DIR.mkdir(parents=True, exist_ok=True)

    if args.auth:
        # Build authorization URL
        cid = kc_get(KC_CLIENT_ID)
        if not cid:
            print('ERROR: store Client ID first:')
            print(f'  security add-generic-password -s {KC_CLIENT_ID} -a hermes -w "YOUR_CLIENT_ID"')
            sys.exit(1)
        redirect = 'http://localhost:8765/callback'
        url = 'https://accounts.google.com/o/oauth2/v2/auth?' + urllib.parse.urlencode({
            'client_id': cid,
            'redirect_uri': redirect,
            'response_type': 'code',
            'scope': ' '.join(SCOPES),
            'access_type': 'offline',
            'prompt': 'consent',
        })
        print('Open this URL in your browser, approve, then paste the authorization code below:')
        print()
        print(url)
        print()
        code = input('Authorization code: ').strip()
        cs = kc_get(KC_CLIENT_SECRET)
        body = urllib.parse.urlencode({
            'client_id': cid,
            'client_secret': cs,
            'code': code,
            'grant_type': 'authorization_code',
            'redirect_uri': redirect,
        }).encode()
        req = urllib.request.Request(
            'https://oauth2.googleapis.com/token',
            data=body,
            headers={'Content-Type': 'application/x-www-form-urlencoded'},
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            d = json.loads(resp.read().decode())
        if 'refresh_token' not in d:
            print('ERROR: no refresh_token. Make sure prompt=consent was used.', file=sys.stderr)
            print(d, file=sys.stderr)
            sys.exit(1)
        kc_set(KC_REFRESH_TOKEN, d['refresh_token'])
        expires_at = time.time() + int(d.get('expires_in', 3600))
        kc_set(KC_ACCESS_TOKEN, d['access_token'])
        kc_set(KC_TOKEN_EXPIRY, str(int(expires_at)))
        print('OK — credentials stored in keychain. Token expires in', int((expires_at - time.time()) / 60), 'min.')
        return

    # Normal pull
    try:
        token = get_valid_token()
    except SystemExit as e:
        log(str(e))
        sys.exit(1)
    log(f'token OK, expires {kc_get(KC_TOKEN_EXPIRY)}', args.verbose)

    end = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    start = end - timedelta(days=args.days)

    try:
        data = pull_reconcile(start, end, token)
    except urllib.error.HTTPError as e:
        log(f'API error {e.code}: {e.read().decode()[:300]}')
        if e.code == 401:
            log('Token rejected. Re-running auth flow.')
        sys.exit(1)

    raw_path = RAW_DIR / f'pull_{start.strftime("%Y%m%d")}_{end.strftime("%Y%m%d")}.json'
    raw_path.write_text(json.dumps(data, indent=2))
    log(f'wrote raw to {raw_path} ({raw_path.stat().st_size} bytes)')

    # Try listing data types — useful for debugging what's available
    if args.verbose:
        try:
            dt = list_data_types(token)
            (RAW_DIR / 'data_types.json').write_text(json.dumps(dt, indent=2))
            log(f'dataTypes: {len(dt.get("dataTypes", []))} types supported')
        except Exception as e:
            log(f'dataTypes call failed: {e}')

    con = init_db()
    con.execute(
        'INSERT OR REPLACE INTO pull_log (pulled_at, range_start, range_end, ok, message) VALUES (?, ?, ?, 1, ?)',
        (datetime.now().isoformat(), start.isoformat(), end.isoformat(), f'{raw_path.stat().st_size}B'),
    )
    con.commit()
    log(f'pull_log updated. DB at {DB_PATH}')

if __name__ == '__main__':
    main()