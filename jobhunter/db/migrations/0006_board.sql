-- The personal dashboard (a private Claude Artifact page): which page actions Hunter has already applied.
CREATE TABLE IF NOT EXISTS board_sync (
    job_key TEXT PRIMARY KEY,
    canonical_key TEXT,
    synced_rev INTEGER NOT NULL DEFAULT 0,
    note_id INTEGER,
    synced_at TEXT
);
