-- Slice E: material changes on jobs the person cares about, observed by the normal refresh (never invented by a failed check).
CREATE TABLE IF NOT EXISTS job_changes (
    id INTEGER PRIMARY KEY,
    job_id INTEGER NOT NULL REFERENCES jobs(id),
    field TEXT NOT NULL,                 -- salary | location | remote | title | description | employment_type | availability | closing_date | apply_url
    old_value TEXT,
    new_value TEXT,
    source_id TEXT,                      -- which source showed it
    observed_at TEXT NOT NULL,
    material INTEGER NOT NULL DEFAULT 1,
    acknowledged_at TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_job_changes_job ON job_changes(job_id, observed_at);
