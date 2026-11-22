"""Hourly Tuya water meter logger for Raspberry Pi, compatible with Python 3.7."""
import argparse
import csv
import stat
import time
import tempfile
from datetime import datetime, timezone
from decimal import Decimal
import getpass
import json
import os
from pathlib import Path
import sys

from check_tuya_meter import TuyaAPI

ROOT = Path(__file__).resolve().parent
CONFIG = ROOT / 'water_meter_config.json'
CSV_PATH = ROOT / 'water_readings.csv'
ERROR_PATH = ROOT / 'water_meter_errors.log'
FIELDS = ['polled_at', 'water_m3', 'raw_reading', 'meter_id', 'fault', 'battery_voltage_v']
METERS = [('house', 'bfec26e9610452f97erqyg'),
          ('garden', 'bf988860ca07292df4ofod')]


def set_cyprus_timezone():
    if not Path('/usr/share/zoneinfo/Europe/Nicosia').is_file():
        raise RuntimeError('Cyprus timezone data missing. Install the tzdata package.')
    os.environ['TZ'] = 'Europe/Nicosia'
    time.tzset()


def load_config():
    info = CONFIG.stat()
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
        raise RuntimeError('Credential file must belong to this account and have mode 600. Run chmod 600 water_meter_config.json.')
    return json.loads(CONFIG.read_text(encoding='utf-8'))


def setup():
    print('Water meter setup — credentials are saved in a file readable only by your Pi account.')
    access_id = getpass.getpass('Tuya Access ID (hidden): ').strip()
    secret = getpass.getpass('Tuya Access Secret (hidden): ').strip()
    for label, value in [('Access ID', access_id), ('Access Secret', secret)]:
        if not value or not value.isascii() or any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in value):
            raise ValueError(f'{label} is empty or contains unexpected characters.')
    config = {'endpoint': 'https://openapi.tuyaeu.com',
              'access_id': access_id, 'access_secret': secret}
    descriptor, temporary = tempfile.mkstemp(prefix='.meter-config-', dir=str(ROOT))
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
            json.dump(config, stream, indent=2)
        os.replace(temporary, str(CONFIG))
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    print('Saved restricted configuration. Checking a live reading...')
    return read_once()


def make_row(status, specification, now=None):
    values = {item['code']: item['value'] for item in status}
    definitions = {item['code']: item for item in specification.get('status', [])}
    field = definitions.get('water_use_data')
    if not field:
        raise ValueError('water_use_data is absent from this meter specification. Check its saved details JSON; no CSV row written.')
    attributes = json.loads(field['values'])
    scale = attributes.get('scale')
    unit = str(attributes.get('unit', '')).lower().replace('³', '3').strip()
    if type(scale) is not int or not 0 <= scale <= 9 or unit not in ('m3', 'l', 'liter', 'litre'):
        raise ValueError('Unrecognised water unit or decimal scale; no CSV row written.')
    raw = values.get(field['code'])
    if type(raw) is not int or raw < 0:
        raise ValueError('Meter returned an invalid consumption value; no CSV row written.')
    water = Decimal(raw).scaleb(-scale)
    if unit != 'm3':
        water /= Decimal(1000)
    voltage = values.get('voltage_current')
    voltage_definition = definitions.get('voltage_current', {})
    voltage_attributes = json.loads(voltage_definition.get('values', '{}'))
    volts = ''
    if type(voltage) is int and voltage_attributes.get('unit') == 'V' and type(voltage_attributes.get('scale')) is int:
        volts = format(Decimal(voltage).scaleb(-voltage_attributes['scale']), 'f')
    return {
        'polled_at': (now or datetime.now().astimezone()).strftime('%Y-%m-%d %H:%M:%S'),
        'water_m3': format(water, 'f'),
        'raw_reading': raw,
        'meter_id': values.get('meter_id', ''),
        'fault': values.get('fault', ''),
        'battery_voltage_v': volts,
    }


def append_row(path, row):
    # Validate an existing file before appending, and lock it against concurrent runs.
    import fcntl
    with path.open('a+', encoding='utf-8', newline='') as stream:
        stream.seek(0)
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            stream.seek(0)
            header = next(csv.reader(stream), None)
            if header is not None and header != FIELDS:
                raise ValueError('Existing CSV has different columns; no row written.')
            stream.seek(0, 2)
            writer = csv.DictWriter(stream, fieldnames=FIELDS)
            if header is None:
                writer.writeheader()
            writer.writerow(row)
            stream.flush()
        finally:
            stream.seek(0)
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def require_success(api, response, stage):
    if not isinstance(response, dict) or not response.get('success'):
        safe = {key: response.get(key) for key in ('http_status', 'code', 'msg') if key in response} if isinstance(response, dict) else {}
        raise RuntimeError(f'{stage} failed: {api.safe_text(json.dumps(safe))}')


def read_once():
    set_cyprus_timezone()
    config = load_config()
    if config['endpoint'] != 'https://openapi.tuyaeu.com':
        raise ValueError('Unexpected endpoint in configuration.')
    api = TuyaAPI(config['endpoint'], config['access_id'], config['access_secret'])
    auth = api.connect()
    require_success(api, auth, 'Authentication')
    from urllib.parse import quote
    failures = 0
    for name, device_id in METERS:
        try:
            device = quote(device_id, safe='')
            response = api.get('/v1.0/iot-03/devices/{}/status'.format(device))
            specification = api.get('/v1.2/iot-03/devices/{}/specification'.format(device))
            details = {'device_id': device_id, 'status': response, 'specification': specification}
            (ROOT / (name + '_details.json')).write_text(json.dumps(details, indent=2), encoding='utf-8')
            require_success(api, response, name + ' status')
            require_success(api, specification, name + ' specification')
            if not isinstance(response.get('result'), list) or not isinstance(specification.get('result'), dict):
                raise ValueError('Unexpected device response format; no CSV row written.')
            row = make_row(response['result'], specification['result'])
            if not row['meter_id']:
                row['meter_id'] = device_id
            destination = ROOT / (name + '_readings.csv')
            append_row(destination, row)
            print('{}: Recorded {} m3 at {}'.format(name, row['water_m3'], row['polled_at']))
            print('CSV: {}'.format(destination))
        except Exception as exc:
            failures += 1
            detail = str(exc) if isinstance(exc, (ValueError, RuntimeError)) else type(exc).__name__
            message = '{} ERROR {}: {}'.format(datetime.now(timezone.utc).isoformat(), name, api.safe_text(detail))
            print(message)
            with ERROR_PATH.open('a', encoding='utf-8') as stream:
                stream.write(message + '\n')
    return 1 if failures else 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--setup', action='store_true', help='Save restricted credentials and verify a live reading')
    args = parser.parse_args()
    os.umask(0o077)
    try:
        return setup() if args.setup else read_once()
    except Exception as exc:
        # Only our explicit validation messages are logged; external exceptions may contain secrets.
        detail = str(exc) if isinstance(exc, (ValueError, RuntimeError)) else type(exc).__name__
        message = f'{datetime.now(timezone.utc).isoformat(timespec="seconds")} ERROR {detail}'
        print(message)
        try:
            with ERROR_PATH.open('a', encoding='utf-8') as stream:
                stream.write(message + '\n')
        except OSError:
            pass
        return 1


if __name__ == '__main__':
    sys.exit(main())
