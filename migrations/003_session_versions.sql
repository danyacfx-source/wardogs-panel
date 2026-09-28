-- Rotate every active session when a user's authorization state changes.
ALTER TABLE users ADD COLUMN session_version INTEGER NOT NULL DEFAULT 0;
ALTER TABLE sessions ADD COLUMN session_version INTEGER NOT NULL DEFAULT 0;