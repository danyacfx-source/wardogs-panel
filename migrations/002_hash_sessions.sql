-- Session tokens are now stored as SHA-256 digests by app.db.
-- Remove legacy plaintext tokens rather than leaving replayable credentials in
-- an existing database. Users will simply sign in again after the upgrade.
DELETE FROM sessions WHERE length(token) <> 64;