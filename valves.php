<?php
declare(strict_types=1);
ini_set('display_errors', '0');
// Installed outside the public directory by the Pi deployment command.
$private = '/home/clients/6e33a55e6023aaacad331aa24646622f/water-valve-private';
header('Cache-Control: no-store');
header('X-Content-Type-Options: nosniff');
header('Referrer-Policy: no-referrer');
header("Content-Security-Policy: default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'");
if (($_SERVER['HTTPS'] ?? '') !== 'on') {
    http_response_code(403);
    exit('HTTPS is required.');
}
ini_set('session.use_strict_mode', '1');
session_name('water_valve_session');
session_set_cookie_params(['lifetime'=>0, 'path'=>'/water/', 'secure'=>true, 'httponly'=>true, 'samesite'=>'Strict']);
session_start();
function escape(string $s): string { return htmlspecialchars($s, ENT_QUOTES, 'UTF-8'); }
function readJson(string $path): array {
    $value = json_decode((string)file_get_contents($path), true, 512, JSON_THROW_ON_ERROR);
    if (!is_array($value)) { throw new RuntimeException('Invalid configuration.'); }
    return $value;
}
function writeState($file, array $value): void {
    rewind($file);
    $data = json_encode($value, JSON_THROW_ON_ERROR);
    if (fwrite($file, $data) !== strlen($data) || !ftruncate($file, strlen($data)) || !fflush($file)) {
        throw new RuntimeException('Unable to save control state.');
    }
}
function tuya(array $config, string $method, string $path, string $token='', ?array $body=null): array {
    $payload = $body === null ? '' : json_encode($body, JSON_THROW_ON_ERROR | JSON_UNESCAPED_SLASHES);
    $time = (string)(int)floor(microtime(true)*1000);
    $signed = $method . "\n" . hash('sha256', $payload) . "\n\n" . $path;
    $signature = strtoupper(hash_hmac('sha256', $config['access_id'].$token.$time.$signed, $config['access_secret']));
    $curl = curl_init('https://openapi.tuyaeu.com'.$path);
    curl_setopt_array($curl, [CURLOPT_RETURNTRANSFER=>true, CURLOPT_CUSTOMREQUEST=>$method,
        CURLOPT_CONNECTTIMEOUT=>8, CURLOPT_TIMEOUT=>20, CURLOPT_FOLLOWLOCATION=>false,
        CURLOPT_HTTPHEADER=>['client_id: '.$config['access_id'], 'access_token: '.$token,
            'sign: '.$signature, 'sign_method: HMAC-SHA256', 't: '.$time, 'Content-Type: application/json']]);
    if ($body !== null) { curl_setopt($curl, CURLOPT_POSTFIELDS, $payload); }
    $raw = curl_exec($curl);
    $http = curl_getinfo($curl, CURLINFO_HTTP_CODE);
    curl_close($curl);
    if ($raw === false) { throw new RuntimeException('Tuya connection failed.'); }
    $reply = json_decode($raw, true);
    if ($http !== 200 || !is_array($reply) || ($reply['success'] ?? false) !== true) {
        throw new RuntimeException('Tuya rejected the request.');
    }
    return $reply;
}
function token(array $config): string {
    $reply = tuya($config, 'GET', '/v1.0/token?grant_type=1');
    $token = $reply['result']['access_token'] ?? null;
    if (!is_string($token) || $token === '') { throw new RuntimeException('Tuya login failed.'); }
    return $token;
}
function valveState(array $config, string $meter, string $token): ?bool {
    $path = '/v1.0/iot-03/devices/'.rawurlencode($config['meters'][$meter]).'/status';
    $reply = tuya($config, 'GET', $path, $token);
    foreach (($reply['result'] ?? []) as $field) {
        if (($field['code'] ?? '') === 'switch_code' && is_bool($field['value'] ?? null)) { return $field['value']; }
    }
    return null;
}
$message = '';
$states = [];
try {
    $config = readJson($private.'/config.json');
    if (isset($_SESSION['authenticated']) && time()-($_SESSION['last_activity'] ?? 0)>900) { $_SESSION=[]; }
    $_SESSION['csrf'] ??= bin2hex(random_bytes(32));
    if (($_SERVER['REQUEST_METHOD'] ?? '') === 'POST') {
        if (!is_string($_POST['csrf'] ?? null) || !hash_equals($_SESSION['csrf'], $_POST['csrf'])) {
            throw new RuntimeException('This form expired. Refresh and try again.');
        }
        $action = $_POST['action'] ?? '';
        if ($action === 'login') {
            // Global limits also protect against attempts that discard session cookies.
            $lock = fopen($private.'/login-state.json', 'c+');
            if ($lock === false || !flock($lock, LOCK_EX)) { throw new RuntimeException('Sign-in is unavailable.'); }
            try {
                $record = json_decode(stream_get_contents($lock), true) ?: ['start'=>time(), 'count'=>0];
                if (time()-$record['start']>=900) { $record=['start'=>time(), 'count'=>0]; }
                if ($record['count']>=10) { throw new RuntimeException('Too many sign-in attempts. Try again in 15 minutes.'); }
                $record['count']++;
                writeState($lock, $record);
                $password = $_POST['password'] ?? '';
                if (!is_string($password) || !password_verify($password, $config['password_hash'])) {
                    throw new RuntimeException('Sign-in failed.');
                }
                writeState($lock, ['start'=>time(), 'count'=>0]);
            } finally { flock($lock, LOCK_UN); fclose($lock); }
            session_regenerate_id(true);
            $_SESSION=['authenticated'=>true, 'last_activity'=>time(), 'csrf'=>bin2hex(random_bytes(32))];
            header('Location: valves.php', true, 303);
            exit;
        } elseif ($action === 'logout') {
            $_SESSION=[];
            session_regenerate_id(true);
            header('Location: valves.php', true, 303);
            exit;
        } elseif (!($_SESSION['authenticated'] ?? false)) {
            throw new RuntimeException('Sign in first.');
        } elseif ($action === 'command') {
            $meter = $_POST['meter'] ?? '';
            $desired = $_POST['desired'] ?? '';
            $operation = $_POST['operation'] ?? '';
            if (!in_array($meter, ['house','garden'], true) || !in_array($desired, ['open','closed'], true)
                || !is_string($operation) || !hash_equals($_SESSION['operation'] ?? '', $operation)
                || ($_POST['confirm'] ?? '') !== 'yes') { throw new RuntimeException('Refresh the page and confirm the selected valve action.'); }
            unset($_SESSION['operation']);
            $lock = fopen($private.'/command-state.json', 'c+');
            if ($lock === false || !flock($lock, LOCK_EX | LOCK_NB)) { throw new RuntimeException('A valve command is already in progress.'); }
            try {
                $record = json_decode(stream_get_contents($lock), true) ?: [];
                if (time()-($record['time'] ?? 0)<30) { throw new RuntimeException('Wait 30 seconds, then refresh the valve status.'); }
                $apiToken = token($config);
                writeState($lock, ['time'=>time(), 'meter'=>$meter, 'requested'=>$desired]);
                $path = '/v1.0/iot-03/devices/'.rawurlencode($config['meters'][$meter]).'/commands';
                try {
                    $reply = tuya($config, 'POST', $path, $apiToken, ['commands'=>[['code'=>'switch_code','value'=>$desired==='open']]]);
                    if (($reply['result'] ?? null) !== true) { throw new RuntimeException('Unexpected response.'); }
                    $message = ucfirst($meter).' command accepted by Tuya. The reported switch state below may take time to update. Use Refresh status to check again.';
                } catch (Throwable $error) {
                    $message = ucfirst($meter).' command outcome is uncertain. Check the Tuya app and refresh status before submitting another command.';
                }
                $_SESSION['flash']=$message;
            } finally { flock($lock, LOCK_UN); fclose($lock); }
            header('Location: valves.php', true, 303);
            exit;
        } elseif ($action !== 'refresh') { throw new RuntimeException('Unknown action.'); }
    }
    if ($_SESSION['authenticated'] ?? false) {
        $_SESSION['last_activity']=time();
        $message = $_SESSION['flash'] ?? '';
        unset($_SESSION['flash']);
        $_SESSION['operation'] ??= bin2hex(random_bytes(32));
        $apiToken=token($config);
        foreach (['house','garden'] as $meter) {
            try { $states[$meter]=valveState($config, $meter, $apiToken); }
            catch (Throwable $error) { $states[$meter]=null; }
        }
    }
} catch (Throwable $error) {
    // Error messages above contain no Tuya response bodies or configuration values.
    $message = $error instanceof RuntimeException ? $error->getMessage() : 'Valve controls are unavailable. Try again later.';
}
$authenticated = (bool)($_SESSION['authenticated'] ?? false);
$csrf = escape($_SESSION['csrf'] ?? '');
?>
<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Water valve controls · brookes.ch</title>
<style>body{font-family:system-ui,sans-serif;background:#f3f7f8;color:#173641;margin:0}main{max-width:760px;margin:40px auto;padding:0 20px}section{background:white;border:1px solid #dce7e9;border-radius:12px;padding:22px;margin:20px 0}button,input{font:inherit;padding:10px;border:1px solid #bccfd3;border-radius:6px}button{background:#176d7d;color:white;cursor:pointer}label{display:block;margin:14px 0}input[type=checkbox]{margin-right:8px}.notice{padding:16px;background:#e9f2f4}form{margin:14px 0}a{color:#176d7d}button:disabled{opacity:.5;cursor:default}</style>
</head><body><main><a href="./">Water dashboard</a><h1>Water valve controls</h1>
<?php if ($message !== ''): ?><p class="notice" role="status"><?=escape($message)?></p><?php endif; ?>
<?php if (!$authenticated): ?>
<section><h2>Sign in</h2><form method="post"><input type="hidden" name="csrf" value="<?=$csrf?>"><input type="hidden" name="action" value="login"><label>Valve control password <input type="password" name="password" autocomplete="current-password" required maxlength="200"></label><button>Sign in</button></form></section>
<?php else: ?>
<p>Reported switch states come from Tuya and can lag behind the physical valves. Sessions expire after 15 minutes of inactivity.</p>
<form method="post"><input type="hidden" name="csrf" value="<?=$csrf?>"><button name="action" value="refresh">Refresh status</button> <button name="action" value="logout">Sign out</button></form>
<?php foreach (['house'=>'House','garden'=>'Garden'] as $meter=>$label): $state=$states[$meter] ?? null; ?>
<section><h2><?=escape($label)?></h2><p>Tuya reports <?= $state === true ? 'Open' : ($state === false ? 'Closed' : 'status unavailable') ?>.</p>
<?php foreach (['open'=>'Open','closed'=>'Close'] as $desired=>$verb): ?>
<form method="post"><input type="hidden" name="csrf" value="<?=$csrf?>"><input type="hidden" name="action" value="command"><input type="hidden" name="meter" value="<?=escape($meter)?>"><input type="hidden" name="desired" value="<?=escape($desired)?>"><input type="hidden" name="operation" value="<?=escape($_SESSION['operation'] ?? '')?>"><label><input type="checkbox" name="confirm" value="yes" required>I confirm: <?=escape(strtolower($verb))?> the <?=escape($label)?> valve.</label><button <?= $state === null ? 'disabled' : '' ?>><?=escape($verb)?> <?=escape($label)?></button></form>
<?php endforeach; ?></section>
<?php endforeach; endif; ?>
</main></body></html>
