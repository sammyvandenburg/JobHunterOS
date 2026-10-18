-- Matching pipeline rework: where a job's country rule came from (stated | inferred | conflict | worldwide | unknown).
ALTER TABLE jobs ADD COLUMN location_confidence TEXT;
