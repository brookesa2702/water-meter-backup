"""Install the website valve page through the Pi's existing publishing SSH key."""
import base64
import getpass
import json
from pathlib import Path
import subprocess

HOST = 'vs672t_watermeter@vs672t.ftp.infomaniak.com'
OPTIONS = ['-i', str(Path.home() / '.ssh/water_dashboard'),
           '-o', 'IdentitiesOnly=yes', '-o', 'BatchMode=yes',
           '-o', 'StrictHostKeyChecking=yes', '-o', 'ConnectTimeout=20']


def install():
    answer = input('Confirm both valves show OPEN in the app and switch_code is true. Type YES: ')
    if answer != 'YES':
        raise SystemExit('Installation stopped before making changes.')
    password = getpass.getpass('Create a separate website valve password (at least 16 characters): ')
    if len(password) < 16 or len(password.encode('utf-8')) > 72:
        raise SystemExit('Use at least 16 characters and at most 72 UTF-8 bytes.')
    if password != getpass.getpass('Repeat the website valve password: '):
        raise SystemExit('Passwords differ.')
    root = Path('/home/pi/water-meter')
    config = json.loads((root / 'water_meter_config.json').read_text())
    if config['endpoint'] != 'https://openapi.tuyaeu.com':
        raise SystemExit('Unexpected Tuya endpoint.')
    folder = Path(__file__).resolve().parent
    payload = {
        'access_id': config['access_id'], 'access_secret': config['access_secret'],
        'password': password, 'source': (folder / 'valves.php').read_text(),
        'meters': {'house': 'bfec26e9610452f97erqyg', 'garden': 'bf988860ca07292df4ofod'},
    }
    encoded = base64.b64encode(json.dumps(payload).encode()).decode()
    # Credentials travel through SSH stdin, outside command arguments and public files.
    installer = r'''<?php
    declare(strict_types=1);
    umask(0077);
    $payload=json_decode(base64_decode('__PAYLOAD__'), true, 512, JSON_THROW_ON_ERROR);
    $home=getenv('HOME');
    if (!$home || !is_dir($home.'/sites/brookes.ch/water')) { throw new RuntimeException('Website directory missing.'); }
    $private=$home.'/water-valve-private';
    $web=$home.'/sites/brookes.ch/water';
    if (file_exists($private.'/config.json') || file_exists($web.'/valves.php')) {
        throw new RuntimeException('Valve installation already exists. Stop and review before replacing it.');
    }
    $source=str_replace("'__PRIVATE_DIRECTORY__'", var_export($private,true), $payload['source']);
    token_get_all($source, TOKEN_PARSE);
    $dashboard=file_get_contents($web.'/index.html');
    if (!is_string($dashboard) || substr_count($dashboard,'</header>')!==1) {
        throw new RuntimeException('Unexpected dashboard structure.');
    }
    $updated=str_replace('</header>','</header><p><a href="valves.php">Valve controls — sign in</a></p>',$dashboard);
    $stamp=gmdate('Ymd\THis\Z').'-'.bin2hex(random_bytes(4));
    function saveFile(string $path, string $contents, int $mode=0600): void {
        if (file_put_contents($path,$contents,LOCK_EX)!==strlen($contents) || !chmod($path,$mode)) {
            throw new RuntimeException('Failed to save installation file.');
        }
    }
    if (!mkdir($private,0700) && !is_dir($private)) { throw new RuntimeException('Private directory unavailable.'); }
    chmod($private,0700);
    $config=['access_id'=>$payload['access_id'],'access_secret'=>$payload['access_secret'],
        'password_hash'=>password_hash($payload['password'],PASSWORD_DEFAULT),'meters'=>$payload['meters']];
    saveFile($private.'/config.json',json_encode($config,JSON_THROW_ON_ERROR));
    saveFile($private.'/index-before-valves-'.$stamp.'.html',$dashboard);
    saveFile($web.'/valves.php.pending',$source,0644);
    saveFile($web.'/index.html.pending',$updated,0644);
    // Activate the endpoint before exposing the navigation link.
    if (!rename($web.'/valves.php.pending',$web.'/valves.php') || !rename($web.'/index.html.pending',$web.'/index.html')) {
        throw new RuntimeException('Activation incomplete. Review the deployment before retrying.');
    }
    echo "Installed valve controls at https://brookes.ch/water/valves.php\n";
    echo "Dashboard backup: ".$private.'/index-before-valves-'.$stamp.".html\n";
    echo "No valve commands were sent by this installer.\n";
    '''.replace('__PAYLOAD__', encoded)
    subprocess.run(['ssh'] + OPTIONS + [HOST, 'php'], input=installer.encode(), check=True, timeout=90)


if __name__ == '__main__':
    install()
