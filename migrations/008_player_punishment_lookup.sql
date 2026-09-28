-- Accelerate player-card punishment history queries over the tamper-evident audit.
CREATE INDEX IF NOT EXISTS idx_site_audit_player_lookup
ON site_audit(detail, event, timestamp_utc DESC);