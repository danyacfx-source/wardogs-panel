-- PostgreSQL baseline schema for the current application state.
-- The version matches the latest SQLite migration so both backends share
-- the same schema_migrations contract.

CREATE TABLE IF NOT EXISTS schema_migrations (
    version    INTEGER PRIMARY KEY,
    applied_utc DOUBLE PRECISION NOT NULL
);

CREATE TABLE IF NOT EXISTS users (
    steam_id       TEXT PRIMARY KEY,
    persona        TEXT,
    last_seen_utc  DOUBLE PRECISION,
    session_version INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS bindings (
    user_id       TEXT PRIMARY KEY,
    discord_id    TEXT UNIQUE,
    discord_name  TEXT,
    guild_member  INTEGER NOT NULL DEFAULT 0,
    roles         TEXT NOT NULL DEFAULT '[]',
    updated_utc   DOUBLE PRECISION NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS sessions (
    token          TEXT PRIMARY KEY,
    user_id        TEXT,
    created_utc    DOUBLE PRECISION,
    expires_utc    DOUBLE PRECISION,
    session_version INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);

CREATE TABLE IF NOT EXISTS oauth_states (
    state       TEXT PRIMARY KEY,
    provider    TEXT NOT NULL,
    user_id     TEXT,
    created_utc DOUBLE PRECISION NOT NULL,
    expires_utc DOUBLE PRECISION NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_oauth_states_expiry ON oauth_states(expires_utc);

CREATE TABLE IF NOT EXISTS idempotency_keys (
    actor_steam_id TEXT NOT NULL,
    idem_key       TEXT NOT NULL,
    fingerprint    TEXT NOT NULL,
    created_utc    DOUBLE PRECISION NOT NULL,
    expires_utc    DOUBLE PRECISION NOT NULL,
    status_code    INTEGER,
    response_json  TEXT,
    PRIMARY KEY (actor_steam_id, idem_key)
);
CREATE INDEX IF NOT EXISTS idx_idempotency_expiry ON idempotency_keys(expires_utc);

CREATE TABLE IF NOT EXISTS role_permissions (
    role_ref    TEXT PRIMARY KEY,
    perms       TEXT NOT NULL DEFAULT '[]',
    updated_utc DOUBLE PRECISION NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS population_samples (
    server_id   TEXT NOT NULL,
    bucket_utc  BIGINT NOT NULL,
    players     INTEGER NOT NULL DEFAULT 0,
    max_players INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (server_id, bucket_utc)
);
CREATE INDEX IF NOT EXISTS idx_population_time ON population_samples(bucket_utc);

CREATE TABLE IF NOT EXISTS player_activity (
    server_id TEXT NOT NULL,
    steam_id  TEXT NOT NULL,
    name      TEXT,
    kills     INTEGER NOT NULL DEFAULT 0,
    deaths    INTEGER NOT NULL DEFAULT 0,
    cash      INTEGER NOT NULL DEFAULT 0,
    ping_ms   INTEGER NOT NULL DEFAULT 0,
    faction   TEXT,
    seen_utc  DOUBLE PRECISION,
    PRIMARY KEY (server_id, steam_id)
);
CREATE INDEX IF NOT EXISTS idx_player_activity_seen ON player_activity(seen_utc);

CREATE TABLE IF NOT EXISTS site_audit (
    id             BIGSERIAL PRIMARY KEY,
    timestamp_utc  DOUBLE PRECISION NOT NULL,
    actor_steam_id TEXT,
    server_id      TEXT,
    event          TEXT NOT NULL,
    detail         TEXT NOT NULL DEFAULT '',
    prev_hash      TEXT NOT NULL DEFAULT '',
    entry_hash     TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_site_audit_time ON site_audit(timestamp_utc DESC);

CREATE TABLE IF NOT EXISTS server_health (
    server_id   TEXT PRIMARY KEY,
    online      INTEGER NOT NULL,
    detail      TEXT NOT NULL DEFAULT '',
    checked_utc DOUBLE PRECISION NOT NULL
);