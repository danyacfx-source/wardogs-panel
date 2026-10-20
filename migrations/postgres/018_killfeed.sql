-- Kill feed: every kill the game pushes to /api/killfeed/{server}.
CREATE TABLE IF NOT EXISTS killfeed_events (
    server_id    TEXT NOT NULL,
    event_id     TEXT NOT NULL,
    received_utc DOUBLE PRECISION NOT NULL,
    match_id     TEXT DEFAULT '',
    event_time   DOUBLE PRECISION NOT NULL,
    map          TEXT DEFAULT '',
    killer_id    TEXT,
    killer_name  TEXT,
    killer_faction TEXT,
    victim_id    TEXT NOT NULL,
    victim_name  TEXT DEFAULT '',
    victim_faction TEXT,
    cause        TEXT DEFAULT '',
    distance_m   DOUBLE PRECISION,
    headshot     INTEGER NOT NULL DEFAULT 0,
    suicide      INTEGER NOT NULL DEFAULT 0,
    teamkill     INTEGER NOT NULL DEFAULT 0,
    tags         TEXT DEFAULT '[]',
    PRIMARY KEY (server_id, event_id)
);
CREATE INDEX IF NOT EXISTS idx_killfeed_feed ON killfeed_events(server_id, received_utc DESC);
CREATE INDEX IF NOT EXISTS idx_killfeed_team ON killfeed_events(server_id, teamkill, received_utc DESC);
