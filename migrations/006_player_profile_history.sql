-- Player profile cards: preserve first/last observation timestamps.
ALTER TABLE player_activity ADD COLUMN first_seen_utc REAL;
ALTER TABLE player_activity ADD COLUMN last_seen_utc REAL;
UPDATE player_activity
SET first_seen_utc = COALESCE(seen_utc, 0),
    last_seen_utc = COALESCE(seen_utc, 0)
WHERE first_seen_utc IS NULL OR last_seen_utc IS NULL;