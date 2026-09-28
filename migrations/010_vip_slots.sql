-- Time-bound VIP access synchronized with each server's reserved-player list.
CREATE TABLE IF NOT EXISTS vip_slots (
    id              TEXT PRIMARY KEY,
    server_id       TEXT NOT NULL,
    steam_id        TEXT NOT NULL,
    display_name    TEXT NOT NULL DEFAULT '',
    expires_utc     REAL,
    note            TEXT NOT NULL DEFAULT '',
    created_by      TEXT NOT NULL,
    created_utc     REAL NOT NULL,
    updated_utc     REAL NOT NULL,
    synced_utc      REAL,
    sync_state      TEXT NOT NULL DEFAULT 'pending',
    sync_error      TEXT NOT NULL DEFAULT '',
    UNIQUE (server_id, steam_id)
);
CREATE INDEX IF NOT EXISTS idx_vip_slots_server_expiry
ON vip_slots(server_id, expires_utc);