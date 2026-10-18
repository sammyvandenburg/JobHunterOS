-- JobHunterOS initial schema. Times are ISO-8601 UTC strings unless noted.

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- Original user materials, stored byte-for-byte on disk; this table holds metadata only.
CREATE TABLE IF NOT EXISTS source_documents (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL,                 -- cv | linkedin | github | project | other
    label TEXT,
    original_filename TEXT,
    media_type TEXT,
    byte_size INTEGER NOT NULL,
    sha256 TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    stored_path TEXT NOT NULL,          -- immutable original
    text_path TEXT,                     -- derived extracted text (separate file)
    extraction_status TEXT NOT NULL,    -- ok | empty | unsupported | failed
    extraction_notes TEXT,
    source_url TEXT,                    -- e.g. LinkedIn profile URL (a URL alone is not content)
    superseded_by INTEGER REFERENCES source_documents(id),
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_source_documents_kind ON source_documents(kind, created_at);

CREATE TABLE IF NOT EXISTS profile_versions (
    id INTEGER PRIMARY KEY,
    version INTEGER NOT NULL,
    status TEXT NOT NULL,               -- draft | approved | superseded
    summary_text TEXT,
    facts_json TEXT NOT NULL,           -- list of facts with evidence references
    targets_json TEXT,                  -- target direction, level, stretch
    document_ids_json TEXT NOT NULL,
    built_by TEXT NOT NULL,             -- rules | claude | user
    created_at TEXT NOT NULL,
    approved_at TEXT
);

CREATE TABLE IF NOT EXISTS profile_questions (
    id INTEGER PRIMARY KEY,
    profile_version_id INTEGER REFERENCES profile_versions(id),
    key TEXT NOT NULL,
    question TEXT NOT NULL,
    reason TEXT,
    status TEXT NOT NULL DEFAULT 'open',   -- open | answered | skipped
    answer TEXT,
    answered_at TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_profile_questions_status ON profile_questions(status);

CREATE TABLE IF NOT EXISTS preference_changes (
    id INTEGER PRIMARY KEY,
    key TEXT NOT NULL,
    old_json TEXT,
    new_json TEXT,
    reason TEXT,
    confirmed INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    reverted_at TEXT
);

CREATE TABLE IF NOT EXISTS employers (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    canonical_name TEXT NOT NULL,
    website TEXT,
    careers_url TEXT,
    ats_kind TEXT,                      -- greenhouse | lever | ashby | other | NULL
    ats_token TEXT,
    ats_region TEXT,                    -- us | eu (Lever) ; NULL for default
    regions_json TEXT,                  -- ["na","eu"]
    country_codes_json TEXT,
    origin TEXT NOT NULL,               -- catalogue | discovered | user
    discovered_via TEXT,                -- source id that introduced it
    catalogue_checked_at TEXT,          -- date the catalogue entry was validated
    first_seen_at TEXT NOT NULL,
    last_checked_at TEXT,
    last_check_status TEXT,
    notes TEXT,
    UNIQUE(canonical_name)
);

CREATE TABLE IF NOT EXISTS watch_sources (
    id TEXT PRIMARY KEY,                -- e.g. greenhouse:gitlab, remotive:devops
    kind TEXT NOT NULL,                 -- greenhouse | lever | ashby | remotive | arbeitnow | adzuna | import
    employer_id INTEGER REFERENCES employers(id),
    config_json TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    origin TEXT NOT NULL,               -- catalogue | discovered | user | builtin
    created_at TEXT NOT NULL,
    last_run_at TEXT,
    last_success_at TEXT,
    last_status TEXT,                   -- ok | partial | failed | skipped
    last_error TEXT,
    consecutive_failures INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS hunt_runs (
    id INTEGER PRIMARY KEY,
    trigger TEXT NOT NULL,              -- manual | scheduled | demo | import
    status TEXT NOT NULL,               -- running | completed | partial | failed | aborted
    started_at TEXT NOT NULL,
    finished_at TEXT,
    heartbeat_at TEXT,
    owner TEXT,                         -- hostname:pid
    stats_json TEXT,
    error TEXT
);
CREATE INDEX IF NOT EXISTS idx_hunt_runs_started ON hunt_runs(started_at);

CREATE TABLE IF NOT EXISTS source_runs (
    id INTEGER PRIMARY KEY,
    hunt_run_id INTEGER NOT NULL REFERENCES hunt_runs(id),
    source_id TEXT NOT NULL,
    status TEXT NOT NULL,               -- ok | partial | failed | skipped
    started_at TEXT NOT NULL,
    finished_at TEXT,
    fetched_count INTEGER NOT NULL DEFAULT 0,
    relevant_count INTEGER NOT NULL DEFAULT 0,
    new_count INTEGER NOT NULL DEFAULT 0,
    http_status INTEGER,
    error TEXT,
    notes TEXT
);
CREATE INDEX IF NOT EXISTS idx_source_runs_hunt ON source_runs(hunt_run_id);

CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY,
    canonical_key TEXT NOT NULL,
    title TEXT NOT NULL,
    employer_id INTEGER REFERENCES employers(id),
    employer_name TEXT,
    employer_confidential INTEGER NOT NULL DEFAULT 0,
    location_text TEXT,
    country_codes_json TEXT,            -- known countries for the role
    remote_type TEXT NOT NULL DEFAULT 'unknown',  -- remote | hybrid | onsite | unknown
    remote_restriction TEXT,            -- e.g. "Remote, but only in Germany"
    employment_type TEXT,
    salary_text TEXT,
    salary_min REAL,
    salary_max REAL,
    salary_currency TEXT,
    salary_period TEXT,                 -- year | month | hour | unknown
    salary_basis TEXT,                  -- base | total | unknown
    sponsorship TEXT NOT NULL DEFAULT 'unknown',  -- yes | no | unknown
    description_text TEXT,
    description_hash TEXT,
    requisition_id TEXT,
    posted_at TEXT,                     -- original posting date from the source, if given
    posted_at_provenance TEXT,          -- which source field; or "unknown"
    source_updated_at TEXT,             -- source update/republication date
    first_seen_at TEXT NOT NULL,        -- when Hunter first observed it
    last_seen_at TEXT NOT NULL,
    last_checked_at TEXT,
    availability TEXT NOT NULL DEFAULT 'open',  -- open | unverified | closed
    availability_note TEXT,
    discovery_url TEXT,
    direct_url TEXT,
    direct_url_status TEXT,             -- verified | unconfirmed | none
    apply_url TEXT,
    relevance TEXT NOT NULL DEFAULT 'unknown',  -- relevant | uncertain | irrelevant
    relevance_reason TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_jobs_canonical ON jobs(canonical_key);
CREATE INDEX IF NOT EXISTS idx_jobs_employer ON jobs(employer_id);
CREATE INDEX IF NOT EXISTS idx_jobs_first_seen ON jobs(first_seen_at);
CREATE INDEX IF NOT EXISTS idx_jobs_posted ON jobs(posted_at);

CREATE TABLE IF NOT EXISTS job_observations (
    id INTEGER PRIMARY KEY,
    job_id INTEGER NOT NULL REFERENCES jobs(id),
    source_id TEXT NOT NULL,
    external_id TEXT,
    url TEXT,
    title TEXT,
    location_text TEXT,
    posted_at_raw TEXT,
    updated_at_raw TEXT,
    raw_hash TEXT,
    first_observed_at TEXT NOT NULL,
    last_observed_at TEXT NOT NULL,
    hunt_run_id INTEGER REFERENCES hunt_runs(id),
    UNIQUE(source_id, external_id)
);
CREATE INDEX IF NOT EXISTS idx_job_observations_job ON job_observations(job_id);

CREATE TABLE IF NOT EXISTS matches (
    id INTEGER PRIMARY KEY,
    job_id INTEGER NOT NULL REFERENCES jobs(id),
    profile_version_id INTEGER NOT NULL REFERENCES profile_versions(id),
    policy_version TEXT NOT NULL,
    cache_key TEXT NOT NULL,
    score REAL NOT NULL,
    match_group TEXT NOT NULL,          -- good | worth_a_shot | backup | excluded | uncertain
    reasons_json TEXT NOT NULL,         -- rule-based reasons with evidence fact ids
    gaps_json TEXT NOT NULL,
    eligibility_json TEXT NOT NULL,
    exclusion_reason TEXT,
    explanation_text TEXT,
    explanation_source TEXT NOT NULL DEFAULT 'pending',  -- rules | claude | pending | failed
    explanation_json TEXT,
    next_step TEXT,
    computed_at TEXT NOT NULL,
    UNIQUE(job_id, profile_version_id, policy_version)
);
CREATE INDEX IF NOT EXISTS idx_matches_group ON matches(match_group, score);

CREATE TABLE IF NOT EXISTS recommendations (
    id INTEGER PRIMARY KEY,
    job_id INTEGER NOT NULL REFERENCES jobs(id),
    match_id INTEGER REFERENCES matches(id),
    hunt_run_id INTEGER REFERENCES hunt_runs(id),
    channel TEXT NOT NULL,              -- dashboard | digest | cli
    match_group TEXT NOT NULL,
    presented_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_recommendations_job ON recommendations(job_id);

CREATE TABLE IF NOT EXISTS applications (
    id INTEGER PRIMARY KEY,
    job_id INTEGER NOT NULL REFERENCES jobs(id),
    stage TEXT NOT NULL,                -- prepared | applied | replied | interview | offer | rejected | withdrawn
    stage_changed_at TEXT NOT NULL,
    applied_at TEXT,
    applied_source TEXT,                -- user | email
    cv_document_id INTEGER REFERENCES source_documents(id),
    notes TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(job_id)
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL,                 -- opened | saved | unsaved | dismissed | undismissed | prepared | applied | contacted | reply_received | interview | rejected | offer | withdrawn | note | correction | feedback | digest_sent | ...
    job_id INTEGER REFERENCES jobs(id),
    application_id INTEGER REFERENCES applications(id),
    actor TEXT NOT NULL,                -- user | email | system
    payload_json TEXT,
    occurred_at TEXT NOT NULL,          -- original time of the action/message
    recorded_at TEXT NOT NULL,
    reverted_by INTEGER REFERENCES events(id),
    dedupe_key TEXT,
    note TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_job ON events(job_id, kind);
CREATE UNIQUE INDEX IF NOT EXISTS idx_events_dedupe ON events(dedupe_key) WHERE dedupe_key IS NOT NULL;

CREATE TABLE IF NOT EXISTS drafts (
    id INTEGER PRIMARY KEY,
    job_id INTEGER REFERENCES jobs(id),
    application_id INTEGER REFERENCES applications(id),
    kind TEXT NOT NULL,                 -- answers | intro | outreach | followup | package
    content_json TEXT NOT NULL,
    validation_json TEXT,
    status TEXT NOT NULL DEFAULT 'draft',   -- draft | sent_confirmed | discarded
    created_by TEXT NOT NULL,           -- rules | claude | user
    created_at TEXT NOT NULL,
    sent_confirmed_at TEXT
);

CREATE TABLE IF NOT EXISTS contacts (
    id INTEGER PRIMARY KEY,
    job_id INTEGER REFERENCES jobs(id),
    name TEXT,
    role TEXT,
    organization TEXT,
    route TEXT,                         -- public URL or user-supplied address
    provenance TEXT NOT NULL,           -- user | public_page:<url>
    confidence TEXT NOT NULL,           -- confirmed | likely | unknown
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS followups (
    id INTEGER PRIMARY KEY,
    application_id INTEGER NOT NULL REFERENCES applications(id),
    due_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',   -- pending | done | cancelled
    draft_id INTEGER REFERENCES drafts(id),
    created_at TEXT NOT NULL,
    resolved_at TEXT,
    reason TEXT
);

CREATE TABLE IF NOT EXISTS email_accounts (
    id INTEGER PRIMARY KEY,
    provider TEXT NOT NULL,             -- gmail
    address TEXT,
    purpose TEXT NOT NULL,              -- read | send
    scopes_json TEXT NOT NULL,
    secret_ref TEXT NOT NULL,           -- key in the secret store
    status TEXT NOT NULL,               -- connected | disconnected | error
    query TEXT,
    label TEXT,
    since_days INTEGER,
    connected_at TEXT NOT NULL,
    disconnected_at TEXT,
    last_sync_at TEXT,
    last_error TEXT
);

CREATE TABLE IF NOT EXISTS email_messages (
    id INTEGER PRIMARY KEY,
    account_id INTEGER REFERENCES email_accounts(id),
    provider_message_id TEXT NOT NULL,
    thread_id TEXT,
    received_at TEXT,
    from_addr TEXT,
    subject TEXT,
    snippet TEXT,
    extract_text TEXT,
    classification TEXT NOT NULL,       -- receipt | rejection | interview | offer | recruiter | alert | outreach_sent | other
    classification_confidence TEXT NOT NULL,  -- high | medium | low
    is_automated INTEGER NOT NULL DEFAULT 0,
    linked_job_id INTEGER REFERENCES jobs(id),
    linked_application_id INTEGER REFERENCES applications(id),
    link_status TEXT NOT NULL DEFAULT 'none',  -- none | auto | needs_confirmation | confirmed | rejected
    event_id INTEGER REFERENCES events(id),
    origin TEXT NOT NULL,               -- gmail | import
    fetched_at TEXT NOT NULL,
    UNIQUE(provider_message_id)
);

CREATE TABLE IF NOT EXISTS notifications (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL,                 -- digest
    channel TEXT NOT NULL,              -- email | dashboard
    recipient TEXT,
    subject TEXT,
    content_hash TEXT NOT NULL,
    job_ids_json TEXT NOT NULL,
    body_path TEXT,
    status TEXT NOT NULL,               -- pending | sent | failed | uncertain | skipped
    attempts INTEGER NOT NULL DEFAULT 0,
    provider_message_id TEXT,
    error TEXT,
    created_at TEXT NOT NULL,
    sent_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_notifications_hash ON notifications(content_hash);

CREATE TABLE IF NOT EXISTS consents (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL,                 -- digest_email | mailbox_read | model_processing | github_read | readiness
    granted INTEGER NOT NULL,
    details_json TEXT,
    granted_at TEXT NOT NULL,
    revoked_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_consents_kind ON consents(kind, granted_at);

CREATE TABLE IF NOT EXISTS feedback (
    id INTEGER PRIMARY KEY,
    job_id INTEGER REFERENCES jobs(id),
    kind TEXT NOT NULL,                 -- not_interested | interested | correction
    reason_code TEXT,                   -- consultancy | too_senior | too_junior | location | salary | other
    note TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS schedule (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    enabled INTEGER NOT NULL DEFAULT 0,
    paused INTEGER NOT NULL DEFAULT 0,
    frequency TEXT NOT NULL DEFAULT 'manual',   -- manual | daily | every_2h | every_4h | every_6h
    time_of_day TEXT NOT NULL DEFAULT '08:00',  -- local time for daily runs
    timezone TEXT NOT NULL DEFAULT 'UTC',
    notify_mode TEXT NOT NULL DEFAULT 'dashboard',  -- dashboard | daily_email | on_request
    digest_time TEXT NOT NULL DEFAULT '08:30',
    next_run_at TEXT,
    last_run_at TEXT,
    last_digest_at TEXT,
    updated_at TEXT NOT NULL
);
INSERT OR IGNORE INTO schedule (id, updated_at) VALUES (1, strftime('%Y-%m-%dT%H:%M:%SZ','now'));

CREATE TABLE IF NOT EXISTS worker_heartbeat (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    pid INTEGER,
    hostname TEXT,
    started_at TEXT,
    last_beat_at TEXT,
    status TEXT,                        -- idle | running | stopped
    message TEXT
);

CREATE TABLE IF NOT EXISTS model_cache (
    cache_key TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    provider TEXT NOT NULL,
    model TEXT,
    response_json TEXT NOT NULL,
    cost_usd REAL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS model_calls (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL,
    provider TEXT NOT NULL,
    model TEXT,
    status TEXT NOT NULL,               -- ok | failed | invalid_output | budget
    cost_usd REAL,
    duration_ms INTEGER,
    error TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS handoffs (
    id INTEGER PRIMARY KEY,
    application_id INTEGER NOT NULL REFERENCES applications(id),
    path TEXT NOT NULL,
    created_at TEXT NOT NULL
);
