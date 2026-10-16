<?php
declare(strict_types=1);

if (PHP_SAPI !== 'cli') {
    http_response_code(404);
    exit;
}

require_once __DIR__ . '/src/GiveawayParser.php';

function fail(string $message, int $code = 1): never
{
    fwrite(STDERR, '[' . gmdate('c') . '] ' . $message . PHP_EOL);
    exit($code);
}

function requiredSnowflake(array $config, string $key): string
{
    $value = $config[$key] ?? null;
    if (!is_string($value) || preg_match('/^\d{17,20}$/D', $value) !== 1) {
        fail("Invalid or missing config value: {$key}");
    }
    return $value;
}

function fetchMessages(string $token, string $channelId): array
{
    if (!function_exists('curl_init')) {
        fail('PHP cURL extension is required');
    }

    $url = sprintf('https://discord.com/api/v10/channels/%s/messages?limit=100', rawurlencode($channelId));
    $body = '';
    $curl = curl_init($url);
    $options = [
        CURLOPT_HTTPHEADER => [
            'Authorization: Bot ' . $token,
            'Accept: application/json',
            'User-Agent: WARDOGS-RU-Giveaway-Sync/1.0 (+https://wardogs-ru.ru)',
        ],
        CURLOPT_RETURNTRANSFER => false,
        CURLOPT_FOLLOWLOCATION => false,
        CURLOPT_CONNECTTIMEOUT => 10,
        CURLOPT_TIMEOUT => 25,
        CURLOPT_SSL_VERIFYPEER => true,
        CURLOPT_SSL_VERIFYHOST => 2,
        CURLOPT_FAILONERROR => false,
        CURLOPT_WRITEFUNCTION => static function ($handle, string $chunk) use (&$body): int {
            if (strlen($body) + strlen($chunk) > 5 * 1024 * 1024) {
                return 0;
            }
            $body .= $chunk;
            return strlen($chunk);
        },
    ];
    if (defined('CURLOPT_PROTOCOLS') && defined('CURLPROTO_HTTPS')) {
        $options[CURLOPT_PROTOCOLS] = CURLPROTO_HTTPS;
    }
    curl_setopt_array($curl, $options);

    $ok = curl_exec($curl);
    $status = (int) curl_getinfo($curl, CURLINFO_RESPONSE_CODE);
    $error = curl_error($curl);
    curl_close($curl);

    if ($ok === false) {
        fail('Discord request failed' . ($error !== '' ? ': ' . $error : ''));
    }
    if ($status === 429) {
        fail('Discord rate limit reached; the previous JSON was preserved', 75);
    }
    if ($status !== 200) {
        fail("Discord returned HTTP {$status}; the previous JSON was preserved");
    }

    try {
        $decoded = json_decode($body, true, 512, JSON_THROW_ON_ERROR);
    } catch (JsonException) {
        fail('Discord returned invalid JSON; the previous JSON was preserved');
    }
    if (!is_array($decoded)) {
        fail('Discord response has an unexpected shape');
    }
    return $decoded;
}

function atomicWrite(string $outputFile, array $feed): void
{
    if (!str_starts_with($outputFile, '/') || strtolower(pathinfo($outputFile, PATHINFO_EXTENSION)) !== 'json') {
        fail('output_file must be an absolute .json path');
    }
    if (is_link($outputFile)) {
        fail('Refusing to overwrite a symbolic link');
    }

    $directory = dirname($outputFile);
    if (!is_dir($directory) || !is_writable($directory)) {
        fail('Output directory does not exist or is not writable');
    }

    try {
        $json = json_encode($feed, JSON_PRETTY_PRINT | JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES | JSON_THROW_ON_ERROR) . PHP_EOL;
    } catch (JsonException) {
        fail('Could not encode giveaway feed');
    }

    $temporary = tempnam($directory, '.giveaways-');
    if ($temporary === false) {
        fail('Could not create a temporary output file');
    }
    try {
        if (file_put_contents($temporary, $json, LOCK_EX) !== strlen($json)) {
            fail('Could not write the complete giveaway feed');
        }
        chmod($temporary, 0644);
        if (!rename($temporary, $outputFile)) {
            fail('Could not atomically replace the giveaway feed');
        }
    } finally {
        if (is_file($temporary)) {
            unlink($temporary);
        }
    }
}

$configPath = getenv('WARDOGS_GIVEAWAY_CONFIG') ?: __DIR__ . '/config.php';
if (!is_file($configPath)) {
    fail('Config not found. Copy config.example.php to config.php and fill it in.');
}
$config = require $configPath;
if (!is_array($config)) {
    fail('Config must return an array');
}

$token = $config['bot_token'] ?? null;
if (!is_string($token) || strlen($token) < 40 || str_starts_with($token, 'PASTE_')) {
    fail('A valid bot_token is required');
}
$guildId = requiredSnowflake($config, 'guild_id');
$channelId = requiredSnowflake($config, 'channel_id');
$authorId = requiredSnowflake($config, 'giveaway_author_id');
$outputFile = $config['output_file'] ?? null;
if (!is_string($outputFile)) {
    fail('Invalid or missing config value: output_file');
}
$maxItems = max(1, min(20, (int) ($config['max_items'] ?? 20)));
$arguments = array_slice($argv, 1);
$unknownArguments = array_values(array_diff($arguments, ['--check']));
if ($unknownArguments !== []) {
    fail('Unknown option. Supported option: --check');
}
$checkOnly = in_array('--check', $arguments, true);

$lockPath = __DIR__ . '/.sync.lock';
$lock = fopen($lockPath, 'c');
if ($lock === false || !flock($lock, LOCK_EX | LOCK_NB)) {
    fail('Another sync is already running', 75);
}

$now = new DateTimeImmutable('now', new DateTimeZone('UTC'));
$giveaways = [];
$authorMessages = 0;
$contentVisibleMessages = 0;
$messages = fetchMessages($token, $channelId);
foreach ($messages as $message) {
    if (!is_array($message)) {
        continue;
    }
    $author = is_array($message['author'] ?? null) ? $message['author'] : [];
    if (($author['bot'] ?? false) !== true || ($author['id'] ?? null) !== $authorId) {
        continue;
    }
    $authorMessages++;
    if (
        (is_string($message['content'] ?? null) && $message['content'] !== '') ||
        (is_array($message['embeds'] ?? null) && $message['embeds'] !== []) ||
        (is_array($message['components'] ?? null) && $message['components'] !== [])
    ) {
        $contentVisibleMessages++;
    }
    $parsed = GiveawayParser::parse($message, $guildId, $channelId, $now);
    if ($parsed !== null) {
        $giveaways[] = $parsed;
    }
    if (count($giveaways) >= $maxItems) {
        break;
    }
}

if ($authorMessages === 0) {
    fail('No messages from giveaway_author_id were found. Check the author ID, channel permissions and Message Content Intent; the previous JSON was preserved.');
}
if ($contentVisibleMessages === 0) {
    fail('Messages from giveaway_author_id are visible, but their content, embeds and components are empty. Enable Message Content Intent; the previous JSON was preserved.');
}

usort($giveaways, static function (array $a, array $b): int {
    if ($a['status'] !== $b['status']) {
        return $a['status'] === 'active' ? -1 : 1;
    }
    return strcmp((string) ($b['ends_at'] ?? ''), (string) ($a['ends_at'] ?? ''));
});

$feed = [
    'version' => 1,
    'generated_at' => $now->format('Y-m-d\\TH:i:s.v\\Z'),
    'source' => ['guild_id' => $guildId, 'channel_id' => $channelId],
    'giveaways' => $giveaways,
];

if ($checkOnly) {
    flock($lock, LOCK_UN);
    fclose($lock);
    fwrite(STDOUT, sprintf(
        "[%s] Check passed: %d Discord message(s), %d content-visible message(s) from the configured author, %d giveaway(s). No file was changed.\n",
        gmdate('c'),
        count($messages),
        $contentVisibleMessages,
        count($giveaways),
    ));
    exit(0);
}

atomicWrite($outputFile, $feed);
flock($lock, LOCK_UN);
fclose($lock);
fwrite(STDOUT, sprintf("[%s] Synced %d giveaway(s).\n", gmdate('c'), count($giveaways)));
