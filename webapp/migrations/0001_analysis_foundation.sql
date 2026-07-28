CREATE TABLE IF NOT EXISTS analysis_module_settings (
    module_name TEXT PRIMARY KEY,
    enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    updated_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS analysis_jobs (
    id TEXT PRIMARY KEY,
    module_name TEXT NOT NULL,
    module_version TEXT NOT NULL,
    status TEXT NOT NULL CHECK (
        status IN ('queued', 'running', 'completed', 'failed', 'cancelled')
    ),
    scope_type TEXT NOT NULL,
    scope_key TEXT NOT NULL,
    scope_json TEXT NOT NULL,
    input_revision TEXT NOT NULL,
    parameters_json TEXT NOT NULL,
    progress INTEGER NOT NULL DEFAULT 0 CHECK (progress BETWEEN 0 AND 100),
    error TEXT,
    cancel_requested INTEGER NOT NULL DEFAULT 0 CHECK (cancel_requested IN (0, 1)),
    created_at INTEGER NOT NULL,
    started_at INTEGER,
    completed_at INTEGER
);

CREATE TABLE IF NOT EXISTS analysis_results (
    id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL UNIQUE REFERENCES analysis_jobs(id),
    analysis_type TEXT NOT NULL,
    analysis_version TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    scope_type TEXT NOT NULL,
    scope_key TEXT NOT NULL,
    input_scope_json TEXT NOT NULL,
    input_revision TEXT NOT NULL,
    parameters_json TEXT NOT NULL,
    confidence_json TEXT NOT NULL,
    warnings_json TEXT NOT NULL,
    output_json TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS analysis_jobs_status_created_idx
    ON analysis_jobs(status, created_at);
CREATE INDEX IF NOT EXISTS analysis_jobs_module_created_idx
    ON analysis_jobs(module_name, created_at DESC);
CREATE INDEX IF NOT EXISTS analysis_jobs_scope_idx
    ON analysis_jobs(scope_type, scope_key, created_at DESC);
CREATE INDEX IF NOT EXISTS analysis_results_lookup_idx
    ON analysis_results(analysis_type, scope_type, scope_key, created_at DESC);
CREATE INDEX IF NOT EXISTS analysis_results_revision_idx
    ON analysis_results(input_revision, analysis_type);

