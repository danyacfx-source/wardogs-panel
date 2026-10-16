<?php
declare(strict_types=1);

final class GiveawayParser
{
    private const MAX_TITLE = 180;
    private const MAX_DESCRIPTION = 600;

    /**
     * @param array<string, mixed> $message
     * @return array<string, mixed>|null
     */
    public static function parse(array $message, string $guildId, string $channelId, DateTimeImmutable $now): ?array
    {
        $messageId = self::snowflake($message['id'] ?? null);
        if ($messageId === null) {
            return null;
        }

        $embed = is_array($message['embeds'][0] ?? null) ? $message['embeds'][0] : [];
        $parts = [
            self::string($message['content'] ?? null),
            self::string($embed['title'] ?? null),
            self::string($embed['description'] ?? null),
        ];
        foreach (($embed['fields'] ?? []) as $field) {
            if (is_array($field)) {
                $parts[] = self::string($field['name'] ?? null);
                $parts[] = self::string($field['value'] ?? null);
            }
        }
        if (is_array($embed['footer'] ?? null)) {
            $parts[] = self::string($embed['footer']['text'] ?? null);
        }

        $text = implode("\n", array_filter($parts));
        if ($text === '' || preg_match('/(?:розыгрыш|участв|победител|копи[яий])/iu', $text) !== 1) {
            return null;
        }

        $mentions = [];
        foreach (($message['mentions'] ?? []) as $mention) {
            if (!is_array($mention)) {
                continue;
            }
            $id = self::snowflake($mention['id'] ?? null);
            $name = self::string($mention['global_name'] ?? $mention['display_name'] ?? $mention['username'] ?? null);
            if ($id !== null && $name !== '') {
                $mentions[$id] = $name;
            }
        }

        $title = self::clean(self::string($embed['title'] ?? null), $mentions, self::MAX_TITLE);
        if ($title === '') {
            $title = 'Розыгрыш WARDOGS RU';
        }

        $descriptionSource = self::string($embed['description'] ?? $message['content'] ?? null);
        $description = self::description($descriptionSource, $mentions);
        if ($description === '') {
            $description = 'Откройте сообщение в Discord, чтобы узнать условия и принять участие.';
        }

        $endsAt = self::endTime($text);
        $hasButton = false;
        $disabled = false;
        $buttonLabel = 'Участвовать';
        foreach (($message['components'] ?? []) as $row) {
            if (!is_array($row)) {
                continue;
            }
            foreach (($row['components'] ?? []) as $component) {
                if (!is_array($component) || (int) ($component['type'] ?? 0) !== 2) {
                    continue;
                }
                $hasButton = true;
                $label = self::clean(self::string($component['label'] ?? null), $mentions, 40);
                if ($label !== '') {
                    $buttonLabel = $label;
                }
                if (($component['disabled'] ?? false) !== true) {
                    $disabled = false;
                }
            }
        }

        $status = ($hasButton && $disabled) || ($endsAt !== null && $endsAt <= $now) ? 'ended' : 'active';

        return [
            'id' => $messageId,
            'giveaway_id' => self::match($text, '/(?:ID\s*розыгрыша|розыгрыш\s*№?)\s*[:#№]?\s*(\d{1,12})/iu'),
            'title' => $title,
            'description' => $description,
            'ends_at' => $endsAt?->setTimezone(new DateTimeZone('UTC'))->format('Y-m-d\\TH:i:s.v\\Z'),
            'winners_count' => self::number($text, '/(?:кол-во\s+победителей|победител(?:ей|я))\D{0,20}(\d{1,6})/iu'),
            'participants_count' => self::number($text, '/участников\D{0,12}(\d{1,8})/iu'),
            'host' => self::host($text, $mentions),
            'status' => $status,
            'message_url' => sprintf('https://discord.com/channels/%s/%s/%s', $guildId, $channelId, $messageId),
            'button_label' => $buttonLabel,
            'updated_at' => self::isoDate(self::string($message['edited_timestamp'] ?? $message['timestamp'] ?? null), $now),
        ];
    }

    private static function string(mixed $value): string
    {
        return is_string($value) ? $value : '';
    }

    private static function snowflake(mixed $value): ?string
    {
        $value = self::string($value);
        return preg_match('/^\d{17,20}$/D', $value) === 1 ? $value : null;
    }

    /** @param array<string, string> $mentions */
    private static function clean(string $value, array $mentions, int $limit): string
    {
        $value = preg_replace_callback('/<@!?(\d{17,20})>/', static function (array $match) use ($mentions): string {
            return $mentions[$match[1]] ?? 'участник Discord';
        }, $value) ?? '';
        $value = preg_replace('/<a?:[A-Za-z0-9_]+:\d{17,20}>/', '', $value) ?? '';
        $value = preg_replace('/<t:\d{9,12}(?::[A-Za-z])?>/', '', $value) ?? '';
        $value = str_replace(['**', '__', '~~', '`'], '', $value);
        $value = preg_replace('/[\x00-\x08\x0B\x0C\x0E-\x1F\x7F]/u', '', $value) ?? '';
        $value = preg_replace('/[ \t]+/u', ' ', $value) ?? '';
        $value = trim($value);
        return self::truncate($value, $limit);
    }

    /** @param array<string, string> $mentions */
    private static function description(string $value, array $mentions): string
    {
        $lines = preg_split('/\R/u', $value) ?: [];
        $kept = [];
        foreach ($lines as $line) {
            $line = trim($line);
            if ($line === '' || preg_match('/^(?:⏰|📅|🏆|👑|участников|ID\s*розыгрыша)/iu', $line) === 1) {
                continue;
            }
            $kept[] = $line;
            if (count($kept) === 3) {
                break;
            }
        }
        return self::clean(implode(' ', $kept), $mentions, self::MAX_DESCRIPTION);
    }

    private static function endTime(string $text): ?DateTimeImmutable
    {
        if (preg_match_all('/<t:(\d{9,12})(?::[A-Za-z])?>/', $text, $matches) < 1) {
            return null;
        }
        $timestamp = (int) $matches[1][0];
        if ($timestamp < 946684800 || $timestamp > 4102444800) {
            return null;
        }
        return (new DateTimeImmutable('@' . $timestamp))->setTimezone(new DateTimeZone('UTC'));
    }

    private static function match(string $text, string $pattern): ?string
    {
        return preg_match($pattern, $text, $match) === 1 ? $match[1] : null;
    }

    private static function number(string $text, string $pattern): ?int
    {
        $value = self::match($text, $pattern);
        return $value === null ? null : (int) $value;
    }

    /** @param array<string, string> $mentions */
    private static function host(string $text, array $mentions): ?string
    {
        if (preg_match('/(?:устроил|организатор)\s*:\s*([^\r\n]+)/iu', $text, $match) !== 1) {
            return null;
        }
        $host = self::clean($match[1], $mentions, 100);
        return $host !== '' ? $host : null;
    }

    private static function isoDate(string $value, DateTimeImmutable $fallback): string
    {
        try {
            $date = $value !== '' ? new DateTimeImmutable($value) : $fallback;
        } catch (Throwable) {
            $date = $fallback;
        }
        return $date->setTimezone(new DateTimeZone('UTC'))->format('Y-m-d\\TH:i:s.v\\Z');
    }

    private static function truncate(string $value, int $limit): string
    {
        if (function_exists('mb_strlen') && function_exists('mb_substr')) {
            return mb_strlen($value, 'UTF-8') <= $limit ? $value : mb_substr($value, 0, $limit, 'UTF-8');
        }
        return strlen($value) <= $limit ? $value : substr($value, 0, $limit);
    }
}
