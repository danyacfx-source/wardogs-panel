-- Player profile cards: preserve first/last observation timestamps.
ALTER TABLE player_activity ADD COLUMN IF NOT EXISTS first_seen_utc DOUBLE PRECISION;
ALTER TABLE player_activity ADD COLUMN IF NOT EXISTS last_seen_utc DOUBLE PRECISION;
UPDATE player_activity
SET first_seen_utc = COALESCE(seen_utc, 0),
    last_seen_utc = COALESCE(seen_utc, 0)
WHERE first_seen_utc IS NULL OR last_seen_utc IS NULL;