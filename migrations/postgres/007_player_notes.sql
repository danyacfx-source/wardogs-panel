-- Private staff notes attached to retained player profiles.
CREATE TABLE IF NOT EXISTS player_notes (
    id              TEXT PRIMARY KEY,
    steam_id        TEXT NOT NULL,
    author_steam_id TEXT NOT NULL,
    note            TEXT NOT NULL,
    created_utc     DOUBLE PRECISION NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_player_notes_player_time
ON player_notes(steam_id, created_utc DESC);