CREATE TABLE IF NOT EXISTS validation_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  job_id TEXT NOT NULL,
  repository_url TEXT NOT NULL,
  branch TEXT,
  commit_sha TEXT,
  status TEXT NOT NULL,
  judge_status TEXT,
  reason_category TEXT,
  created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_validation_runs_job_id ON validation_runs(job_id);

CREATE TABLE IF NOT EXISTS patch_suggestions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id INTEGER NOT NULL,
  target_file TEXT,
  issue TEXT,
  root_cause TEXT,
  patch_summary TEXT,
  patch_diff TEXT,
  risk TEXT,
  created_at TEXT NOT NULL,
  FOREIGN KEY (run_id) REFERENCES validation_runs(id)
);

CREATE INDEX IF NOT EXISTS idx_patch_suggestions_run_id ON patch_suggestions(run_id);

CREATE TABLE IF NOT EXISTS rerun_results (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  patch_id INTEGER NOT NULL,
  before_judge_status TEXT,
  after_judge_status TEXT,
  before_error_rate REAL,
  after_error_rate REAL,
  before_p95_latency_ms REAL,
  after_p95_latency_ms REAL,
  improved INTEGER NOT NULL,
  created_at TEXT NOT NULL,
  FOREIGN KEY (patch_id) REFERENCES patch_suggestions(id)
);

CREATE INDEX IF NOT EXISTS idx_rerun_results_patch_id ON rerun_results(patch_id);
