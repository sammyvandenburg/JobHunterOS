-- Gate A: the person's own texts live apart from Hunter's suggestions; source refresh history; submission destination.
CREATE TABLE IF NOT EXISTS user_texts (
    id INTEGER PRIMARY KEY,
    job_id INTEGER NOT NULL REFERENCES jobs(id),
    kind TEXT NOT NULL,                   -- answer | intro | letter | question
    key TEXT NOT NULL,                    -- the answer id (a + 8 hex) or the kind
    question TEXT,                        -- the question's wording (answers and questions)
    text TEXT,                            -- what the person wrote (NULL for a question row)
    rev INTEGER NOT NULL DEFAULT 0,
    limit_n INTEGER,                      -- the form's limit, when known
    limit_unit TEXT,                      -- characters | words
    scope TEXT NOT NULL DEFAULT 'job',    -- job (this application) | reusable (approved for reuse in its kind)
    language TEXT,
    approved_at TEXT,
    expires_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(job_id, kind, key)
);
CREATE INDEX IF NOT EXISTS idx_user_texts_job ON user_texts(job_id, kind);
ALTER TABLE applications ADD COLUMN submitted_destination TEXT;
ALTER TABLE applications ADD COLUMN submitted_cv_tombstone_json TEXT;  -- what a deleted CV version was (hash, name), kept for history
ALTER TABLE watch_sources ADD COLUMN etag TEXT;
ALTER TABLE watch_sources ADD COLUMN last_modified TEXT;
ALTER TABLE watch_sources ADD COLUMN next_allowed_at TEXT;   -- a Retry-After the source asked for
ALTER TABLE watch_sources ADD COLUMN last_fetch_at TEXT;
ALTER TABLE watch_sources ADD COLUMN last_bytes INTEGER;
