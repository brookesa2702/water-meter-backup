"""Publish consistent CSV snapshots to brookes.ch. Python 3.7, Linux."""
import fcntl
from pathlib import Path
import shutil
import subprocess
import tempfile

ROOT = Path('/home/pi/water-meter')
HOST = 'vs672t_watermeter@vs672t.ftp.infomaniak.com'
REMOTE = 'sites/brookes.ch/water/'
OPTIONS = ['-i', str(Path.home() / '.ssh/water_dashboard'),
           '-o', 'IdentitiesOnly=yes', '-o', 'BatchMode=yes',
           '-o', 'StrictHostKeyChecking=yes', '-o', 'ConnectTimeout=20',
           '-o', 'ServerAliveInterval=15', '-o', 'ServerAliveCountMax=3']


def publish():
    # Lock while copying, then release before networking so meter logging continues.
    with tempfile.TemporaryDirectory(prefix='water-publish-') as folder:
        snapshots = []
        for name in ('house', 'garden'):
            source = ROOT / (name + '_readings.csv')
            target = Path(folder) / source.name
            with source.open('rb') as stream:
                fcntl.flock(stream.fileno(), fcntl.LOCK_SH)
                with target.open('wb') as output:
                    shutil.copyfileobj(stream, output)
            snapshots.append(target)
        # Temporary remote filenames keep visitors from reading partial uploads.
        for path in snapshots:
            subprocess.run(['scp'] + OPTIONS + [str(path), HOST + ':' + REMOTE + path.name + '.upload'],
                           check=True, timeout=90)
        command = ('cd ~/sites/brookes.ch/water && '
                   'chmod 644 house_readings.csv.upload garden_readings.csv.upload && '
                   'mv -f house_readings.csv.upload house_readings.csv && '
                   'mv -f garden_readings.csv.upload garden_readings.csv')
        subprocess.run(['ssh'] + OPTIONS + [HOST, command], check=True, timeout=45)
        print('House and Garden CSVs published to https://brookes.ch/water/', flush=True)


if __name__ == '__main__':
    publish()
