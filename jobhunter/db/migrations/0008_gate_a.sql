-- Gate A: stable portable identities, request leases, and nothing else. Adds columns only; older code ignores them.
ALTER TABLE contacts ADD COLUMN contact_key TEXT;          -- "ct" + 10 hex, the same on every machine (row ids are not)
CREATE UNIQUE INDEX IF NOT EXISTS idx_contacts_key ON contacts(contact_key) WHERE contact_key IS NOT NULL;
ALTER TABLE requests ADD COLUMN claimed_by TEXT;           -- which run holds the lease
ALTER TABLE requests ADD COLUMN lease_until TEXT;          -- a running request past this time is stale, never "running forever"
ALTER TABLE requests ADD COLUMN heartbeat_at TEXT;
ALTER TABLE requests ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0;
