-- Application workspace: CV variants with immutable revisions, per-application CV selection and the
-- exact submitted version, Hunter requests from the dashboard, research/contact/follow-up records.
-- Nothing here deletes or rewrites user rows; existing CV documents become the first variant on first use.
CREATE TABLE IF NOT EXISTS cv_variants (
    id INTEGER PRIMARY KEY,
    variant_key TEXT NOT NULL UNIQUE,     -- stable, opaque: "cv" + 10 hex characters
    label TEXT NOT NULL,                  -- the student's own name for it, e.g. "Platform — English"
    tags_json TEXT NOT NULL DEFAULT '[]',
    is_default INTEGER NOT NULL DEFAULT 0,
    is_profile INTEGER NOT NULL DEFAULT 0, -- the CV the factual profile was built from (kind 'cv' documents)
    archived_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS cv_revisions (
    id INTEGER PRIMARY KEY,
    revision_key TEXT NOT NULL UNIQUE,    -- stable, opaque: "rev" + 10 hex characters
    variant_id INTEGER NOT NULL REFERENCES cv_variants(id),
    document_id INTEGER NOT NULL REFERENCES source_documents(id),  -- the immutable original (bytes, hash, filename)
    number INTEGER NOT NULL,              -- 1, 2, 3 within the variant; never reused
    note TEXT,
    review_json TEXT,                     -- factual claims in this file that the approved profile does not show
    created_at TEXT NOT NULL,
    UNIQUE(variant_id, number)
);
CREATE INDEX IF NOT EXISTS idx_cv_revisions_variant ON cv_revisions(variant_id, number);

ALTER TABLE applications ADD COLUMN cv_variant_key TEXT;           -- selected for this application (per job only)
ALTER TABLE applications ADD COLUMN cv_choice TEXT;                 -- default | suggested | student
ALTER TABLE applications ADD COLUMN submitted_cv_revision_key TEXT; -- the exact version the person says they sent
ALTER TABLE applications ADD COLUMN submitted_cv_choice TEXT;       -- prepared | other | unknown
ALTER TABLE applications ADD COLUMN submission_json TEXT;           -- snapshot at "I applied": vacancy, package, answers

-- Work the dashboard asked Hunter for (prepare, research, contact, follow-up draft, hand-off, CV file).
CREATE TABLE IF NOT EXISTS requests (
    id INTEGER PRIMARY KEY,
    request_id TEXT NOT NULL UNIQUE,      -- the page's id for it (kind + time), the same on every machine
    job_id INTEGER REFERENCES jobs(id),
    job_key TEXT,
    kind TEXT NOT NULL,                   -- prepare | research | contact | followup_draft | handoff | cv_file | letter | intro
    params_json TEXT,
    state TEXT NOT NULL DEFAULT 'requested',  -- requested | running | ready | needs_input | failed
    message TEXT,
    result_json TEXT,
    requested_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_requests_state ON requests(state, requested_at);

-- Versioned company/role research with sources (never overwritten; a newer version supersedes).
CREATE TABLE IF NOT EXISTS research (
    id INTEGER PRIMARY KEY,
    job_id INTEGER NOT NULL REFERENCES jobs(id),
    version INTEGER NOT NULL,
    inputs_hash TEXT,
    content_json TEXT NOT NULL,
    created_by TEXT NOT NULL,             -- rules | claude
    created_at TEXT NOT NULL,
    UNIQUE(job_id, version)
);

ALTER TABLE contacts ADD COLUMN contact_type TEXT;     -- listed | team | connection
ALTER TABLE contacts ADD COLUMN relevance TEXT;
ALTER TABLE contacts ADD COLUMN source_url TEXT;
ALTER TABLE contacts ADD COLUMN checked_at TEXT;
ALTER TABLE contacts ADD COLUMN contacted_at TEXT;
ALTER TABLE contacts ADD COLUMN status TEXT NOT NULL DEFAULT 'found';  -- found | contacted | dismissed
ALTER TABLE followups ADD COLUMN snoozed_until TEXT;
ALTER TABLE followups ADD COLUMN timezone TEXT;
