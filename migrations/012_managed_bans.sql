-- Panel-managed temporary bans with automatic expiry reconciliation.
CREATE TABLE IF NOT EXISTS managed_bans (
    id              TEXT PRIMARY KEY,
    server_id       TEXT NOT NULL,
    steam_id        TEXT NOT NULL,
    display_name    TEXT NOT NULL DEFAULT '',
    reason          TEXT NOT NULL DEFAULT '',
    expires_utc     REAL,
    created_by      TEXT NOT NULL,
    created_utc     REAL NOT NULL,
    updated_utc     REAL NOT NULL,
    sync_state      TEXT NOT NULL DEFAULT 'active',
    sync_error      TEXT NOT NULL DEFAULT '',
    UNIQUE (server_id, steam_id)
);
CREATE INDEX IF NOT EXISTS idx_managed_bans_expiry
ON managed_bans(expires_utc, sync_state);