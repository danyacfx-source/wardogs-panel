<?php
declare(strict_types=1);

return [
    // Никогда не размещайте этот файл внутри public_html и не отправляйте токен кому-либо.
    'bot_token' => 'PASTE_NEW_DISCORD_BOT_TOKEN_HERE',
    'guild_id' => '403186008579112973',
    'channel_id' => '1546639691607183390',

    // ID автора сообщений о розыгрышах (бот WARDOGS RU), а не имя бота.
    'giveaway_author_id' => 'PASTE_WARDOGS_RU_BOT_USER_ID_HERE',

    // Страница /giveaways/ читает этот JSON. Укажите абсолютный путь внутри корня сайта.
    'output_file' => '/var/www/u1234567/data/www/wardogs-ru.ru/data/giveaways.json',
    'max_items' => 20,
];
