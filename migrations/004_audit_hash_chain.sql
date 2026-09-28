-- Make new site-audit records tamper-evident through a chained SHA-256 hash.
-- Existing records remain readable as an explicitly reported legacy prefix.
ALTER TABLE site_audit ADD COLUMN prev_hash TEXT DEFAULT '';
ALTER TABLE site_audit ADD COLUMN entry_hash TEXT DEFAULT '';