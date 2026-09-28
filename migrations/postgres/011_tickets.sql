-- Internal moderation/support tickets and staff discussion.
CREATE TABLE IF NOT EXISTS tickets (
    id              TEXT PRIMARY KEY,
    title           TEXT NOT NULL,
    description     TEXT NOT NULL DEFAULT '',
    status          TEXT NOT NULL DEFAULT 'open',
    priority        TEXT NOT NULL DEFAULT 'normal',
    player_steam_id TEXT,
    assigned_to     TEXT,
    created_by      TEXT NOT NULL,
    created_utc     DOUBLE PRECISION NOT NULL,
    updated_utc     DOUBLE PRECISION NOT NULL,
    closed_utc      DOUBLE PRECISION
);
CREATE INDEX IF NOT EXISTS idx_tickets_status_updated
ON tickets(status, updated_utc DESC);

CREATE TABLE IF NOT EXISTS ticket_comments (
    id              TEXT PRIMARY KEY,
    ticket_id       TEXT NOT NULL REFERENCES tickets(id) ON DELETE CASCADE,
    author_steam_id TEXT NOT NULL,
    body            TEXT NOT NULL,
    created_utc     DOUBLE PRECISION NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ticket_comments_ticket
ON ticket_comments(ticket_id, created_utc);