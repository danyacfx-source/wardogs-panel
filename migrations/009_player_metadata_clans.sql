-- Panel-owned player labels and clan directory.
CREATE TABLE IF NOT EXISTS player_tags (
    id              TEXT PRIMARY KEY,
    steam_id        TEXT NOT NULL,
    label           TEXT NOT NULL,
    color           TEXT NOT NULL DEFAULT 'gray',
    created_by      TEXT NOT NULL,
    created_utc     REAL NOT NULL,
    UNIQUE (steam_id, label)
);
CREATE INDEX IF NOT EXISTS idx_player_tags_player
ON player_tags(steam_id, created_utc);

CREATE TABLE IF NOT EXISTS clans (
    id              TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    tag             TEXT NOT NULL UNIQUE,
    color           TEXT NOT NULL DEFAULT 'orange',
    description     TEXT NOT NULL DEFAULT '',
    created_by      TEXT NOT NULL,
    created_utc     REAL NOT NULL,
    updated_utc     REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS clan_members (
    clan_id         TEXT NOT NULL,
    steam_id        TEXT NOT NULL UNIQUE,
    member_role     TEXT NOT NULL DEFAULT 'Участник',
    joined_utc      REAL NOT NULL,
    PRIMARY KEY (clan_id, steam_id),
    FOREIGN KEY (clan_id) REFERENCES clans(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_clan_members_clan
ON clan_members(clan_id, joined_utc);