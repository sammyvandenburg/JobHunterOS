-- Slice B: Today, automation modes and bounded auto-preparation.
ALTER TABLE applications ADD COLUMN opened_at TEXT;      -- when the person first opened the prepared application (page or CLI)
ALTER TABLE requests ADD COLUMN origin TEXT NOT NULL DEFAULT 'user';  -- user (page or command line) | auto (Hunter's bounded preparation)
