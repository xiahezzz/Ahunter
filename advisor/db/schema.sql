PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

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
  volume REAL NOT NULL,
  amount REAL NOT NULL DEFAULT 0,
  adj_factor REAL,
  limit_up REAL,
  limit_down REAL,
  source TEXT NOT NULL,
  fetched_at TEXT NOT NULL,
  as_of_date TEXT NOT NULL,
  schema_version INTEGER NOT NULL DEFAULT 1,
  content_hash TEXT NOT NULL,
  quality_status TEXT NOT NULL DEFAULT 'passed',
  PRIMARY KEY(code, trade_date)
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
CREATE INDEX IF NOT EXISTS evidence_code_asof_idx ON evidence(code, as_of);
CREATE INDEX IF NOT EXISTS advice_run_idx ON advice(run_id);
CREATE INDEX IF NOT EXISTS reviews_advice_idx ON reviews(advice_id);
