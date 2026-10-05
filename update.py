#!/usr/bin/env python3
"""Lake of the Ozarks hourly conditions updater.
Fetches lake level, water temp, dam releases and weather, then:
  1. Writes conditions.json to the repo and pushes to GitHub
  2. Appends a row to the Google Sheet via gog CLI

Flags:
  --no-push   write conditions.json but don't commit/push
  --no-sheet  skip the Google Sheet append

The gog keyring password is read from $GOG_KEYRING_PASSWORD, or from
~/.config/lake-ozarks/gog_keyring_password if the variable isn't set.
"""
import json, re, subprocess, datetime, urllib.request, sys, os, time

REPO = os.path.dirname(os.path.abspath(__file__))
SHEET_ID = '1tiNCTE2YKfpOm1ZIo3MGJyVguKzkvlxedrYTp4pYh1s'
GOG = os.path.expanduser('~/.npm-global/bin/gog')
GOG_ACCOUNT = 'crustaison@gmail.com'
GOG_PASS_FILE = os.path.expanduser('~/.config/lake-ozarks/gog_keyring_password')

LAT, LNG = 38.0843, -92.6185  # Lake of the Ozarks center
FULL_POOL = 660.0
DAM_GAUGE = '06926000'  # USGS: Osage River near Bagnell, MO (just below Bagnell Dam)
HISTORY_HOURS = 48
CENTRAL = datetime.timezone(datetime.timedelta(hours=-6))


def fetch(url, retries=3):
    """GET with retries — Ameren and USGS throw frequent 502/503s."""
    req = urllib.request.Request(url, headers={'User-Agent': 'lake-ozarks-conditions/1.0'})
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return r.read().decode('utf-8', errors='replace')
        except Exception:
            if attempt == retries - 1:
                raise
            time.sleep(5 * (attempt + 1))


def _cell(s):
    return re.sub(r'<[^>]+>', '', s).replace('&nbsp;', '').replace(',', '').strip()


def _num(s):
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


def get_ameren_data():
    """Scrape Ameren's lake report page: level, surface temp, 3-day release/level forecast."""
    html = fetch('https://www.ameren.com/property/lake-of-the-ozarks/reports')
    pairs = {}
    for k, v in re.findall(r'<th[^>]*>\s*(.*?)\s*</th>\s*<td[^>]*>\s*(.*?)\s*</td>', html, re.DOTALL):
        k, v = _cell(k), _cell(v)
        if k and v:
            pairs[k] = v
    level = temp = None
    for k, v in pairs.items():
        if 'Current Lake level' in k:
            level = _num(v)
        if 'Surface Water Temp' in k:
            t = _num(v)
            temp = int(t) if t is not None else None

    # Forecast rows: <th>label</th><td>today</td><td>tomorrow</td><td>day 3</td>
    def row(label):
        m = re.search(re.escape(label) + r'.*?</th>((?:\s*<td[^>]*>.*?</td>){3})', html, re.DOTALL)
        if not m:
            return []
        return [_num(_cell(c)) for c in re.findall(r'<td[^>]*>(.*?)</td>', m.group(1), re.DOTALL)]

    releases = row('Forecasted Releases (cfs)')
    levels = row('Forecasted Lake Level')
    forecast = [
        {'release_cfs': releases[i] if i < len(releases) else None,
         'lake_level': levels[i] if i < len(levels) else None}
        for i in range(3)
    ] if releases or levels else []
    return {'level': level, 'water_temp': temp, 'forecast': forecast}


def get_dam_release():
    """Current discharge (cfs) just below Bagnell Dam from USGS."""
    url = f'https://waterservices.usgs.gov/nwis/iv/?sites={DAM_GAUGE}&parameterCd=00060&format=json'
    data = json.loads(fetch(url))
    for ts in data['value']['timeSeries']:
        vals = ts['values'][0]['value']
        if vals and vals[-1]['value'] != '-999999':
            return round(float(vals[-1]['value']))
    return None


def get_weather():
    url = (
        f'https://api.open-meteo.com/v1/forecast'
        f'?latitude={LAT}&longitude={LNG}'
        f'&current=temperature_2m,relative_humidity_2m,apparent_temperature,'
        f'wind_speed_10m,wind_direction_10m,wind_gusts_10m,weather_code'
        f'&daily=temperature_2m_max,temperature_2m_min,precipitation_sum,'
        f'precipitation_probability_max,wind_speed_10m_max,weather_code'
        f'&temperature_unit=fahrenheit&wind_speed_unit=mph'
        f'&precipitation_unit=inch&timezone=America/Chicago&forecast_days=4'
    )
    d = json.loads(fetch(url))
    cur = d['current']
    daily = d['daily']

    WIND_DIR = ['N','NNE','NE','ENE','E','ESE','SE','SSE','S','SSW','SW','WSW','W','WNW','NW','NNW']
    wind_deg = cur.get('wind_direction_10m')
    wind_dir = WIND_DIR[round(wind_deg / 22.5) % 16] if wind_deg is not None else None

    forecast = []
    for i in range(len(daily['time'])):
        forecast.append({
            'date': daily['time'][i],
            'high_f': round(daily['temperature_2m_max'][i]),
            'low_f': round(daily['temperature_2m_min'][i]),
            'wind_max_mph': round(daily['wind_speed_10m_max'][i]),
            'precip_in': daily['precipitation_sum'][i],
            'precip_chance': daily['precipitation_probability_max'][i],
            'weather_code': daily['weather_code'][i],
        })

    return {
        'air_temp_f': round(cur['temperature_2m']),
        'feels_like_f': round(cur['apparent_temperature']),
        'humidity': cur['relative_humidity_2m'],
        'wind_speed_mph': round(cur['wind_speed_10m']),
        'wind_gusts_mph': round(cur['wind_gusts_10m']),
        'wind_dir': wind_dir,
        'wind_deg': wind_deg,
        'weather_code': cur['weather_code'],
        'forecast': forecast,
    }


def gog_env():
    env = dict(os.environ)
    if 'GOG_KEYRING_PASSWORD' not in env and os.path.exists(GOG_PASS_FILE):
        with open(GOG_PASS_FILE) as f:
            env['GOG_KEYRING_PASSWORD'] = f.read().strip()
    return env


def step(name, fn, errors):
    try:
        return fn()
    except Exception as e:
        errors.append(f'{name}: {e}')
        print(f'  ERROR ({name}): {e}')
        return None


def main():
    no_push = '--no-push' in sys.argv
    no_sheet = '--no-sheet' in sys.argv
    now = datetime.datetime.now(datetime.timezone.utc)
    local = now.astimezone(CENTRAL)
    errors = []

    cond_path = os.path.join(REPO, 'conditions.json')
    prev = {}
    if os.path.exists(cond_path):
        try:
            with open(cond_path) as f:
                prev = json.load(f)
        except Exception:
            pass

    print('Fetching Ameren lake report...')
    ameren = step('ameren', get_ameren_data, errors) or {}
    level = ameren.get('level')
    water_temp = ameren.get('water_temp')
    print(f'  Level: {level} ft MSL, water: {water_temp}°F')

    print('Fetching dam release (USGS)...')
    release = step('dam_release', get_dam_release, errors)
    print(f'  Release: {release} cfs')

    print('Fetching weather...')
    wx = step('weather', get_weather, errors)
    if wx:
        print(f'  Air: {wx["air_temp_f"]}°F, Wind: {wx["wind_speed_mph"]} mph {wx["wind_dir"]}')
    else:
        # Fall back to the last good weather rather than blanking the page
        keys = ('air_temp_f','feels_like_f','humidity','wind_speed_mph','wind_gusts_mph',
                'wind_dir','wind_deg','weather_code','forecast')
        wx = {k: prev[k] for k in keys if k in prev}
        if wx:
            wx['weather_updated'] = prev.get('weather_updated', prev.get('updated'))
            print('  Using cached weather data')

    stamp = now.strftime('%Y-%m-%dT%H:%M:%SZ')

    # Rolling hourly level history for the 24h trend on the page
    history = [h for h in prev.get('level_history', []) if h.get('level') is not None]
    if level is not None:
        history.append({'t': stamp, 'level': level})
    history = history[-HISTORY_HOURS:]

    conditions = {
        'updated': stamp,
        'lake_level': level,
        'full_pool': FULL_POOL,
        'below_full_pool': round(FULL_POOL - level, 2) if level is not None else None,
        'water_temp_f': water_temp,
        'dam_release_cfs': release,
        'ameren_forecast': ameren.get('forecast', []),
        'level_history': history,
        'errors': errors,
        'weather_updated': stamp,
        **wx,
    }

    with open(cond_path, 'w') as f:
        json.dump(conditions, f, indent=2)
    print('Wrote conditions.json')

    if not no_push:
        try:
            subprocess.run(['git', 'add', 'conditions.json'], cwd=REPO, check=True)
            staged = subprocess.run(['git', 'diff', '--staged', '--quiet'], cwd=REPO)
            if staged.returncode != 0:
                subprocess.run(['git', 'commit', '-q', '-m',
                                f'chore: update conditions {local:%Y-%m-%d %H:%M}'],
                               cwd=REPO, check=True)
                # Rebase first so a remote change doesn't reject the push
                subprocess.run(['git', 'pull', '-q', '--rebase', '--autostash', 'origin', 'main'],
                               cwd=REPO, check=True)
                subprocess.run(['git', 'push', '-q', 'origin', 'main'], cwd=REPO, check=True)
                print('Pushed to GitHub')
            else:
                print('No changes to push')
        except Exception as e:
            print(f'Git error: {e}')

    if SHEET_ID and not no_sheet:
        try:
            row = [
                f'{local:%Y-%m-%d %H:%M}',
                str(level if level is not None else ''),
                str(conditions['below_full_pool'] if level is not None else ''),
                str(water_temp if water_temp is not None else ''),
                '',  # was Osage temp (gauge offline); kept so columns stay aligned
                str(wx.get('air_temp_f', '')),
                str(wx.get('feels_like_f', '')),
                str(wx.get('humidity', '')),
                str(wx.get('wind_speed_mph', '')),
                str(wx.get('wind_gusts_mph', '')),
                str(wx.get('wind_dir', '')),
                '; '.join(errors) if errors else 'OK',
                str(release if release is not None else ''),
            ]
            cmd = [GOG, '-a', GOG_ACCOUNT, 'sheets', 'append', SHEET_ID, 'Sheet1!A:M', *row]
            subprocess.run(cmd, env=gog_env(), check=True, capture_output=True)
            print(f'Appended row to sheet {SHEET_ID}')
        except Exception as e:
            print(f'Sheet error: {e}')

    print(f'Done — {len(errors)} error(s)')
    return 0 if not errors else 1


if __name__ == '__main__':
    sys.exit(main())
