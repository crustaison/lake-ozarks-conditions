#!/usr/bin/env python3
"""Lake of the Ozarks daily conditions updater.
Fetches lake level, water temp, and weather, then:
  1. Writes conditions.json to the repo and pushes to GitHub
  2. Appends a row to the Google Sheet via gog CLI
"""
import json, re, subprocess, datetime, urllib.request, sys, os

REPO = os.path.expanduser('~/lake-ozarks-conditions')
SHEET_ID = '1tiNCTE2YKfpOm1ZIo3MGJyVguKzkvlxedrYTp4pYh1s'
GOG = os.path.expanduser('~/.npm-global/bin/gog')
GOG_ENV = {**os.environ, 'GOG_KEYRING_PASSWORD': 'crustaison'}

LAT, LNG = 38.0843, -92.6185  # Lake of the Ozarks center


def fetch(url, headers=None):
    req = urllib.request.Request(url, headers=headers or {'User-Agent': 'lake-ozarks-conditions/1.0'})
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.read().decode('utf-8', errors='replace')


def get_ameren_data():
    """Scrape Ameren lake reports page for level and surface water temp."""
    import re as _re
    html = fetch('https://www.ameren.com/property/lake-of-the-ozarks/reports')
    pairs = {}
    for k, v in _re.findall(r'<th[^>]*>\s*(.*?)\s*</th>\s*<td[^>]*>\s*(.*?)\s*</td>', html, _re.DOTALL):
        k = _re.sub(r'<[^>]+>', '', k).strip()
        v = _re.sub(r'<[^>]+>', '', v).strip().replace(',', '')
        if k and v:
            pairs[k] = v
    level = None
    temp = None
    for k, v in pairs.items():
        if 'Current Lake level' in k:
            try: level = float(v)
            except: pass
        if 'Surface Water Temp' in k:
            try: temp = int(v)
            except: pass
    return level, temp


def get_lake_level():
    level, _ = get_ameren_data()
    return level


def get_water_temp():
    _, temp = get_ameren_data()
    return temp


def get_osage_temp():
    """Fetch Osage River water temp below Bagnell Dam from USGS gauge 06926080."""
    url = 'https://waterservices.usgs.gov/nwis/iv/?sites=06926080&parameterCd=00010&format=json&siteStatus=active'
    data = json.loads(fetch(url))
    for ts in data['value']['timeSeries']:
        if ts['variable']['variableCode'][0]['value'] == '00010':
            vals = ts['values'][0]['value']
            if vals and vals[-1]['value'] != '-999999':
                c = float(vals[-1]['value'])
                return round(c * 9/5 + 32, 1)  # C to F
    return None


def get_weather():
    url = (
        f'https://api.open-meteo.com/v1/forecast'
        f'?latitude={LAT}&longitude={LNG}'
        f'&current=temperature_2m,relative_humidity_2m,apparent_temperature,'
        f'wind_speed_10m,wind_direction_10m,wind_gusts_10m,weather_code'
        f'&daily=temperature_2m_max,temperature_2m_min,precipitation_sum,wind_speed_10m_max'
        f'&temperature_unit=fahrenheit&wind_speed_unit=mph'
        f'&precipitation_unit=inch&timezone=America/Chicago&forecast_days=3'
    )
    d = json.loads(fetch(url))
    cur = d['current']
    daily = d['daily']

    WIND_DIR = ['N','NNE','NE','ENE','E','ESE','SE','SSE','S','SSW','SW','WSW','W','WNW','NW','NNW']
    wind_deg = cur.get('wind_direction_10m')
    wind_dir = WIND_DIR[round(wind_deg / 22.5) % 16] if wind_deg is not None else None

    forecast = []
    for i in range(min(3, len(daily['time']))):
        forecast.append({
            'date': daily['time'][i],
            'high_f': round(daily['temperature_2m_max'][i]),
            'low_f': round(daily['temperature_2m_min'][i]),
            'wind_max_mph': round(daily['wind_speed_10m_max'][i]),
            'precip_in': daily['precipitation_sum'][i],
            'weather_code': 0,
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


def main():
    now = datetime.datetime.now(datetime.timezone.utc)
    today = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=-6))).strftime('%Y-%m-%d')
    errors = []

    print('Fetching lake level...')
    level = None
    try:
        level = get_lake_level()
        print(f'  Level: {level} ft MSL')
    except Exception as e:
        errors.append(f'level: {e}')
        print(f'  ERROR: {e}')

    print('Fetching water temp...')
    water_temp = None
    try:
        water_temp = get_water_temp()
        print(f'  Water temp: {water_temp}°F')
    except Exception as e:
        errors.append(f'water_temp: {e}')
        print(f'  ERROR: {e}')

    print('Fetching Osage River temp (below dam)...')
    osage_temp = None
    try:
        osage_temp = get_osage_temp()
        print(f'  Osage temp: {osage_temp}°F')
    except Exception as e:
        errors.append(f'osage_temp: {e}')
        print(f'  ERROR: {e}')

    print('Fetching weather...')
    wx = {}
    try:
        wx = get_weather()
        print(f'  Air: {wx["air_temp_f"]}°F, Wind: {wx["wind_speed_mph"]} mph {wx["wind_dir"]}')
    except Exception as e:
        errors.append(f'weather: {e}')
        print(f'  ERROR: {e}')

    # Build conditions.json
    conditions = {
        'updated': now.strftime('%Y-%m-%dT%H:%M:%SZ'),
        'lake_level': level,
        'full_pool': 660.0,
        'below_full_pool': round(660.0 - level, 2) if level else None,
        'water_temp_f': water_temp,
        'osage_temp_f': osage_temp,
        **wx,
    }

    cond_path = os.path.join(REPO, 'conditions.json')
    with open(cond_path, 'w') as f:
        json.dump(conditions, f, indent=2)
    print('Wrote conditions.json')

    # Git commit + push
    try:
        subprocess.run(['git', 'add', 'conditions.json'], cwd=REPO, check=True)
        result = subprocess.run(
            ['git', 'diff', '--staged', '--quiet'], cwd=REPO
        )
        if result.returncode != 0:
            subprocess.run(
                ['git', 'commit', '-m', f'chore: update conditions {today}'],
                cwd=REPO, check=True
            )
            subprocess.run(['git', 'push', 'origin', 'main'], cwd=REPO, check=True)
            print('Pushed to GitHub')
        else:
            print('No changes to push')
    except Exception as e:
        print(f'Git error: {e}')

    # Append to Google Sheet
    if SHEET_ID:
        try:
            row = [
                today,
                str(level or ''),
                str(round(660.0 - level, 2) if level else ''),
                str(water_temp or ''),
                str(osage_temp or ''),
                str(wx.get('air_temp_f', '')),
                str(wx.get('feels_like_f', '')),
                str(wx.get('humidity', '')),
                str(wx.get('wind_speed_mph', '')),
                str(wx.get('wind_gusts_mph', '')),
                str(wx.get('wind_dir', '')),
                '; '.join(errors) if errors else 'OK',
            ]
            cmd = [
                GOG, '-a', 'crustaison@gmail.com',
                'sheets', 'append', SHEET_ID, 'Sheet1!A:K',
                *row
            ]
            subprocess.run(cmd, env=GOG_ENV, check=True, capture_output=True)
            print(f'Appended row to sheet {SHEET_ID}')
        except Exception as e:
            print(f'Sheet error: {e}')

    print(f'Done — {len(errors)} error(s)')
    return 0 if not errors else 1


if __name__ == '__main__':
    sys.exit(main())
