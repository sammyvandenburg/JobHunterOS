-- Company marks: the employer's own favicon from its verified domain, fetched once by Hunter, cached small and private.
CREATE TABLE IF NOT EXISTS employer_logos (
    domain TEXT PRIMARY KEY,
    data_uri TEXT,                       -- a small image as a data URI, or NULL when none was found
    media_type TEXT,
    bytes INTEGER,
    status TEXT NOT NULL,                -- ok | none | failed
    checked_at TEXT NOT NULL
);
