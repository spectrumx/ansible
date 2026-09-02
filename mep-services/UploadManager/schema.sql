CREATE TABLE IF NOT EXISTS upload_jobs (
    job_id TEXT PRIMARY KEY,
    capture_id TEXT NOT NULL,
    name TEXT NOT NULL,
    path TEXT NOT NULL,
    destination TEXT NOT NULL,
    sds_path TEXT,
    state TEXT NOT NULL,
    requested_at REAL NOT NULL,
    started_at REAL,
    finished_at REAL,
    cancelled_at REAL,
    retry_count INTEGER NOT NULL DEFAULT 0,
    uploaded_files INTEGER NOT NULL DEFAULT 0,
    total_files INTEGER NOT NULL DEFAULT 0,
    uploaded_bytes INTEGER NOT NULL DEFAULT 0,
    total_bytes INTEGER NOT NULL DEFAULT 0,
    current_file TEXT,
    last_error TEXT,
    credentials_json TEXT,
    dry_run INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS upload_job_files (
    job_id TEXT NOT NULL REFERENCES upload_jobs(job_id) ON DELETE CASCADE,
    relative_path TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    modified_ns INTEGER NOT NULL,
    state TEXT NOT NULL DEFAULT 'pending',
    remote_asset_id TEXT,
    retry_count INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    PRIMARY KEY (job_id, relative_path)
);

CREATE TABLE IF NOT EXISTS upload_job_activity (
    activity_id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL REFERENCES upload_jobs(job_id) ON DELETE CASCADE,
    timestamp REAL NOT NULL,
    level TEXT NOT NULL,
    event TEXT NOT NULL,
    message TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS remote_captures (
    capture_id TEXT NOT NULL,
    destination TEXT NOT NULL,
    remote_id TEXT,
    remote_path TEXT,
    verification_status TEXT NOT NULL DEFAULT 'never_checked',
    last_checked_at REAL,
    expected_files INTEGER,
    verified_files INTEGER,
    missing_files INTEGER,
    wrong_size_files INTEGER,
    verification_error TEXT,
    PRIMARY KEY (capture_id, destination)
);

CREATE INDEX IF NOT EXISTS idx_upload_jobs_capture_id ON upload_jobs(capture_id);
CREATE INDEX IF NOT EXISTS idx_upload_job_files_job_id ON upload_job_files(job_id);
CREATE INDEX IF NOT EXISTS idx_upload_job_activity_job_id ON upload_job_activity(job_id, activity_id);
