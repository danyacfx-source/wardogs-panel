-- Retry queue for failed upstream ban/unban writes (mirror or primary server).
-- One row per (server, player): the latest intent wins.
CREATE TABLE IF NOT EXISTS remote_ops (
    id          TEXT PRIMARY KEY,
    kind        TEXT NOT NULL,
    server_id   TEXT NOT NULL,
    steam_id    TEXT NOT NULL,
    payload     TEXT NOT NULL DEFAULT '{}',
    attempts    INTEGER NOT NULL DEFAULT 0,
    next_try    DOUBLE PRECISION NOT NULL,
    last_error  TEXT NOT NULL DEFAULT '',
    created_utc DOUBLE PRECISION NOT NULL,
    updated_utc DOUBLE PRECISION NOT NULL,
    UNIQUE (server_id, steam_id)
);
CREATE INDEX IF NOT EXISTS idx_remote_ops_next ON remote_ops(next_try);
