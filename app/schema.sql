PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    sheet_count INTEGER,
    vehicle_type TEXT,
    batch_name TEXT NOT NULL,
    tar_key TEXT NOT NULL,
    source_scope TEXT NOT NULL,
    s3_size INTEGER,
    s3_last_modified TEXT,
    total_image_count INTEGER,
    selection_version INTEGER NOT NULL DEFAULT 1,
    image_target_count INTEGER NOT NULL DEFAULT 6,
    validation_completed_at TEXT,
    validation_completed_by TEXT,
    validation_completed_selection_version INTEGER,
    validation_completed_image_target_count INTEGER,
    locality_name TEXT,
    locality_category TEXT,
    region_id TEXT,
    subtype_label TEXT,
    dispatch_hold TEXT,
    pipeline_status TEXT,
    sheet_validation TEXT,
    compltd_status TEXT,
    compltd_validator TEXT,
    compltd_started_at TEXT,
    compltd_completed_at TEXT,
    compltd_outcome TEXT,
    compltd_reviewed_images INTEGER,
    compltd_failed_images INTEGER,
    compltd_updated_at TEXT,
    indexed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_runs_batch_name ON runs(batch_name);

CREATE TABLE IF NOT EXISTS run_images (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    selection_version INTEGER NOT NULL,
    image_index INTEGER NOT NULL,
    member_name TEXT NOT NULL,
    cache_path TEXT,
    cached_at TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(run_id, selection_version, image_index),
    FOREIGN KEY(run_id) REFERENCES runs(run_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_run_images_run_id ON run_images(run_id);

CREATE TABLE IF NOT EXISTS image_validations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_image_id INTEGER NOT NULL UNIQUE,
    run_id TEXT NOT NULL,
    selection_version INTEGER NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('pass', 'fail')),
    notes TEXT NOT NULL DEFAULT '',
    submitted_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(run_image_id) REFERENCES run_images(id) ON DELETE CASCADE,
    FOREIGN KEY(run_id) REFERENCES runs(run_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_image_validations_run_id ON image_validations(run_id);

CREATE TABLE IF NOT EXISTS sync_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sheet_runs INTEGER NOT NULL,
    s3_runs INTEGER NOT NULL,
    indexed_runs INTEGER NOT NULL,
    missing_in_s3 INTEGER NOT NULL,
    extra_in_s3 INTEGER NOT NULL,
    synced_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
