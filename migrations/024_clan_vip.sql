-- VIP клана: пул мест, выданные места и заявки на оплату пула.
CREATE TABLE IF NOT EXISTS clan_vip_pools (
    clan_id     TEXT PRIMARY KEY,
    seats       INTEGER NOT NULL DEFAULT 0,
    expires_utc REAL NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS clan_vip_grants (
    clan_id     TEXT NOT NULL,
    steam_id    TEXT NOT NULL,
    granted_by  TEXT NOT NULL DEFAULT '',
    created_utc REAL NOT NULL,
    PRIMARY KEY (clan_id, steam_id)
);

CREATE TABLE IF NOT EXISTS clan_vip_orders (
    id              TEXT PRIMARY KEY,
    clan_id         TEXT NOT NULL,
    buyer_steam_id  TEXT NOT NULL,
    product_id      TEXT NOT NULL,
    pack_seats      INTEGER NOT NULL,
    amount          INTEGER NOT NULL,
    status          TEXT NOT NULL,
    created_utc     REAL NOT NULL,
    paid_utc        REAL
);
CREATE INDEX IF NOT EXISTS idx_clan_vip_orders_clan ON clan_vip_orders(clan_id, created_utc DESC);
