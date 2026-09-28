-- Multi-bot control plane. Tokens are stored only as SHA-256 hashes.
CREATE TABLE IF NOT EXISTS bot_integrations (
    bot_id        TEXT PRIMARY KEY,
    name          TEXT NOT NULL,
    guild_id      TEXT DEFAULT '',
    bot_user_id   TEXT DEFAULT '',
    base_url      TEXT DEFAULT '',
    token_hash    TEXT NOT NULL,
    scopes        TEXT NOT NULL DEFAULT '[]',
    enabled       INTEGER NOT NULL DEFAULT 1,
    status        TEXT NOT NULL DEFAULT 'pending',
    version       TEXT DEFAULT '',
    detail        TEXT DEFAULT '',
    metadata      TEXT NOT NULL DEFAULT '{}',
    created_utc   DOUBLE PRECISION NOT NULL,
    updated_utc   DOUBLE PRECISION NOT NULL,
    last_seen_utc DOUBLE PRECISION
);
CREATE INDEX IF NOT EXISTS idx_bot_integrations_seen
ON bot_integrations(enabled, last_seen_utc DESC);

CREATE TABLE IF NOT EXISTS bot_events (
    id          BIGSERIAL PRIMARY KEY,
    bot_id      TEXT NOT NULL REFERENCES bot_integrations(bot_id),
    event_id    TEXT NOT NULL,
    event_type  TEXT NOT NULL,
    payload     TEXT NOT NULL DEFAULT '{}',
    created_utc DOUBLE PRECISION NOT NULL,
    UNIQUE (bot_id, event_id)
);
CREATE INDEX IF NOT EXISTS idx_bot_events_time
ON bot_events(bot_id, created_utc DESC);
