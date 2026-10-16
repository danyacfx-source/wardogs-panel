<?php
declare(strict_types=1);

/*
 * Private, write-only bridge from the WARDOGS RU Discord bot to data/clans.json.
 * Put the secret configuration OUTSIDE public_html:
 *   ../private-config/clans-secret.php
 */

header('Content-Type: application/json; charset=utf-8');
header('Cache-Control: no-store, max-age=0');
header('X-Robots-Tag: noindex, nofollow, noarchive');

const CLAN_LOGO_MAX_BYTES = 25 * 1024 * 1024;

function finish(int $status, string $code): never
{
    http_response_code($status);
    echo json_encode(['ok' => $status >= 200 && $status < 300, 'code' => $code], JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES);
    exit;
}

if (($_SERVER['REQUEST_METHOD'] ?? '') !== 'POST') {
    header('Allow: POST');
    finish(405, 'method_not_allowed');
}

$configPath = dirname(__DIR__) . '/private-config/clans-secret.php';
if (!is_file($configPath)) {
    error_log('WARDOGS clan sync: missing private configuration');
    finish(503, 'not_configured');
}

$config = require $configPath;
$secret = is_array($config) ? ($config['secret'] ?? '') : '';
$allowedIp = is_array($config) ? ($config['allowed_ip'] ?? '') : '';
if (!is_string($secret) || strlen($secret) < 32) {
    error_log('WARDOGS clan sync: invalid secret configuration');
    finish(503, 'not_configured');
}
if (is_string($allowedIp) && $allowedIp !== '' && !hash_equals($allowedIp, (string)($_SERVER['REMOTE_ADDR'] ?? ''))) {
    finish(403, 'forbidden');
}

$length = (int)($_SERVER['CONTENT_LENGTH'] ?? 0);
if ($length < 2 || $length > 262144) {
    finish(413, 'invalid_body_size');
}
$raw = file_get_contents('php://input', false, null, 0, 262145);
if (!is_string($raw) || strlen($raw) !== $length || strlen($raw) > 262144) {
    finish(400, 'invalid_body');
}

$timestamp = (string)($_SERVER['HTTP_X_WARDOGS_TIMESTAMP'] ?? '');
$signature = strtolower((string)($_SERVER['HTTP_X_WARDOGS_SIGNATURE'] ?? ''));
if (!ctype_digit($timestamp) || abs(time() - (int)$timestamp) > 300 || !preg_match('/^[a-f0-9]{64}$/', $signature)) {
    finish(401, 'invalid_signature');
}
$expected = hash_hmac('sha256', $timestamp . '.' . $raw, $secret);
if (!hash_equals($expected, $signature)) {
    finish(401, 'invalid_signature');
}

try {
    $payload = json_decode($raw, true, 16, JSON_THROW_ON_ERROR);
} catch (JsonException) {
    finish(400, 'invalid_json');
}

function textField(array $value, string $key, int $max): bool
{
    return isset($value[$key]) && is_string($value[$key]) && trim($value[$key]) !== '' && mb_strlen($value[$key], 'UTF-8') <= $max;
}

if (!is_array($payload) || ($payload['version'] ?? null) !== 1 || !isset($payload['source']['guild_id']) || $payload['source']['guild_id'] !== '403186008579112973' || !isset($payload['clans']) || !is_array($payload['clans']) || count($payload['clans']) > 100) {
    finish(422, 'invalid_feed');
}

$discordInvite = '~^https://(?:discord\.gg|discord\.com/invite)/[A-Za-z0-9-]{2,64}$~D';
$recruitmentPost = '~^https://discord\.com/channels/403186008579112973/\d{17,20}/\d{17,20}$~D';
$discordImage = '~^https://(?:cdn\.discordapp\.com|media\.discordapp\.net)/attachments/\d{17,20}/\d{17,20}/[A-Za-z0-9_.-]{1,200}(?:\?[^\s]{1,300})?$~D';
foreach ($payload['clans'] as $clan) {
    if (!is_array($clan)) finish(422, 'invalid_clan');
    $logoSource = $clan['logo_source_url'] ?? null;
    $logoHash = $clan['logo_sha256'] ?? null;
    $logoMime = $clan['logo_mime'] ?? null;
    $logoValid = ($logoSource === null && $logoHash === null && $logoMime === null) ||
        (is_string($logoSource) && preg_match($discordImage, $logoSource) && is_string($logoHash) && preg_match('/^[a-f0-9]{64}$/D', $logoHash) && in_array($logoMime, ['image/png', 'image/jpeg', 'image/webp'], true));
    if (!preg_match('/^\d{17,20}$/D', (string)($clan['id'] ?? '')) ||
        !textField($clan, 'name', 80) || !textField($clan, 'tag', 16) || !textField($clan, 'leader_name', 80) ||
        !textField($clan, 'description', 600) || !textField($clan, 'goals', 600) ||
        !isset($clan['declared_members'], $clan['server_members']) || !is_int($clan['declared_members']) || !is_int($clan['server_members']) ||
        $clan['declared_members'] < 1 || $clan['declared_members'] > 100000 || $clan['server_members'] < 0 || $clan['server_members'] > 100000 ||
        !isset($clan['discord_url']) || !is_string($clan['discord_url']) || !preg_match($discordInvite, $clan['discord_url']) ||
        !in_array($clan['recruitment_status'] ?? null, ['open', 'closed'], true) ||
        (($clan['recruitment_url'] ?? null) !== null && (!is_string($clan['recruitment_url']) || !preg_match($recruitmentPost, $clan['recruitment_url']))) ||
        !textField($clan, 'created_at', 40) || !textField($clan, 'updated_at', 40) || !$logoValid) {
        finish(422, 'invalid_clan');
    }
}

function detectedImageMime(string $bytes): ?string
{
    if (str_starts_with($bytes, "\x89PNG\r\n\x1a\n")) return 'image/png';
    if (strlen($bytes) >= 3 && substr($bytes, 0, 3) === "\xff\xd8\xff") return 'image/jpeg';
    if (strlen($bytes) >= 12 && substr($bytes, 0, 4) === 'RIFF' && substr($bytes, 8, 4) === 'WEBP') return 'image/webp';
    return null;
}

function localizeClanLogo(array &$clan, string $dataDir): void
{
    $source = $clan['logo_source_url'] ?? null;
    $hash = $clan['logo_sha256'] ?? null;
    $mime = $clan['logo_mime'] ?? null;
    if ($source === null) {
        $clan['logo_url'] = null;
        unset($clan['logo_source_url'], $clan['logo_sha256'], $clan['logo_mime']);
        return;
    }

    $logoDir = $dataDir . '/clan-logos';
    if (!is_dir($logoDir) && !mkdir($logoDir, 0755, true) && !is_dir($logoDir)) finish(500, 'logo_write_failed');
    $extension = ['image/png' => 'png', 'image/jpeg' => 'jpg', 'image/webp' => 'webp'][$mime];
    $prefix = $clan['id'] . '-';
    $filename = $prefix . substr($hash, 0, 20) . '.' . $extension;
    $target = $logoDir . '/' . $filename;

    if (!is_file($target)) {
        $context = stream_context_create(['http' => [
            'method' => 'GET', 'timeout' => 15, 'follow_location' => 0, 'ignore_errors' => true,
            'header' => "User-Agent: WARDOGS-RU-Clan-Sync/1.0\r\nAccept: image/png,image/jpeg,image/webp\r\n",
        ]]);
        $bytes = @file_get_contents($source, false, $context, 0, CLAN_LOGO_MAX_BYTES + 1);
        $statusLine = $http_response_header[0] ?? '';
        if (!is_string($bytes) || !preg_match('~^HTTP/\S+ 200(?:\s|$)~', $statusLine) || strlen($bytes) < 1 || strlen($bytes) > CLAN_LOGO_MAX_BYTES) finish(422, 'invalid_clan_logo');
        if (!hash_equals($hash, hash('sha256', $bytes)) || detectedImageMime($bytes) !== $mime) finish(422, 'invalid_clan_logo');
        $tempLogo = $logoDir . '/.' . bin2hex(random_bytes(8)) . '.tmp';
        if (file_put_contents($tempLogo, $bytes, LOCK_EX) === false) finish(500, 'logo_write_failed');
        @chmod($tempLogo, 0644);
        if (!rename($tempLogo, $target)) { @unlink($tempLogo); finish(500, 'logo_write_failed'); }
    }

    foreach (glob($logoDir . '/' . $prefix . '*') ?: [] as $oldLogo) {
        if ($oldLogo !== $target && is_file($oldLogo)) @unlink($oldLogo);
    }
    $clan['logo_url'] = '/data/clan-logos/' . $filename;
    unset($clan['logo_source_url'], $clan['logo_sha256'], $clan['logo_mime']);
}

$payload['generated_at'] = gmdate('c');
$dataDir = __DIR__ . '/data';
foreach ($payload['clans'] as &$clan) localizeClanLogo($clan, $dataDir);
unset($clan);
$activeLogos = [];
foreach ($payload['clans'] as $clan) {
    if (is_string($clan['logo_url'] ?? null)) $activeLogos[basename($clan['logo_url'])] = true;
}
foreach (glob($dataDir . '/clan-logos/*.{png,jpg,webp}', GLOB_BRACE) ?: [] as $logoFile) {
    if (is_file($logoFile) && !isset($activeLogos[basename($logoFile)])) @unlink($logoFile);
}
$encoded = json_encode($payload, JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES | JSON_PRETTY_PRINT | JSON_THROW_ON_ERROR) . "\n";
$target = $dataDir . '/clans.json';
$temp = $dataDir . '/.clans-' . bin2hex(random_bytes(8)) . '.tmp';

if (!is_dir($dataDir) || file_put_contents($temp, $encoded, LOCK_EX) === false) {
    finish(500, 'write_failed');
}
@chmod($temp, 0644);
if (!rename($temp, $target)) {
    @unlink($temp);
    finish(500, 'write_failed');
}

finish(200, 'updated');
