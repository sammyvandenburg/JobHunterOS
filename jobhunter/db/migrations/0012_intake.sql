-- Slice D: one intake for links, descriptions, alert emails, recruiter messages and referrals.
CREATE TABLE IF NOT EXISTS intakes (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL,                 -- link | description | alert | recruiter | referral
    received_at TEXT NOT NULL,
    job_ids_json TEXT NOT NULL,         -- the vacancies it resolved to (existing identities when it was the same job)
    new_job_ids_json TEXT NOT NULL,     -- which of them were new
    contact_ids_json TEXT,              -- a recruiter or referrer recorded with it
    email_id INTEGER REFERENCES email_messages(id),
    from_name TEXT,
    note TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_intakes_received ON intakes(received_at);
