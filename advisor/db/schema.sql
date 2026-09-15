PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS lagent_experiments (
  experiment_id TEXT PRIMARY KEY,
  submission_identity TEXT NOT NULL UNIQUE,
  name TEXT NOT NULL,
  raw_json TEXT NOT NULL,
  input_hash TEXT NOT NULL,
  config_json TEXT NOT NULL,
  config_hash TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS lagent_experiment_preflights (
  record_id TEXT PRIMARY KEY,
  experiment_id TEXT NOT NULL REFERENCES lagent_experiments(experiment_id),
  submission_identity TEXT NOT NULL,
  result_json TEXT NOT NULL,
  result_hash TEXT NOT NULL,
  created_at TEXT NOT NULL,
  UNIQUE(experiment_id, submission_identity)
);

CREATE TABLE IF NOT EXISTS advisor_runs (
  run_id TEXT PRIMARY KEY,
  run_type TEXT NOT NULL CHECK(run_type IN ('premarket', 'review', 'backfill', 'manual', 'failure')),
  as_of TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('running', 'passed', 'failed', 'blocked')),
  started_at TEXT NOT NULL,
  finished_at TEXT,
  message TEXT
);

CREATE TABLE IF NOT EXISTS data_quality_checks (
  check_id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL REFERENCES advisor_runs(run_id),
  check_name TEXT NOT NULL,
  severity TEXT NOT NULL CHECK(severity IN ('blocking', 'warning', 'info')),
  status TEXT NOT NULL CHECK(status IN ('passed', 'failed')),
  details_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS securities (
  code TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  exchange TEXT NOT NULL,
  board TEXT,
  industry TEXT,
  concepts_json TEXT NOT NULL DEFAULT '[]',
  security_type TEXT NOT NULL DEFAULT 'a_share',
  list_date TEXT,
  delist_date TEXT,
  status TEXT NOT NULL DEFAULT 'active',
  is_st INTEGER NOT NULL DEFAULT 0 CHECK(is_st IN (0, 1)),
  source TEXT NOT NULL DEFAULT 'legacy',
  source_at TEXT NOT NULL DEFAULT '',
  source_content_hash TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS market_daily (
  code TEXT NOT NULL,
  trade_date TEXT NOT NULL,
  open REAL NOT NULL,
  high REAL NOT NULL,
  low REAL NOT NULL,
  close REAL NOT NULL,
  volume INTEGER NOT NULL,
  amount REAL,
  source TEXT NOT NULL,
  source_at TEXT NOT NULL DEFAULT '',
  fetched_at TEXT NOT NULL,
  as_of_date TEXT NOT NULL,
  schema_version INTEGER NOT NULL DEFAULT 1,
  content_hash TEXT NOT NULL,
  quality_status TEXT NOT NULL DEFAULT 'passed',
  PRIMARY KEY(code, trade_date)
);

CREATE TABLE IF NOT EXISTS market_adjustment_factors (
  code TEXT NOT NULL,
  trade_date TEXT NOT NULL CHECK(date(trade_date) = trade_date),
  factor REAL NOT NULL CHECK(factor > 0),
  source TEXT NOT NULL,
  source_at TEXT NOT NULL,
  fetched_at TEXT NOT NULL,
  algorithm_version TEXT NOT NULL,
  content_hash TEXT NOT NULL,
  PRIMARY KEY(code, trade_date)
);

CREATE TABLE IF NOT EXISTS market_daily_absences (
  code TEXT NOT NULL,
  trade_date TEXT NOT NULL CHECK(date(trade_date) = trade_date),
  reason TEXT NOT NULL CHECK(reason IN ('suspended')),
  source TEXT NOT NULL,
  source_at TEXT NOT NULL,
  fetched_at TEXT NOT NULL,
  content_hash TEXT NOT NULL,
  PRIMARY KEY(code, trade_date)
);

CREATE TABLE IF NOT EXISTS trading_sessions (
  trade_date TEXT PRIMARY KEY CHECK(date(trade_date) = trade_date),
  primary_source TEXT NOT NULL,
  fallback_source TEXT NOT NULL,
  primary_observed_at TEXT NOT NULL,
  fallback_observed_at TEXT NOT NULL,
  fetched_at TEXT NOT NULL,
  content_hash TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS trading_session_observations (
  observation_id TEXT PRIMARY KEY,
  observed_at TEXT NOT NULL,
  primary_source TEXT NOT NULL,
  fallback_source TEXT NOT NULL,
  latest_session TEXT CHECK(latest_session IS NULL OR date(latest_session) = latest_session),
  session_set_hash TEXT NOT NULL,
  content_hash TEXT NOT NULL UNIQUE
);

CREATE INDEX IF NOT EXISTS idx_trading_session_observations_observed_at
  ON trading_session_observations(observed_at DESC);

CREATE TABLE IF NOT EXISTS market_daily_requests (
  request_id TEXT PRIMARY KEY,
  request_type TEXT NOT NULL CHECK(request_type IN ('cold_start', 'catch_up')),
  target_session TEXT,
  start_date TEXT,
  end_date TEXT,
  idempotency_key TEXT NOT NULL UNIQUE,
  status TEXT NOT NULL CHECK(status IN ('pending', 'claimed', 'completed', 'failed', 'cancelled')),
  created_at TEXT NOT NULL,
  claimed_by TEXT,
  claimed_at TEXT,
  completed_at TEXT,
  message TEXT
);

CREATE TABLE IF NOT EXISTS market_daily_runs (
  run_id TEXT PRIMARY KEY,
  request_id TEXT NOT NULL REFERENCES market_daily_requests(request_id),
  run_type TEXT NOT NULL CHECK(run_type IN ('cold_start', 'catch_up', 'repair')),
  status TEXT NOT NULL CHECK(status IN ('pending', 'running', 'partial', 'complete', 'failed', 'cancelled')),
  target_session TEXT NOT NULL CHECK(date(target_session) = target_session),
  start_date TEXT NOT NULL CHECK(date(start_date) = start_date),
  end_date TEXT NOT NULL CHECK(date(end_date) = end_date),
  universe_hash TEXT NOT NULL,
  total_items INTEGER NOT NULL DEFAULT 0 CHECK(total_items >= 0),
  completed_items INTEGER NOT NULL DEFAULT 0 CHECK(completed_items >= 0),
  failed_items INTEGER NOT NULL DEFAULT 0 CHECK(failed_items >= 0),
  created_at TEXT NOT NULL,
  started_at TEXT,
  finished_at TEXT,
  message TEXT
);

CREATE TABLE IF NOT EXISTS market_daily_run_securities (
  run_id TEXT NOT NULL REFERENCES market_daily_runs(run_id),
  code TEXT NOT NULL,
  name TEXT NOT NULL,
  exchange TEXT NOT NULL CHECK(exchange IN ('SH', 'SZ')),
  list_date TEXT NOT NULL CHECK(date(list_date) = list_date),
  delist_date TEXT,
  status TEXT NOT NULL,
  PRIMARY KEY(run_id, code)
);

CREATE TABLE IF NOT EXISTS market_daily_run_items (
  run_id TEXT NOT NULL REFERENCES market_daily_runs(run_id),
  code TEXT NOT NULL,
  start_date TEXT NOT NULL CHECK(date(start_date) = start_date),
  end_date TEXT NOT NULL CHECK(date(end_date) = end_date),
  status TEXT NOT NULL CHECK(status IN ('pending', 'running', 'completed', 'source_missing', 'conflicted', 'skipped')),
  attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts >= 0),
  selected_source TEXT,
  last_error TEXT,
  claimed_by TEXT,
  claim_expires_at TEXT,
  updated_at TEXT NOT NULL,
  completed_at TEXT,
  PRIMARY KEY(run_id, code)
);

CREATE TABLE IF NOT EXISTS market_daily_repairs (
  repair_id TEXT PRIMARY KEY,
  code TEXT NOT NULL,
  trade_date TEXT NOT NULL CHECK(date(trade_date) = trade_date),
  previous_content_hash TEXT NOT NULL,
  replacement_content_hash TEXT NOT NULL,
  reason TEXT NOT NULL,
  repaired_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS market_daily_service_leases (
  lease_name TEXT PRIMARY KEY,
  owner_id TEXT NOT NULL,
  acquired_at TEXT NOT NULL,
  heartbeat_at TEXT NOT NULL,
  expires_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS market_sources (
  source_key TEXT PRIMARY KEY,
  source TEXT NOT NULL,
  endpoint TEXT NOT NULL,
  params_hash TEXT NOT NULL,
  fetched_at TEXT NOT NULL,
  status TEXT NOT NULL,
  details_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events_normalized (
  evidence_source_id TEXT PRIMARY KEY,
  source_type TEXT NOT NULL,
  source_id TEXT NOT NULL,
  code TEXT,
  as_of TEXT NOT NULL,
  summary TEXT NOT NULL,
  raw_ref_json TEXT NOT NULL,
  quality_status TEXT NOT NULL DEFAULT 'passed'
);

CREATE TABLE IF NOT EXISTS evidence (
  evidence_id TEXT PRIMARY KEY,
  run_id TEXT,
  code TEXT,
  as_of TEXT NOT NULL,
  source_type TEXT NOT NULL,
  source_id TEXT NOT NULL,
  summary TEXT NOT NULL,
  confidence REAL NOT NULL DEFAULT 0.5,
  facts_json TEXT NOT NULL DEFAULT '[]',
  inferences_json TEXT NOT NULL DEFAULT '[]',
  conflicts_json TEXT NOT NULL DEFAULT '[]',
  quality_flags_json TEXT NOT NULL DEFAULT '[]'
);

CREATE TABLE IF NOT EXISTS analyst_outputs (
  output_id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL REFERENCES advisor_runs(run_id),
  role TEXT NOT NULL,
  code TEXT,
  as_of TEXT NOT NULL,
  summary TEXT NOT NULL,
  payload_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS stock_profiles (
  code TEXT PRIMARY KEY REFERENCES securities(code),
  thesis_json TEXT NOT NULL DEFAULT '{}',
  information_flow_json TEXT NOT NULL DEFAULT '[]',
  capital_flow_json TEXT NOT NULL DEFAULT '[]',
  fundamentals_json TEXT NOT NULL DEFAULT '{}',
  analyst_flow_json TEXT NOT NULL DEFAULT '[]',
  ledger_exposure_json TEXT NOT NULL DEFAULT '{}',
  assets_json TEXT NOT NULL DEFAULT '[]',
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS stock_profile_history (
  history_id TEXT PRIMARY KEY,
  code TEXT NOT NULL REFERENCES securities(code),
  run_id TEXT,
  change_summary TEXT NOT NULL,
  snapshot_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS advice (
  advice_id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL REFERENCES advisor_runs(run_id),
  code TEXT NOT NULL REFERENCES securities(code),
  action TEXT NOT NULL,
  confidence REAL NOT NULL,
  rationale TEXT NOT NULL,
  evidence_ids_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS reviews (
  review_id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL REFERENCES advisor_runs(run_id),
  advice_id TEXT NOT NULL REFERENCES advice(advice_id),
  outcome TEXT NOT NULL,
  review_text TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS advice_trade_matches (
  match_id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL REFERENCES advisor_runs(run_id),
  advice_id TEXT NOT NULL REFERENCES advice(advice_id),
  transaction_id TEXT NOT NULL REFERENCES ledger_transactions(transaction_id),
  account_id TEXT NOT NULL REFERENCES ledger_accounts(account_id),
  code TEXT NOT NULL,
  trade_date TEXT NOT NULL,
  created_at TEXT NOT NULL,
  UNIQUE(run_id, advice_id, transaction_id)
);

CREATE TABLE IF NOT EXISTS ledger_accounts (
  account_id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  currency TEXT NOT NULL DEFAULT 'CNY',
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ledger_transactions (
  transaction_id TEXT PRIMARY KEY,
  account_id TEXT NOT NULL REFERENCES ledger_accounts(account_id),
  trade_date TEXT NOT NULL,
  transaction_type TEXT NOT NULL CHECK(transaction_type IN ('cash_deposit', 'cash_withdrawal', 'buy', 'sell', 'fee', 'tax')),
  code TEXT,
  quantity INTEGER NOT NULL DEFAULT 0,
  price REAL NOT NULL DEFAULT 0,
  amount REAL NOT NULL,
  fees REAL NOT NULL DEFAULT 0,
  source TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS positions (
  account_id TEXT NOT NULL REFERENCES ledger_accounts(account_id),
  code TEXT NOT NULL,
  quantity INTEGER NOT NULL,
  cost_basis REAL NOT NULL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY(account_id, code)
);

CREATE TABLE IF NOT EXISTS portfolio_snapshots (
  snapshot_id TEXT PRIMARY KEY,
  account_id TEXT NOT NULL REFERENCES ledger_accounts(account_id),
  as_of TEXT NOT NULL,
  cash REAL NOT NULL,
  market_value REAL NOT NULL,
  realized_pnl REAL NOT NULL,
  unrealized_pnl REAL NOT NULL,
  exposure_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS chart_assets (
  asset_id TEXT PRIMARY KEY,
  code TEXT NOT NULL,
  chart_type TEXT NOT NULL,
  as_of TEXT NOT NULL,
  path TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS report_archive (
  report_id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL REFERENCES advisor_runs(run_id),
  report_type TEXT NOT NULL CHECK(report_type IN ('premarket', 'review', 'failure')),
  report_date TEXT NOT NULL,
  markdown_path TEXT NOT NULL,
  json_path TEXT NOT NULL,
  supersedes_report_id TEXT,
  created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS market_daily_code_date_idx ON market_daily(code, trade_date);
CREATE INDEX IF NOT EXISTS market_adjustment_factors_code_date_idx ON market_adjustment_factors(code, trade_date);
CREATE INDEX IF NOT EXISTS market_daily_absences_code_date_idx ON market_daily_absences(code, trade_date);
CREATE INDEX IF NOT EXISTS market_daily_requests_status_idx ON market_daily_requests(status, created_at);
CREATE INDEX IF NOT EXISTS market_daily_runs_status_idx ON market_daily_runs(status, created_at);
CREATE INDEX IF NOT EXISTS market_daily_run_items_status_idx ON market_daily_run_items(run_id, status);
CREATE INDEX IF NOT EXISTS market_daily_repairs_code_date_idx ON market_daily_repairs(code, trade_date);
CREATE INDEX IF NOT EXISTS evidence_code_asof_idx ON evidence(code, as_of);
CREATE INDEX IF NOT EXISTS advice_run_idx ON advice(run_id);
CREATE INDEX IF NOT EXISTS reviews_advice_idx ON reviews(advice_id);
CREATE INDEX IF NOT EXISTS advice_trade_matches_transaction_idx ON advice_trade_matches(transaction_id);

CREATE TABLE IF NOT EXISTS research_batches (
  batch_id TEXT PRIMARY KEY,
  as_of TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('pending', 'running', 'passed', 'blocked', 'failed', 'cancelled')),
  team_refs_json TEXT NOT NULL,
  subject_refs_json TEXT NOT NULL,
  execution_policy_ref TEXT NOT NULL,
  created_at TEXT NOT NULL,
  finished_at TEXT,
  message TEXT
);

CREATE TABLE IF NOT EXISTS research_cycles (
  cycle_id TEXT PRIMARY KEY,
  batch_id TEXT REFERENCES research_batches(batch_id),
  subject_code TEXT NOT NULL,
  subject_name TEXT,
  as_of TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('pending', 'running', 'passed', 'blocked', 'failed', 'cancelled')),
  snapshot_id TEXT,
  cycle_fingerprint TEXT NOT NULL,
  created_at TEXT NOT NULL,
  finished_at TEXT,
  message TEXT,
  UNIQUE(batch_id, subject_code, as_of)
);

CREATE TABLE IF NOT EXISTS research_runs (
  research_run_id TEXT PRIMARY KEY,
  cycle_id TEXT NOT NULL REFERENCES research_cycles(cycle_id),
  team_ref TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('pending', 'running', 'passed', 'blocked', 'failed', 'cancelled')),
  conclusion_hash TEXT,
  created_at TEXT NOT NULL,
  finished_at TEXT,
  message TEXT,
  UNIQUE(cycle_id, team_ref)
);

CREATE TABLE IF NOT EXISTS research_snapshots (
  snapshot_id TEXT PRIMARY KEY,
  cycle_id TEXT NOT NULL REFERENCES research_cycles(cycle_id),
  subject_code TEXT NOT NULL,
  as_of TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('building', 'sealed', 'invalid')),
  product_refs_json TEXT NOT NULL,
  product_hashes_json TEXT NOT NULL,
  unavailable_json TEXT NOT NULL DEFAULT '{}',
  snapshot_hash TEXT NOT NULL UNIQUE,
  created_at TEXT NOT NULL,
  sealed_at TEXT
);

CREATE TABLE IF NOT EXISTS research_artifacts (
  content_hash TEXT PRIMARY KEY,
  media_type TEXT NOT NULL,
  byte_size INTEGER NOT NULL CHECK(byte_size >= 0),
  relative_path TEXT NOT NULL UNIQUE,
  retention_class TEXT NOT NULL,
  availability TEXT NOT NULL CHECK(availability IN ('available', 'expired', 'unavailable')),
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS research_query_backing_migrations (
  source_artifact_hash TEXT PRIMARY KEY REFERENCES research_artifacts(content_hash),
  backing_artifact_hash TEXT NOT NULL REFERENCES research_artifacts(content_hash),
  migrated_envelope_json TEXT NOT NULL
    CHECK(json_valid(migrated_envelope_json) AND json_type(migrated_envelope_json) = 'object'),
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS research_snapshot_products (
  snapshot_id TEXT NOT NULL REFERENCES research_snapshots(snapshot_id),
  product_ref TEXT NOT NULL,
  artifact_hash TEXT NOT NULL REFERENCES research_artifacts(content_hash),
  quality_status TEXT NOT NULL CHECK(quality_status IN ('passed', 'warning', 'blocked')),
  provider_attempts_json TEXT NOT NULL,
  PRIMARY KEY(snapshot_id, product_ref)
);

-- Legacy Snapshot/Cycle tables predate explicit Research Scope and require a
-- non-null security code. Market Research must never invent a sentinel code
-- merely to fit that shape, so its otherwise identical sealed snapshot index
-- is stored here. This is persistence metadata, not a second executor.
CREATE TABLE IF NOT EXISTS research_scope_snapshots (
  snapshot_id TEXT PRIMARY KEY,
  cycle_id TEXT NOT NULL,
  scope TEXT NOT NULL CHECK(scope IN ('market', 'security')),
  subject_code TEXT,
  subject_name TEXT,
  as_of TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('building', 'sealed', 'invalid')),
  product_refs_json TEXT NOT NULL,
  product_hashes_json TEXT NOT NULL,
  unavailable_json TEXT NOT NULL DEFAULT '{}',
  snapshot_hash TEXT NOT NULL UNIQUE,
  created_at TEXT NOT NULL,
  sealed_at TEXT,
  CHECK((scope = 'market' AND subject_code IS NULL) OR (scope = 'security' AND subject_code IS NOT NULL)),
  UNIQUE(cycle_id, scope)
);

CREATE TABLE IF NOT EXISTS research_scope_snapshot_products (
  snapshot_id TEXT NOT NULL REFERENCES research_scope_snapshots(snapshot_id),
  product_ref TEXT NOT NULL,
  artifact_hash TEXT NOT NULL REFERENCES research_artifacts(content_hash),
  quality_status TEXT NOT NULL CHECK(quality_status IN ('passed', 'warning', 'blocked')),
  provider_attempts_json TEXT NOT NULL,
  PRIMARY KEY(snapshot_id, product_ref)
);

CREATE INDEX IF NOT EXISTS research_scope_snapshots_cycle_idx
  ON research_scope_snapshots(cycle_id, scope, created_at DESC);

-- Scope-aware invocation metadata gives Market requests the same crash
-- recovery boundary as Security requests without forcing a code into the
-- historical ``research_invocations`` table.
CREATE TABLE IF NOT EXISTS research_scope_invocations (
  invocation_key TEXT PRIMARY KEY,
  cycle_id TEXT NOT NULL,
  scope TEXT NOT NULL CHECK(scope IN ('market', 'security')),
  agent_ref TEXT NOT NULL,
  as_of TEXT NOT NULL,
  input_hashes_json TEXT NOT NULL,
  policy_ref TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('pending', 'running', 'passed', 'blocked', 'failed', 'cancelled')),
  finding_hash TEXT REFERENCES research_artifacts(content_hash),
  query_log_hash TEXT REFERENCES research_artifacts(content_hash),
  created_at TEXT NOT NULL,
  finished_at TEXT,
  message TEXT,
  CHECK(scope = 'market'),
  UNIQUE(cycle_id, agent_ref)
);

CREATE INDEX IF NOT EXISTS research_scope_invocations_cycle_idx
  ON research_scope_invocations(cycle_id, status, agent_ref);

-- Market invocations share the same retry/Attempt audit boundary as the
-- historical Security table, while retaining the explicit no-code Scope
-- foreign key.  Do not route these rows through research_invocations: that
-- table requires a Security cycle and subject_code.
CREATE TABLE IF NOT EXISTS research_scope_invocation_attempts (
  attempt_id TEXT PRIMARY KEY,
  invocation_key TEXT NOT NULL REFERENCES research_scope_invocations(invocation_key),
  attempt_number INTEGER NOT NULL CHECK(attempt_number >= 1),
  status TEXT NOT NULL CHECK(status IN ('running', 'passed', 'failed', 'blocked', 'cancelled')),
  capsule_hash TEXT NOT NULL REFERENCES research_artifacts(content_hash),
  output_hash TEXT REFERENCES research_artifacts(content_hash),
  error_class TEXT,
  started_at TEXT NOT NULL,
  finished_at TEXT,
  duration_ms INTEGER,
  policy_json TEXT NOT NULL DEFAULT '{}',
  cli_version TEXT,
  model TEXT,
  reasoning_effort TEXT,
  usage_json TEXT NOT NULL DEFAULT '{}',
  UNIQUE(invocation_key, attempt_number)
);

CREATE TABLE IF NOT EXISTS research_invocations (
  invocation_key TEXT PRIMARY KEY,
  cycle_id TEXT NOT NULL REFERENCES research_cycles(cycle_id),
  agent_ref TEXT NOT NULL,
  subject_code TEXT NOT NULL,
  as_of TEXT NOT NULL,
  input_hashes_json TEXT NOT NULL,
  policy_ref TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('pending', 'running', 'passed', 'blocked', 'failed', 'cancelled')),
  finding_hash TEXT,
  query_log_hash TEXT REFERENCES research_artifacts(content_hash),
  created_at TEXT NOT NULL,
  finished_at TEXT,
  message TEXT
);

CREATE TABLE IF NOT EXISTS research_invocation_attempts (
  attempt_id TEXT PRIMARY KEY,
  invocation_key TEXT NOT NULL REFERENCES research_invocations(invocation_key),
  attempt_number INTEGER NOT NULL CHECK(attempt_number >= 1),
  status TEXT NOT NULL CHECK(status IN ('running', 'passed', 'failed', 'blocked', 'cancelled')),
  capsule_hash TEXT NOT NULL REFERENCES research_artifacts(content_hash),
  output_hash TEXT REFERENCES research_artifacts(content_hash),
  error_class TEXT,
  started_at TEXT NOT NULL,
  finished_at TEXT,
  duration_ms INTEGER,
  policy_json TEXT NOT NULL DEFAULT '{}',
  cli_version TEXT,
  model TEXT,
  reasoning_effort TEXT,
  usage_json TEXT NOT NULL DEFAULT '{}',
  UNIQUE(invocation_key, attempt_number)
);

CREATE TABLE IF NOT EXISTS research_stage_runs (
  stage_run_id TEXT PRIMARY KEY,
  research_run_id TEXT NOT NULL REFERENCES research_runs(research_run_id),
  stage_name TEXT NOT NULL,
  stage_order INTEGER NOT NULL CHECK(stage_order >= 0),
  status TEXT NOT NULL CHECK(status IN ('pending', 'running', 'passed', 'blocked', 'failed', 'cancelled')),
  input_hashes_json TEXT NOT NULL,
  output_hash TEXT REFERENCES research_artifacts(content_hash),
  created_at TEXT NOT NULL,
  finished_at TEXT,
  message TEXT,
  UNIQUE(research_run_id, stage_name)
);

CREATE TABLE IF NOT EXISTS research_stage_attempts (
  stage_attempt_id TEXT PRIMARY KEY,
  stage_run_id TEXT NOT NULL REFERENCES research_stage_runs(stage_run_id),
  attempt_number INTEGER NOT NULL CHECK(attempt_number >= 1),
  status TEXT NOT NULL CHECK(status IN ('running', 'passed', 'failed', 'blocked', 'cancelled')),
  capsule_hash TEXT NOT NULL REFERENCES research_artifacts(content_hash),
  output_hash TEXT REFERENCES research_artifacts(content_hash),
  error_class TEXT,
  started_at TEXT NOT NULL,
  finished_at TEXT,
  duration_ms INTEGER,
  policy_json TEXT NOT NULL DEFAULT '{}',
  cli_version TEXT,
  model TEXT,
  reasoning_effort TEXT,
  usage_json TEXT NOT NULL DEFAULT '{}',
  UNIQUE(stage_run_id, attempt_number)
);

CREATE TABLE IF NOT EXISTS research_team_conclusions (
  conclusion_hash TEXT PRIMARY KEY REFERENCES research_artifacts(content_hash),
  research_run_id TEXT NOT NULL REFERENCES research_runs(research_run_id),
  team_ref TEXT NOT NULL,
  subject_code TEXT NOT NULL,
  as_of TEXT NOT NULL,
  stance TEXT NOT NULL,
  conviction TEXT NOT NULL,
  quality_status TEXT NOT NULL CHECK(quality_status IN ('passed', 'warning', 'blocked')),
  created_at TEXT NOT NULL,
  UNIQUE(research_run_id, team_ref)
);

CREATE TABLE IF NOT EXISTS research_reports (
  report_id TEXT PRIMARY KEY,
  cycle_id TEXT NOT NULL REFERENCES research_cycles(cycle_id),
  team_ref TEXT NOT NULL,
  report_kind TEXT NOT NULL CHECK(report_kind IN ('conclusion', 'blocked', 'daily_brief', 'cycle_index', 'review')),
  json_hash TEXT REFERENCES research_artifacts(content_hash),
  markdown_hash TEXT REFERENCES research_artifacts(content_hash),
  status TEXT NOT NULL CHECK(status IN ('complete', 'blocked')),
  created_at TEXT NOT NULL,
  UNIQUE(cycle_id, team_ref, report_kind)
);

CREATE TABLE IF NOT EXISTS research_reviews (
  review_id TEXT PRIMARY KEY,
  conclusion_hash TEXT NOT NULL REFERENCES research_team_conclusions(conclusion_hash),
  team_ref TEXT NOT NULL,
  subject_code TEXT NOT NULL,
  as_of TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('passed', 'blocked')),
  outcome TEXT,
  review_hash TEXT REFERENCES research_artifacts(content_hash),
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS research_team_profiles (
  profile_id TEXT PRIMARY KEY,
  team_ref TEXT NOT NULL,
  subject_code TEXT NOT NULL,
  conclusion_hash TEXT NOT NULL REFERENCES research_team_conclusions(conclusion_hash),
  profile_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  UNIQUE(team_ref, subject_code, conclusion_hash)
);

CREATE INDEX IF NOT EXISTS research_cycles_batch_idx ON research_cycles(batch_id);
CREATE INDEX IF NOT EXISTS research_runs_cycle_idx ON research_runs(cycle_id);
CREATE INDEX IF NOT EXISTS research_invocations_cycle_idx ON research_invocations(cycle_id);
CREATE INDEX IF NOT EXISTS research_stage_runs_run_idx ON research_stage_runs(research_run_id);
CREATE INDEX IF NOT EXISTS research_stage_attempts_stage_idx ON research_stage_attempts(stage_run_id);
CREATE INDEX IF NOT EXISTS research_team_profiles_subject_idx ON research_team_profiles(subject_code, team_ref);

-- Durable Request/Record control plane.  It deliberately lives alongside the
-- immutable legacy Cycle tables: legacy artifacts remain read-only evidence,
-- while every new trigger is represented by exactly one Request.
CREATE TABLE IF NOT EXISTS research_requests (
  request_id TEXT PRIMARY KEY,
  submission_key TEXT NOT NULL UNIQUE,
  team_ref TEXT NOT NULL,
  team_id TEXT NOT NULL,
  team_version INTEGER NOT NULL CHECK(team_version >= 1),
  scope TEXT NOT NULL CHECK(scope IN ('market', 'security')),
  subject_code TEXT,
  subject_name TEXT,
  origin TEXT NOT NULL,
  requested_at TEXT NOT NULL,
  accepted_at TEXT NOT NULL,
  boundary_at TEXT,
  status TEXT NOT NULL CHECK(status IN ('queued', 'running', 'passed', 'partial', 'blocked', 'failed', 'cancelled')),
  cancel_requested INTEGER NOT NULL DEFAULT 0 CHECK(cancel_requested IN (0, 1)),
  phase TEXT NOT NULL DEFAULT 'queued',
  agents_completed INTEGER NOT NULL DEFAULT 0 CHECK(agents_completed >= 0),
  agents_total INTEGER NOT NULL DEFAULT 0 CHECK(agents_total >= 0),
  decision_stage TEXT,
  reason_code TEXT,
  cycle_id TEXT,
  report_json_hash TEXT,
  report_markdown_hash TEXT,
  published_at TEXT,
  rerun_of TEXT REFERENCES research_requests(request_id),
  claimed_by TEXT,
  claimed_at TEXT,
  last_updated_at TEXT NOT NULL,
  created_at TEXT NOT NULL,
  finished_at TEXT,
  CHECK((scope = 'market' AND subject_code IS NULL) OR (scope = 'security' AND subject_code IS NOT NULL)),
  CHECK(julianday(accepted_at) >= julianday(requested_at)),
  CHECK(boundary_at IS NULL OR julianday(boundary_at) >= julianday(requested_at) OR origin = 'legacy')
);

CREATE TABLE IF NOT EXISTS research_request_policies (
  request_id TEXT PRIMARY KEY REFERENCES research_requests(request_id),
  policy_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS research_records (
  record_id TEXT PRIMARY KEY,
  request_id TEXT NOT NULL UNIQUE REFERENCES research_requests(request_id),
  team_ref TEXT NOT NULL,
  team_id TEXT NOT NULL,
  team_version INTEGER NOT NULL CHECK(team_version >= 1),
  scope TEXT NOT NULL CHECK(scope IN ('market', 'security')),
  subject_code TEXT,
  subject_name TEXT,
  origin TEXT NOT NULL,
  requested_at TEXT NOT NULL,
  accepted_at TEXT NOT NULL,
  boundary_at TEXT,
  status TEXT NOT NULL CHECK(status IN ('passed', 'partial', 'blocked', 'failed', 'cancelled')),
  phase TEXT NOT NULL,
  reason_code TEXT,
  cycle_id TEXT,
  report_json_hash TEXT,
  report_markdown_hash TEXT,
  published_at TEXT,
  rerun_of TEXT,
  created_at TEXT NOT NULL,
  CHECK((scope = 'market' AND subject_code IS NULL) OR (scope = 'security' AND subject_code IS NOT NULL))
);

CREATE TABLE IF NOT EXISTS research_request_events (
  event_id INTEGER PRIMARY KEY AUTOINCREMENT,
  request_id TEXT NOT NULL REFERENCES research_requests(request_id),
  occurred_at TEXT NOT NULL,
  status TEXT NOT NULL,
  phase TEXT NOT NULL,
  agents_completed INTEGER NOT NULL DEFAULT 0,
  agents_total INTEGER NOT NULL DEFAULT 0,
  decision_stage TEXT,
  reason_code TEXT,
  UNIQUE(request_id, occurred_at, status, phase, agents_completed, agents_total, decision_stage, reason_code)
);

CREATE TABLE IF NOT EXISTS research_service_leases (
  lease_name TEXT PRIMARY KEY,
  owner_id TEXT NOT NULL,
  acquired_at TEXT NOT NULL,
  heartbeat_at TEXT NOT NULL,
  expires_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS research_requests_queue_idx
  ON research_requests(status, cancel_requested, accepted_at, request_id);
CREATE INDEX IF NOT EXISTS research_requests_team_idx
  ON research_requests(team_id, team_version, status, published_at DESC, request_id DESC);
CREATE INDEX IF NOT EXISTS research_records_publication_idx
  ON research_records(published_at DESC, record_id DESC);
CREATE INDEX IF NOT EXISTS research_request_events_request_idx
  ON research_request_events(request_id, event_id);

-- Immutable Eastmoney first-level industry membership snapshots.  These are
-- intentionally independent from both security master labels and quote
-- provider output: a Market Record pins one version by content hash.
CREATE TABLE IF NOT EXISTS research_industry_taxonomy_versions (
  taxonomy_hash TEXT PRIMARY KEY,
  as_of_date TEXT NOT NULL,
  observed_at TEXT NOT NULL,
  fetched_at TEXT NOT NULL,
  source TEXT NOT NULL,
  coverage REAL NOT NULL CHECK(coverage >= 0 AND coverage <= 1),
  payload_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  UNIQUE(as_of_date, taxonomy_hash)
);

CREATE TABLE IF NOT EXISTS research_industry_taxonomy_members (
  taxonomy_hash TEXT NOT NULL REFERENCES research_industry_taxonomy_versions(taxonomy_hash),
  industry_id TEXT NOT NULL,
  industry_name TEXT NOT NULL,
  code TEXT NOT NULL,
  PRIMARY KEY(taxonomy_hash, code),
  UNIQUE(taxonomy_hash, industry_id, code)
);

CREATE INDEX IF NOT EXISTS research_industry_taxonomy_date_idx
  ON research_industry_taxonomy_versions(as_of_date DESC, created_at DESC);
-- LAgent configuration revisions and traces share the durable Research queue.
CREATE TABLE IF NOT EXISTS research_lagent_settings (
  version INTEGER PRIMARY KEY,
  config_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS research_lagent_requests (
  request_id TEXT PRIMARY KEY REFERENCES research_requests(request_id),
  config_version INTEGER NOT NULL,
  task TEXT NOT NULL,
  config_json TEXT NOT NULL,
  products_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS research_lagent_events (
  sequence INTEGER PRIMARY KEY AUTOINCREMENT,
  request_id TEXT NOT NULL REFERENCES research_requests(request_id),
  event_key TEXT NOT NULL,
  agent_id TEXT NOT NULL,
  parent_id TEXT,
  kind TEXT NOT NULL,
  status TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  UNIQUE(request_id, event_key)
);

-- Historical experiments reuse the Research control database and ArtifactStore.
-- Immutable facts are separate from rebuildable projections and worker leases.
CREATE TABLE IF NOT EXISTS lagent_records (
  record_sequence INTEGER PRIMARY KEY AUTOINCREMENT,
  record_id TEXT NOT NULL UNIQUE,
  experiment_id TEXT NOT NULL REFERENCES lagent_experiments(experiment_id),
  kind TEXT NOT NULL,
  submission_identity TEXT NOT NULL,
  input_hash TEXT NOT NULL,
  value_json TEXT NOT NULL CHECK(json_valid(value_json)),
  value_hash TEXT NOT NULL,
  created_at TEXT NOT NULL,
  UNIQUE(experiment_id, kind, submission_identity)
);
CREATE INDEX IF NOT EXISTS lagent_records_page ON lagent_records(experiment_id, kind, record_sequence);
CREATE TABLE IF NOT EXISTS lagent_record_links (
  record_id TEXT NOT NULL REFERENCES lagent_records(record_id),
  relation TEXT NOT NULL,
  target_id TEXT NOT NULL REFERENCES lagent_records(record_id),
  PRIMARY KEY(record_id, relation, target_id)
);
CREATE TABLE IF NOT EXISTS lagent_artifact_links (
  record_id TEXT NOT NULL REFERENCES lagent_records(record_id),
  content_hash TEXT NOT NULL REFERENCES research_artifacts(content_hash),
  retention TEXT NOT NULL CHECK(retention IN ('permanent', 'source_policy')),
  PRIMARY KEY(record_id, content_hash)
);
CREATE TABLE IF NOT EXISTS lagent_events (
  event_sequence INTEGER PRIMARY KEY AUTOINCREMENT,
  experiment_id TEXT NOT NULL REFERENCES lagent_experiments(experiment_id),
  test_id TEXT NOT NULL REFERENCES lagent_records(record_id),
  phase_id TEXT NOT NULL,
  action_id TEXT NOT NULL,
  attempt INTEGER NOT NULL CHECK(attempt >= 0),
  generation INTEGER NOT NULL CHECK(generation >= 0),
  kind TEXT NOT NULL,
  input_hash TEXT NOT NULL,
  value_json TEXT NOT NULL CHECK(json_valid(value_json)),
  value_hash TEXT NOT NULL,
  actual_at TEXT NOT NULL,
  simulated_at TEXT,
  UNIQUE(test_id, phase_id, action_id, attempt)
);
CREATE INDEX IF NOT EXISTS lagent_events_page ON lagent_events(test_id, event_sequence);
CREATE TABLE IF NOT EXISTS lagent_projections (
  test_id TEXT NOT NULL REFERENCES lagent_records(record_id),
  name TEXT NOT NULL,
  event_sequence INTEGER NOT NULL REFERENCES lagent_events(event_sequence),
  value_json TEXT NOT NULL CHECK(json_valid(value_json)),
  value_hash TEXT NOT NULL,
  PRIMARY KEY(test_id, name)
);
CREATE TABLE IF NOT EXISTS lagent_worker_leases (
  test_id TEXT PRIMARY KEY REFERENCES lagent_records(record_id),
  worker_id TEXT NOT NULL,
  generation INTEGER NOT NULL CHECK(generation > 0),
  expires_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS lagent_outbox (
  outbox_id TEXT PRIMARY KEY,
  event_sequence INTEGER NOT NULL REFERENCES lagent_events(event_sequence),
  destination TEXT NOT NULL,
  payload_json TEXT NOT NULL CHECK(json_valid(payload_json)),
  payload_hash TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS lagent_outbox_deliveries (
  outbox_id TEXT PRIMARY KEY REFERENCES lagent_outbox(outbox_id),
  receipt_json TEXT NOT NULL CHECK(json_valid(receipt_json)),
  receipt_hash TEXT NOT NULL,
  delivered_at TEXT NOT NULL
);

CREATE TRIGGER IF NOT EXISTS lagent_records_no_update BEFORE UPDATE ON lagent_records
BEGIN SELECT RAISE(ABORT, 'experiment records are immutable'); END;
CREATE TRIGGER IF NOT EXISTS lagent_records_no_delete BEFORE DELETE ON lagent_records
BEGIN SELECT RAISE(ABORT, 'experiment records are permanent'); END;
CREATE TRIGGER IF NOT EXISTS lagent_events_no_update BEFORE UPDATE ON lagent_events
BEGIN SELECT RAISE(ABORT, 'experiment events are immutable'); END;
CREATE TRIGGER IF NOT EXISTS lagent_events_no_delete BEFORE DELETE ON lagent_events
BEGIN SELECT RAISE(ABORT, 'experiment events are permanent'); END;
CREATE TRIGGER IF NOT EXISTS lagent_record_links_no_update BEFORE UPDATE ON lagent_record_links
BEGIN SELECT RAISE(ABORT, 'experiment links are immutable'); END;
CREATE TRIGGER IF NOT EXISTS lagent_record_links_no_delete BEFORE DELETE ON lagent_record_links
BEGIN SELECT RAISE(ABORT, 'experiment links are permanent'); END;
CREATE TRIGGER IF NOT EXISTS lagent_artifact_links_no_update BEFORE UPDATE ON lagent_artifact_links
BEGIN SELECT RAISE(ABORT, 'experiment artifact links are immutable'); END;
CREATE TRIGGER IF NOT EXISTS lagent_artifact_links_no_delete BEFORE DELETE ON lagent_artifact_links
BEGIN SELECT RAISE(ABORT, 'experiment artifact links are permanent'); END;
CREATE TRIGGER IF NOT EXISTS lagent_outbox_no_update BEFORE UPDATE ON lagent_outbox
BEGIN SELECT RAISE(ABORT, 'experiment outbox is immutable'); END;
CREATE TRIGGER IF NOT EXISTS lagent_outbox_no_delete BEFORE DELETE ON lagent_outbox
BEGIN SELECT RAISE(ABORT, 'experiment outbox is permanent'); END;
CREATE TRIGGER IF NOT EXISTS lagent_deliveries_no_update BEFORE UPDATE ON lagent_outbox_deliveries
BEGIN SELECT RAISE(ABORT, 'experiment delivery receipt is immutable'); END;
CREATE TRIGGER IF NOT EXISTS lagent_deliveries_no_delete BEFORE DELETE ON lagent_outbox_deliveries
BEGIN SELECT RAISE(ABORT, 'experiment delivery receipt is permanent'); END;
CREATE TRIGGER IF NOT EXISTS lagent_experiments_no_update BEFORE UPDATE ON lagent_experiments
BEGIN SELECT RAISE(ABORT, 'experiment drafts are immutable; create a linked revision'); END;
CREATE TRIGGER IF NOT EXISTS lagent_experiments_no_delete BEFORE DELETE ON lagent_experiments
BEGIN SELECT RAISE(ABORT, 'experiment drafts are permanent'); END;
CREATE TRIGGER IF NOT EXISTS lagent_preflights_no_update BEFORE UPDATE ON lagent_experiment_preflights
BEGIN SELECT RAISE(ABORT, 'experiment preflights are immutable'); END;
CREATE TRIGGER IF NOT EXISTS lagent_preflights_no_delete BEFORE DELETE ON lagent_experiment_preflights
BEGIN SELECT RAISE(ABORT, 'experiment preflights are permanent'); END;

CREATE TABLE IF NOT EXISTS lagent_bundle_rows (
  bundle_id TEXT NOT NULL REFERENCES lagent_records(record_id),
  dataset TEXT NOT NULL,
  row_id TEXT NOT NULL,
  natural_key TEXT NOT NULL,
  security TEXT,
  trade_date TEXT,
  event_at TEXT NOT NULL,
  available_at TEXT NOT NULL,
  time_quality TEXT NOT NULL,
  file_hash TEXT NOT NULL DEFAULT '',
  value_json TEXT NOT NULL CHECK(json_valid(value_json)),
  value_hash TEXT NOT NULL,
  PRIMARY KEY(bundle_id, dataset, row_id),
  UNIQUE(bundle_id, dataset, natural_key)
);
CREATE INDEX IF NOT EXISTS lagent_bundle_lookup ON lagent_bundle_rows(bundle_id, dataset, trade_date, security, event_at, available_at);
CREATE TRIGGER IF NOT EXISTS lagent_bundle_rows_no_update BEFORE UPDATE ON lagent_bundle_rows
BEGIN SELECT RAISE(ABORT, 'historical bundle rows are immutable'); END;
CREATE TRIGGER IF NOT EXISTS lagent_bundle_rows_no_delete BEFORE DELETE ON lagent_bundle_rows
BEGIN SELECT RAISE(ABORT, 'historical bundle rows are permanent'); END;

-- One scheduling index for business Requests and historical experiment Tests.
-- Source rows keep their own domain state; this table owns ordering and dispatch.
CREATE TABLE IF NOT EXISTS research_work_queue (
  work_id TEXT PRIMARY KEY,
  kind TEXT NOT NULL CHECK(kind IN ('request', 'experiment')),
  state TEXT NOT NULL CHECK(state IN ('queued', 'running', 'waiting', 'finished')),
  source_status TEXT NOT NULL,
  priority INTEGER NOT NULL CHECK(priority IN (0, 1)),
  enqueued_at TEXT NOT NULL,
  claimed_by TEXT,
  service_owner_id TEXT,
  input_record_id TEXT REFERENCES lagent_records(record_id),
  cancel_requested INTEGER NOT NULL DEFAULT 0 CHECK(cancel_requested IN (0, 1)),
  CHECK((kind='request' AND input_record_id IS NULL) OR (kind='experiment' AND input_record_id IS NOT NULL))
);
CREATE INDEX IF NOT EXISTS research_work_pending ON research_work_queue(state, priority, enqueued_at, work_id);
CREATE TRIGGER IF NOT EXISTS research_request_queue_insert AFTER INSERT ON research_requests
BEGIN
  INSERT INTO research_work_queue(work_id, kind, state, source_status, priority, enqueued_at, claimed_by, cancel_requested)
  VALUES (NEW.request_id, 'request', CASE WHEN NEW.status IN ('queued','running') THEN NEW.status ELSE 'finished' END,
          NEW.status, CASE WHEN NEW.origin='scheduled' THEN 1 ELSE 0 END, NEW.accepted_at, NEW.claimed_by, NEW.cancel_requested);
END;
CREATE TRIGGER IF NOT EXISTS research_request_queue_update AFTER UPDATE ON research_requests
BEGIN
  UPDATE research_work_queue SET
    state=CASE WHEN NEW.status IN ('queued','running') THEN NEW.status ELSE 'finished' END,
    source_status=NEW.status, claimed_by=NEW.claimed_by, cancel_requested=NEW.cancel_requested,
    service_owner_id=CASE WHEN NEW.status='running' THEN service_owner_id ELSE NULL END
  WHERE work_id=NEW.request_id AND kind='request';
END;
CREATE TRIGGER IF NOT EXISTS research_request_queue_delete AFTER DELETE ON research_requests
BEGIN DELETE FROM research_work_queue WHERE work_id=OLD.request_id AND kind='request'; END;
INSERT OR IGNORE INTO research_work_queue(work_id, kind, state, source_status, priority, enqueued_at, claimed_by, cancel_requested)
SELECT request_id, 'request', CASE WHEN status IN ('queued','running') THEN status ELSE 'finished' END,
       status, CASE WHEN origin='scheduled' THEN 1 ELSE 0 END, accepted_at, claimed_by, cancel_requested
FROM research_requests;
CREATE TRIGGER IF NOT EXISTS experiment_queue_status_insert AFTER INSERT ON lagent_projections WHEN NEW.name='status'
BEGIN
  UPDATE research_work_queue SET source_status=json_extract(NEW.value_json,'$.status'),
    state=CASE WHEN json_extract(NEW.value_json,'$.status') IN ('completed','cancelled','blocked','failed') THEN 'finished' ELSE state END
  WHERE work_id=NEW.test_id AND kind='experiment';
END;
CREATE TRIGGER IF NOT EXISTS experiment_queue_status_update AFTER UPDATE ON lagent_projections WHEN NEW.name='status'
BEGIN
  UPDATE research_work_queue SET source_status=json_extract(NEW.value_json,'$.status'),
    state=CASE WHEN json_extract(NEW.value_json,'$.status') IN ('completed','cancelled','blocked','failed') THEN 'finished' ELSE state END
  WHERE work_id=NEW.test_id AND kind='experiment';
END;
INSERT OR IGNORE INTO research_work_queue(work_id,kind,state,source_status,priority,enqueued_at,input_record_id,cancel_requested)
SELECT l.target_id, 'experiment',
       CASE WHEN json_extract(p.value_json,'$.status') IN ('completed','cancelled','blocked','failed') THEN 'finished' ELSE 'queued' END,
       json_extract(p.value_json,'$.status'), 0, r.created_at, r.record_id,
       EXISTS(SELECT 1 FROM lagent_records c JOIN lagent_record_links cl ON cl.record_id=c.record_id
              WHERE c.kind='service_control' AND json_extract(c.value_json,'$.operation')='cancel' AND cl.relation='test' AND cl.target_id=l.target_id)
FROM lagent_records r JOIN lagent_record_links l ON l.record_id=r.record_id AND l.relation='test'
JOIN lagent_projections p ON p.test_id=l.target_id AND p.name='status'
WHERE r.kind='service_input' AND json_extract(r.value_json,'$.queue_version')=1;
