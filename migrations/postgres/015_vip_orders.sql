CREATE TABLE IF NOT EXISTS vip_orders (
    id TEXT PRIMARY KEY,
    buyer_steam_id TEXT NOT NULL,
    server_id TEXT NOT NULL,
    plan_id TEXT NOT NULL,
    amount INTEGER NOT NULL,
    months INTEGER NOT NULL,
    seats INTEGER NOT NULL,
    recipients TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'awaiting_payment',
    created_utc DOUBLE PRECISION NOT NULL,
    actor_steam_id TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_vip_orders_created ON vip_orders(created_utc);
