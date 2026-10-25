-- Slice C: the assisted application lane. One row per attempt; every step is recorded before the next starts.
CREATE TABLE IF NOT EXISTS assisted_runs (
    id INTEGER PRIMARY KEY,
    run_key TEXT NOT NULL UNIQUE,          -- "ar" + 10 hex, portable
    job_id INTEGER NOT NULL REFERENCES jobs(id),
    application_id INTEGER REFERENCES applications(id),
    capability TEXT NOT NULL,              -- assisted_fill | reviewed_submit (the lane this run may use)
    destination TEXT NOT NULL,             -- the exact page opened
    state TEXT NOT NULL,                   -- preflight | permitted | filled | approved | submitting | submitted | user_submitted | uncertain | failed | cancelled
    permission_json TEXT,                  -- what the person allowed (fill, upload the CV, which CV version), when
    readback_json TEXT,                    -- what the form really held after filling (values read from the page)
    readback_hash TEXT,
    approval_hash TEXT,                    -- sha256 of the single-use code; the code itself is shown once and never stored
    approval_expires_at TEXT,
    approved_at TEXT,
    consumed_at TEXT,                      -- when the code was used (once)
    lease_until TEXT,                      -- a submit in flight; a dead process leaves "uncertain", never a second click
    receipt_json TEXT,                     -- final URL, confirmation text and id, screenshot path, time
    error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_assisted_runs_job ON assisted_runs(job_id, created_at);
