<?php
declare(strict_types=1);

return [
    // Generate with: openssl rand -hex 32
    'secret' => 'PASTE_A_RANDOM_64_CHARACTER_HEX_SECRET_HERE',

    // Optional: public IPv4/IPv6 address of the VPS running the bot.
    // Leave empty when the bot host does not have a fixed outbound address.
    'allowed_ip' => '',
];
