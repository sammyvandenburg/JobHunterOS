-- Private per-job notes. Keyed by job id, which is stable across rediscovery
-- (jobs are upserted, never deleted), so notes are never overwritten.
CREATE TABLE IF NOT EXISTS job_notes (
    id INTEGER PRIMARY KEY,
    job_id INTEGER NOT NULL REFERENCES jobs(id),
    body TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    deleted_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_job_notes_job ON job_notes(job_id, deleted_at);
-- Favorites are recorded as events (favorited / unfavorited), like saved.
CREATE INDEX IF NOT EXISTS idx_events_job_kind_id ON events(job_id, kind, id);
