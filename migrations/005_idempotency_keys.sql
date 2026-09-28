-- Persist dangerous-operation request keys so duplicate retries are safe
-- across application workers and process restarts.
CREATE TABLE IF NOT EXISTS idempotency_keys (
    actor_steam_id TEXT NOT NULL,
    idem_key       TEXT NOT NULL,
    fingerprint    TEXT NOT NULL,
    created_utc    REAL NOT NULL,
    expires_utc    REAL NOT NULL,
    status_code    INTEGER,
    response_json  TEXT,
    PRIMARY KEY (actor_steam_id, idem_key)
);
CREATE INDEX IF NOT EXISTS idx_idempotency_expiry ON idempotency_keys(expires_utc);