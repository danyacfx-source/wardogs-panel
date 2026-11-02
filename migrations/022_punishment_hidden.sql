-- Скрытие записей истории наказаний в карточке игрока. Сама запись site_audit
-- не удаляется: журнал хешируется цепочкой (prev_hash/entry_hash), и любое
-- удаление строки сломало бы проверку целостности.
CREATE TABLE IF NOT EXISTS punishment_hidden (
    audit_id    INTEGER PRIMARY KEY,
    steam_id    TEXT NOT NULL,
    hidden_by   TEXT NOT NULL,
    hidden_utc  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_punishment_hidden_player ON punishment_hidden(steam_id);
