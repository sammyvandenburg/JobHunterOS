-- Phase 1 reliability: application packages are versioned and idempotent, applications
-- remember the exact CV and package that were used, and digest sending has an outbox.
-- Nothing here deletes or rewrites user rows.
ALTER TABLE drafts ADD COLUMN inputs_hash TEXT;
ALTER TABLE applications ADD COLUMN package_draft_id INTEGER REFERENCES drafts(id);
-- At most one active package per job and input set (prevents duplicate packages from simultaneous clicks).
CREATE UNIQUE INDEX IF NOT EXISTS idx_drafts_active_package ON drafts(job_id, inputs_hash) WHERE kind = 'package' AND status = 'draft';
ALTER TABLE notifications ADD COLUMN delivery_key TEXT;
ALTER TABLE notifications ADD COLUMN claimed_at TEXT;
ALTER TABLE notifications ADD COLUMN resolved_at TEXT;
ALTER TABLE notifications ADD COLUMN resolution TEXT;
CREATE UNIQUE INDEX IF NOT EXISTS idx_notifications_delivery_key ON notifications(delivery_key) WHERE delivery_key IS NOT NULL;
