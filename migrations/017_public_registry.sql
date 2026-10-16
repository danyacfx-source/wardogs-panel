-- Общие реестры для публичных JSON-фидов (розыгрыши, кланы).
CREATE TABLE IF NOT EXISTS public_registry (
  key TEXT PRIMARY KEY,
  payload_json TEXT NOT NULL,
  updated_utc REAL NOT NULL
);
