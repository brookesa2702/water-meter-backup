"""Conditional daily alarm email. Raspberry Pi / Python 3.7."""
import argparse
import csv
from datetime import datetime, timedelta
from email.message import EmailMessage
from email.utils import parseaddr
import fcntl
import getpass
import json
import os
from pathlib import Path
import smtplib
import ssl
import stat
import tempfile
import time

ROOT = Path('/home/pi/water-meter')
CONFIG = ROOT / 'alarm_email_config.json'
STATE = ROOT / 'alarm_email_state.json'
RECIPIENTS = ['brookesa@yahoo.com', 'john.moore@secomak.com']
CHANGEOVER = '2026-11-18 12:50:00'  # Cyprus local time
METERS = [('Relocated meter', 'house_readings.csv'), ('Garden', 'garden_readings.csv')]
FLAGS = [(64, 'Abnormal water use'), (2048, 'Water overuse'), (4096, 'Cover alarm')]


def save_json(path, value):
    fd, temporary = tempfile.mkstemp(prefix='.alarm-email-', dir=str(path.parent))
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as output:
            json.dump(value, output, indent=2)
        os.replace(temporary, str(path))
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def connect(config):
    context = ssl.create_default_context()
    if config['port'] == 465:
        smtp = smtplib.SMTP_SSL(config['host'], 465, timeout=30, context=context)
    else:
        smtp = smtplib.SMTP(config['host'], 587, timeout=30)
        try:
            smtp.ehlo()
            smtp.starttls(context=context)
            smtp.ehlo()
        except Exception:
            smtp.close()
            raise
    try:
        smtp.login(config['username'], config['password'])
    except Exception:
        smtp.close()
        raise
    return smtp


def setup():
    print('Configure sending account locally. No email will be sent during setup.')
    host = input('SMTP server: ').strip()
    port = int(input('SMTP port [587; or 465 for implicit TLS]: ').strip() or '587')
    username = input('SMTP username (usually the full email address): ').strip()
    sender = input('From email address [{}]: '.format(username)).strip() or username
    password = getpass.getpass('SMTP password / app password (hidden): ')
    if port not in (465, 587) or not all((host, username, password)) or any(c in sender for c in '\r\n') or parseaddr(sender)[1] != sender or '@' not in sender:
        raise ValueError('Invalid SMTP settings')
    config = {'host': host, 'port': port, 'username': username, 'sender': sender, 'password': password}
    with connect(config):
        pass
    save_json(CONFIG, config)
    print('SMTP login succeeded. Settings saved with owner-only permissions. No email sent.')


def find_alarms(now):
    alarms = []
    quality = []
    cutoff = now - 72 * 3600
    for name, filename in METERS:
        path = ROOT / filename
        with path.open(encoding='utf-8-sig', newline='') as stream:
            fcntl.flock(stream.fileno(), fcntl.LOCK_SH)
            rows = list(csv.DictReader(stream))
        matches = {}
        known = []
        for row in rows:
            stamp = row.get('polled_at', '')
            try:
                tick = datetime.strptime(stamp, '%Y-%m-%d %H:%M:%S').timestamp()
            except ValueError:
                continue
            if not cutoff <= tick <= now:
                continue
            raw = (row.get('fault') or '').strip()
            if not raw.isdigit():
                continue
            known.append(tick)
            active = [label for bit, label in FLAGS if int(raw) & bit]
            if active:
                label = name
                if name == 'Relocated meter':
                    label = 'House' if stamp >= CHANGEOVER else 'Total property'
                matches.setdefault(label, []).append((tick, stamp, active))
        for label, events in matches.items():
            latest = max(events, key=lambda row: row[0])
            flags = sorted({flag for row in events for flag in row[2]})
            alarms.append({'meter': label, 'labels': flags, 'latest': latest[1]})
        ticks = sorted(set(known))
        if not ticks or now - ticks[-1] > 3 * 3600:
            quality.append(name + ': recent alarm data is unavailable or out of date.')
        elif ticks[0] - cutoff > 90 * 60 or any(b-a > 90*60 for a,b in zip(ticks,ticks[1:])):
            quality.append(name + ': the available alarm history has gaps.')
    return alarms, quality


def make_message(config, alarms, quality, now):
    message = EmailMessage()
    message['From'] = config['sender']
    message['To'] = ', '.join(RECIPIENTS)
    message['Reply-To'] = 'brookesa@yahoo.com'
    message['Subject'] = 'Water meter warning — ' + ', '.join(row['meter'] for row in alarms)
    message['Date'] = datetime.fromtimestamp(now).astimezone().strftime('%a, %d %b %Y %H:%M:%S %z')
    lines = ['Water meter alarms were recorded during the previous 72 hours.',
             'Check time: ' + datetime.fromtimestamp(now).strftime('%Y-%m-%d %H:%M:%S') + ' (Cyprus time).', '']
    for row in alarms:
        lines.extend([row['meter'] + ': ' + ', '.join(row['labels']),
                      'Most recent recorded alarm: ' + row['latest'] + ' (Cyprus time).', ''])
    lines.extend(['These are recorded meter flags, not confirmation of a leak or that an alarm remains active.',
                  'Relocated meter alarms refer to Total property before 18 November 2026 at 12:50 Cyprus time and House from that time onward.',
                  'Dashboard: https://brookes.ch/water/'])
    if quality:
        lines.extend(['', 'Data availability:'] + quality)
    message.set_content('\n'.join(lines))
    return message


def main():
    os.umask(0o077)
    if not Path('/usr/share/zoneinfo/Asia/Nicosia').exists():
        raise ValueError('Cyprus timezone data is missing')
    os.environ['TZ'] = 'Asia/Nicosia'
    time.tzset()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--setup', action='store_true')
    parser.add_argument('--preview', action='store_true', help='Check and show email text without sending')
    args = parser.parse_args()
    if args.setup:
        setup()
        return 0
    now = time.time()
    alarms, quality = find_alarms(now)
    if args.preview:
        print('Recipients: ' + ', '.join(RECIPIENTS))
        if alarms:
            print(make_message({'sender': 'preview@example.invalid'}, alarms, quality, now).get_content())
        else:
            print('No matching alarm recorded in the previous 72 hours. No email would be sent.')
        for note in quality:
            print(note)
        return 0
    with (ROOT / '.alarm-email.lock').open('a') as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        today = datetime.fromtimestamp(now).strftime('%Y-%m-%d')
        state = json.loads(STATE.read_text()) if STATE.exists() else {}
        if state.get('completed_date') == today:
            print('Daily check already completed; no duplicate email sent.')
            return 0
        if not alarms:
            for note in quality:
                print(note)
            # Missing data must not be recorded as a successful all-clear.
            if quality:
                print('No matching alarm found, but history is incomplete. No email sent.')
                return 1
            save_json(STATE, {'completed_date': today, 'result': 'no_alarm'})
            print('No matching alarms. No email sent.')
            return 0
        info = CONFIG.stat()
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
            raise ValueError('Email configuration must be owned by pi with permissions 600')
        config = json.loads(CONFIG.read_text(encoding='utf-8'))
        message = make_message(config, alarms, quality, now)
        with connect(config) as smtp:
            refused = smtp.send_message(message)
        if refused:
            raise RuntimeError('One or more recipients were rejected; inspect delivery with the mail provider')
        save_json(STATE, {'completed_date': today, 'result': 'sent', 'meters': [row['meter'] for row in alarms]})
        print('Alarm email accepted by the mail server for both recipients.')
        return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception as exc:
        # Do not expose authentication settings or SMTP exchanges in logs.
        print('Daily alarm email failed: ' + type(exc).__name__)
        raise SystemExit(1)
