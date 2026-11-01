-- Признак активного VIP-членства из сид-бота. Пишет только бот, читает панель.
-- Не пересекается с vip_slots: RCON-синхронизация панели эту таблицу не трогает.
CREATE TABLE IF NOT EXISTS seed_vip_active (
    steam_id     TEXT PRIMARY KEY,
    active_until DOUBLE PRECISION,
    updated_utc  DOUBLE PRECISION NOT NULL
);
