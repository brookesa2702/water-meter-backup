"""Read a Tuya meter's status and specification without API Explorer."""
import getpass
import json
import hashlib
import hmac
import time
import re
from html import unescape
from pathlib import Path
from urllib.parse import quote, urlparse
from urllib.request import Request, urlopen, getproxies
from urllib.error import HTTPError, URLError


class TuyaAPI:
    """Minimal read-only client; never logs credentials or tokens."""
    def __init__(self, endpoint, access_id, access_secret):
        self.endpoint = endpoint
        self.access_id = access_id
        self.access_secret = access_secret
        self.token = ''

    def safe_text(self, value):
        text = str(value)
        for private in (self.access_id, self.access_secret, self.token):
            if private:
                text = text.replace(private, '[redacted]')
        text = re.sub(r'(?i)(access_token|refresh_token|client_secret|access_secret|sign)\s*[=:]\s*[^\s<,;]+', r'\1=[redacted]', text)
        return text

    def response_details(self, payload, headers):
        text = payload.decode('utf-8', errors='replace')
        text = re.sub(r'(?is)<(script|style)\b.*?</\1>', '', text)
        text = unescape(re.sub(r'<[^>]+>', ' ', text))
        text = ' '.join(self.safe_text(text).split())
        return {
            'content_type': self.safe_text(headers.get('Content-Type', '(not supplied)')),
            'server': self.safe_text(headers.get('Server', '(not supplied)')),
            'response_preview': text[:600] or '(empty response body)',
        }

    def get(self, path):
        """Retry read requests, renewing rejected tokens without recursion."""
        is_token = path.split('?', 1)[0] == '/v1.0/token'
        for attempt in range(4):
            try:
                response = self._get_once(path)
            except (URLError, TimeoutError, ConnectionError):
                if attempt == 3:
                    raise
                time.sleep((2, 5, 15)[attempt])
                continue
            code = str(response.get('code', ''))
            if not is_token and code in ('1010', '1011') and attempt < 3:
                time.sleep((2, 5, 15)[attempt])
                auth = self.connect()
                if not auth.get('success'):
                    return auth
                continue
            temporary = code in ('500', '501', '1005') or response.get('http_status') in (429, 500, 502, 503, 504)
            if temporary and attempt < 3:
                time.sleep((2, 5, 15)[attempt])
                continue
            return response

    def _get_once(self, path):
        timestamp = str(int(time.time() * 1000))
        content_hash = hashlib.sha256(b'').hexdigest()
        string_to_sign = f'GET\n{content_hash}\n\n{path}'
        message = self.access_id + self.token + timestamp + string_to_sign
        signature = hmac.new(self.access_secret.encode(), message.encode(), hashlib.sha256).hexdigest().upper()
        request = Request(self.endpoint + path, headers={
            'client_id': self.access_id, 'access_token': self.token,
            'sign': signature, 'sign_method': 'HMAC-SHA256',
            't': timestamp, 'lang': 'en', 'User-Agent': 'WaterMeterDiagnostic/3.0',
        })
        try:
            with urlopen(request, timeout=30) as response:
                payload = response.read()
        except HTTPError as exc:
            # HTTP errors may contain Tuya error JSON or a proxy's HTML page.
            payload = exc.read(65536)
            try:
                data = json.loads(payload)
            except (ValueError, UnicodeDecodeError):
                data = {}
            if not isinstance(data, dict):
                data = {}
            error = {'success': False, 'http_status': exc.code,
                     'code': data.get('code'), 'msg': self.safe_text(data.get('msg', 'HTTP request rejected'))}
            # Do not print a full JSON authentication response containing tokens.
            if not data:
                error.update(self.response_details(payload, exc.headers or {}))
            else:
                error['content_type'] = self.safe_text((exc.headers or {}).get('Content-Type', '(not supplied)'))
            return error
        try:
            data = json.loads(payload)
        except (ValueError, UnicodeDecodeError):
            return {'success': False, 'msg': 'Server returned an unexpected non-JSON response'}
        if not isinstance(data, dict):
            return {'success': False, 'msg': 'Server returned an unexpected JSON format'}
        return data

    def connect(self):
        # Token requests must never be signed with a rejected previous token.
        self.token = ''
        response = self.get('/v1.0/token?grant_type=1')
        if response.get('success'):
            result = response.get('result')
            token = result.get('access_token') if isinstance(result, dict) else None
            if not isinstance(token, str) or not token:
                return {'success': False, 'msg': 'Authentication response did not contain an access token'}
            self.token = token
        return response


def show_error(label, response):
    print(label)
    for key in ('http_status', 'code', 'msg', 'content_type', 'server', 'response_preview'):
        if response.get(key) is not None:
            print(f'  {key}: {response[key]}')


def main():
    print('Tuya meter diagnostic v3 (no additional Python packages required).')
    proxies = getproxies()
    print('Windows/Python HTTPS proxy configured:', 'yes' if proxies.get('https') or proxies.get('all') else 'no')
    print('Enter details from your Tuya cloud project. Credentials are not saved.')
    endpoint = input('API endpoint [https://openapi.tuyaeu.com]: ').strip().rstrip('/') or 'https://openapi.tuyaeu.com'
    parsed = urlparse(endpoint)
    allowed = {'openapi.tuyacn.com', 'openapi.tuyaus.com', 'openapi.tuyaeu.com',
               'openapi.tuyain.com', 'openapi-ueaz.tuyaus.com', 'openapi-weaz.tuyaeu.com'}
    if parsed.scheme != 'https' or parsed.netloc not in allowed or parsed.path:
        print('Please use an official Tuya API endpoint for your project data center.')
        return 1
    access_id = getpass.getpass('Access ID / Client ID (hidden): ').strip()
    access_secret = getpass.getpass('Access Secret / Client Secret (hidden): ').strip()
    device_id = input('Meter Device ID: ').strip()
    if not all((access_id, access_secret, device_id)):
        print('All fields are required.')
        return 1
    for label, value in (('Access ID', access_id), ('Access Secret', access_secret)):
        if not value.isascii() or any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in value):
            print(f'{label} contains whitespace or a non-ASCII character. Copy only the credential value.')
            return 1
    api = TuyaAPI(endpoint, access_id, access_secret)
    stage = 'authentication'
    try:
        auth = api.connect()
        if not isinstance(auth, dict) or not auth.get('success'):
            show_error('Authentication failed:', auth)
            return 1
        device = quote(device_id, safe='')
        result = {}
        for name, path in (
            ('status', f'/v1.0/iot-03/devices/{device}/status'),
            ('specification', f'/v1.2/iot-03/devices/{device}/specification'),
        ):
            stage = name
            response = api.get(path)
            # Save only these read responses, never the authentication response.
            result[name] = {key: response[key] for key in ('success', 'result', 'http_status', 'code', 'msg') if key in response}
            if not response.get('success'):
                show_error(f'{name.capitalize()} request failed:', response)
        stage = 'saving results'
        destination = Path(__file__).resolve().with_name('meter_details.json')
        destination.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding='utf-8')
        print('Saved meter status and specification to:', destination)
        print('Review that file before sharing it. It does not include your authentication tokens.')
        return 0 if all(r.get('success') for r in result.values()) else 1
    except (URLError, TimeoutError) as exc:
        print(f'Network request failed during {stage}: {type(exc).__name__}')
        print('Check connectivity, firewall/proxy settings, and the project endpoint.')
        return 1
    except Exception as exc:
        print(f'Program failed during {stage}: {type(exc).__name__}')
        # Show only file/line locations, never traceback locals or request headers.
        import traceback
        for frame in traceback.extract_tb(exc.__traceback__):
            print(f'  {Path(frame.filename).name}, line {frame.lineno}, in {frame.name}')
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
