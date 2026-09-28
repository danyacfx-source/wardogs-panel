-- Accumulated playtime and session counters per server/player.
CREATE TABLE IF NOT EXISTS player_presence (
    server_id                  TEXT NOT NULL,
    steam_id                  TEXT NOT NULL,
    total_seconds             REAL NOT NULL DEFAULT 0,
    session_count             INTEGER NOT NULL DEFAULT 0,
    current_session_started_utc REAL,
    last_seen_utc             REAL NOT NULL,
    online                    INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (server_id, steam_id)
);
CREATE INDEX IF NOT EXISTS idx_player_presence_player
ON player_presence(steam_id, last_seen_utc DESC);