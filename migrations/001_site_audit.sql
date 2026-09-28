-- Базовая миграция локального аудита и доступности серверов.
-- Применяется автоматически app.db.init() и хранится в schema_migrations.
CREATE TABLE IF NOT EXISTS site_audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp_utc REAL NOT NULL,
    actor_steam_id TEXT,
    server_id TEXT,
    event TEXT NOT NULL,
    detail TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_site_audit_time ON site_audit(timestamp_utc DESC);
CREATE TABLE IF NOT EXISTS server_health (
    server_id TEXT PRIMARY KEY,
    online INTEGER NOT NULL,
    detail TEXT DEFAULT '',
    checked_utc REAL NOT NULL
);
