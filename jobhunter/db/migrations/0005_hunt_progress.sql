-- Background hunts report progress while they run.
ALTER TABLE hunt_runs ADD COLUMN progress_json TEXT;
