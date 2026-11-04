-- История для карточки игрока: время по дням, сессии и подробности банов.
CREATE TABLE IF NOT EXISTS player_daily_activity (
    steam_id      TEXT NOT NULL,
    day           TEXT NOT NULL,
    server_id     TEXT NOT NULL,
    play_seconds  DOUBLE PRECISION NOT NULL DEFAULT 0,
    seed_seconds  DOUBLE PRECISION NOT NULL DEFAULT 0,
    PRIMARY KEY (steam_id, day, server_id)
);

CREATE TABLE IF NOT EXISTS player_sessions (
    server_id     TEXT NOT NULL,
    steam_id      TEXT NOT NULL,
    started_utc   DOUBLE PRECISION NOT NULL,
    last_seen_utc DOUBLE PRECISION NOT NULL,
    seed_seconds  DOUBLE PRECISION NOT NULL DEFAULT 0,
    PRIMARY KEY (server_id, steam_id, started_utc)
);
CREATE INDEX IF NOT EXISTS idx_player_sessions_player ON player_sessions(steam_id, started_utc DESC);
CREATE INDEX IF NOT EXISTS idx_player_sessions_seen ON player_sessions(last_seen_utc);

CREATE TABLE IF NOT EXISTS punishment_details (
    id          TEXT PRIMARY KEY,
    kind        TEXT NOT NULL,
    steam_id    TEXT NOT NULL,
    server_id   TEXT,
    reason      TEXT NOT NULL DEFAULT '',
    expires_utc DOUBLE PRECISION,
    created_by  TEXT,
    created_utc DOUBLE PRECISION NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_punishment_details_player ON punishment_details(steam_id, created_utc DESC);

CREATE TABLE IF NOT EXISTS player_ratings (
    steam_id    TEXT PRIMARY KEY,
    rating      DOUBLE PRECISION NOT NULL,
    games       INTEGER NOT NULL DEFAULT 0,
    updated_utc DOUBLE PRECISION NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_player_ratings_rating ON player_ratings(rating);

CREATE TABLE IF NOT EXISTS match_results (
    server_id       TEXT NOT NULL,
    event_id        TEXT NOT NULL,
    match_id        TEXT NOT NULL DEFAULT '',
    map             TEXT NOT NULL DEFAULT '',
    started_utc     DOUBLE PRECISION,
    ended_utc       DOUBLE PRECISION NOT NULL,
    winning_faction TEXT NOT NULL DEFAULT '',
    faction_scores  TEXT NOT NULL DEFAULT '[]',
    players_count   INTEGER,
    PRIMARY KEY (server_id, event_id)
);
CREATE INDEX IF NOT EXISTS idx_match_results_time ON match_results(ended_utc DESC);

CREATE TABLE IF NOT EXISTS server_match_state (
    server_id   TEXT PRIMARY KEY,
    started_utc DOUBLE PRECISION NOT NULL
);

CREATE TABLE IF NOT EXISTS player_names (
    steam_id       TEXT NOT NULL,
    name           TEXT NOT NULL,
    first_seen_utc DOUBLE PRECISION NOT NULL,
    last_seen_utc  DOUBLE PRECISION NOT NULL,
    PRIMARY KEY (steam_id, name)
);
CREATE INDEX IF NOT EXISTS idx_player_names_seen ON player_names(last_seen_utc DESC);
CREATE INDEX IF NOT EXISTS idx_player_daily_day ON player_daily_activity(day);
CREATE INDEX IF NOT EXISTS idx_killfeed_time ON killfeed_events(event_time);

CREATE TABLE IF NOT EXISTS player_match_results (
    server_id TEXT NOT NULL,
    event_id  TEXT NOT NULL,
    steam_id  TEXT NOT NULL,
    match_id  TEXT NOT NULL DEFAULT '',
    map       TEXT NOT NULL DEFAULT '',
    faction   TEXT NOT NULL DEFAULT '',
    won       INTEGER,
    ended_utc DOUBLE PRECISION NOT NULL,
    PRIMARY KEY (server_id, event_id, steam_id)
);
CREATE INDEX IF NOT EXISTS idx_player_match_results_player ON player_match_results(steam_id, ended_utc DESC);
CREATE INDEX IF NOT EXISTS idx_player_match_results_time ON player_match_results(ended_utc);

CREATE INDEX IF NOT EXISTS idx_killfeed_killer ON killfeed_events(killer_id, event_time DESC);
CREATE INDEX IF NOT EXISTS idx_killfeed_victim ON killfeed_events(victim_id, event_time DESC);

-- Уровень VIP: 1 — VIP, 2 — плюс личный тикет, 3 — плюс объявление о входе в игре.
ALTER TABLE vip_slots ADD COLUMN tier INTEGER NOT NULL DEFAULT 1;
