-- Panel-side ban queue: targets that are offline get banned on first sighting.
CREATE TABLE IF NOT EXISTS queued_bans (
    steam_id        TEXT PRIMARY KEY,
    display_name    TEXT NOT NULL DEFAULT '',
    reason          TEXT NOT NULL DEFAULT '',
    expires_utc     REAL,
    created_by      TEXT NOT NULL,
    created_utc     REAL NOT NULL,
    updated_utc     REAL NOT NULL,
    applied_servers TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_queued_bans_created
ON queued_bans(created_utc);
