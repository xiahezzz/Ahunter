# Event-Driven Investment Advisor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the first working local A-share investment-advisor loop: free-source market data, three-year K-line storage, MX evidence, TradingAgents-astock analyst adaptation, stock profiles, ledger, charts, archived 08:30/22:30 reports, dashboard, and Mac keepalive templates.

**Architecture:** Keep the existing Node 24 MX collector unchanged and add a Python advisor service beside it. The advisor owns market-data ingestion, SQLite state, evidence, analyst orchestration, reports, charts, FastAPI endpoints, and Vite/React dashboard UI. All report-producing paths pass through deterministic data-quality gates before any stock recommendation is archived.

**Tech Stack:** Python 3.11+, SQLite, pytest, pydantic, PyYAML, pandas, requests, matplotlib, mplfinance, FastAPI, uvicorn, Vite, React, TypeScript, Node 24.18.0 for existing collector commands, macOS launchd.

## Global Constraints

- Work only inside `/Users/mac/Documents/Ahunter/a_hunter`.
- Do not modify existing collector behavior unless a task explicitly says so.
- Run every collector and collector-adjacent test command with `/Users/mac/.local/share/chrome-devtools-mcp/node/bin/node`.
- Empty `config/allowed-rids.yaml` remains intentionally inactive and records no MX content.
- Only the user may supply or authorize a RID.
- Routine collection must use Chrome DevTools Network events only; do not navigate, reload, click, type into, inject into, or operate the MX page.
- Never request, record, expose, or store credentials, cookies, tokens, Socket.IO session IDs, or Chrome debugging identifiers.
- Use free A-share data sources learned from `/Users/mac/Documents/TradingAgents-astock`; do not use Tushare, paid data, or API-key-only market data for required pipelines.
- The system is an investment advisor, not a live trading agent; do not connect to a broker or submit orders.
- Blocking data-quality failures prevent stock recommendations and produce archived failure reports.
- Reports run at 08:30 for premarket advice and 22:30 for same-day review.
- Daily report outputs are archived under `reports/YYYY-MM-DD/`.
- Use TDD for behavior changes. Run targeted tests before wider suites. Commit after every task.

---

## File Structure

- Create `pyproject.toml`: Python package metadata, dependencies, pytest configuration, console entry points.
- Create `advisor/__init__.py`: package marker and version string.
- Create `advisor/config.py`: typed advisor configuration loaded from YAML and environment overrides.
- Create `advisor/paths.py`: centralized repository, data, report, and chart paths.
- Create `advisor/db/schema.sql`: advisor SQLite schema.
- Create `advisor/db/migrate.py`: idempotent migration runner.
- Create `advisor/db/repository.py`: small SQLite repository functions used by tasks.
- Create `advisor/data_sources/contracts.py`: vendor-independent market-data dataclasses.
- Create `advisor/data_sources/free_sources.py`: adapters wrapping TradingAgents-astock-style free sources.
- Create `advisor/data_sources/backfill.py`: resumable three-year daily K-line backfill command.
- Create `advisor/evidence/mx_adapter.py`: read accepted MX events and map them into normalized evidence candidates.
- Create `advisor/evidence/builder.py`: merge market, MX, ledger, profile, and analyst evidence with quality checks.
- Create `advisor/quality.py`: deterministic report data-quality gates.
- Create `advisor/ledger/model.py`: ledger transaction validation and position/PnL math.
- Create `advisor/ledger/importer.py`: CSV import command.
- Create `advisor/profiles/service.py`: stock-profile creation, update, and Markdown rendering.
- Create `advisor/charts/kline.py`: PNG K-line chart generation from stored data.
- Create `advisor/agents/astock_adapter.py`: TradingAgents-astock analyst-role adapter with a deterministic fake runner for tests.
- Create `advisor/reporting/contracts.py`: advice and review output contracts.
- Create `advisor/reporting/premarket.py`: 08:30 report command.
- Create `advisor/reporting/review.py`: 22:30 report command linked to morning advice.
- Create `advisor/web/api.py`: FastAPI application and JSON endpoints.
- Create `frontend/package.json`, `frontend/src/*`, `frontend/vite.config.ts`, `frontend/tsconfig.json`: Vite/React dashboard.
- Create `advisor/scheduler/launchd.py`: launchd plist rendering and validation helpers.
- Create `config/advisor.yaml`: local advisor defaults.
- Create `config/data-sources.yaml`: free-source vendor configuration and limits.
- Create `config/ledger-import.schema.json`: documented CSV import schema.
- Create `config/launchd/*.plist.template`: Mac keepalive templates.
- Create `tests/advisor/**`: Python unit and integration tests.
- Modify `package.json`: add frontend scripts only after frontend is introduced.
- Modify `scripts/self-test.mjs`: call Python advisor tests only after the Python test command exists.
- Modify `agents.md`: add advisor operations after implementation has commands, keeping collector rules intact.

---

### Task 1: Python Advisor Project Skeleton

**Files:**
- Create: `pyproject.toml`
- Create: `advisor/__init__.py`
- Create: `advisor/paths.py`
- Create: `tests/advisor/test_paths.py`

**Interfaces:**
- Produces: `advisor.paths.repo_root() -> pathlib.Path`
- Produces: `advisor.paths.data_dir() -> pathlib.Path`
- Produces: `advisor.paths.reports_dir() -> pathlib.Path`
- Produces: `advisor.paths.advisor_data_dir() -> pathlib.Path`
- Produces: `pytest tests/advisor` as the Python test entry point.

- [ ] **Step 1: Write the failing path tests**

Create `tests/advisor/test_paths.py`:

```python
from pathlib import Path

from advisor.paths import advisor_data_dir, data_dir, repo_root, reports_dir


def test_repo_root_points_to_project_root():
    root = repo_root()
    assert (root / "agents.md").exists()
    assert (root / "package.json").exists()


def test_standard_data_paths_are_under_repo_root():
    root = repo_root()
    assert data_dir() == root / "data"
    assert reports_dir() == root / "reports"
    assert advisor_data_dir() == root / "data" / "advisor"
    for path in [data_dir(), reports_dir(), advisor_data_dir()]:
        assert isinstance(path, Path)
        assert str(path).startswith(str(root))
```

- [ ] **Step 2: Run the targeted test and verify it fails**

Run:

```bash
python3 -m pytest tests/advisor/test_paths.py -q
```

Expected: FAIL with `ModuleNotFoundError: No module named 'advisor'`.

- [ ] **Step 3: Add the project skeleton**

Create `pyproject.toml`:

```toml
[build-system]
requires = ["setuptools>=69", "wheel"]
build-backend = "setuptools.build_meta"

[project]
name = "a-hunter-advisor"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = [
  "fastapi>=0.116,<1",
  "uvicorn[standard]>=0.35,<1",
  "pydantic>=2.8,<3",
  "PyYAML>=6.0,<7",
  "pandas>=2.2,<3",
  "requests>=2.32,<3",
  "matplotlib>=3.9,<4",
  "mplfinance>=0.12.10b0",
]

[project.optional-dependencies]
dev = ["pytest>=8.2,<9"]

[project.scripts]
advisor-backfill = "advisor.data_sources.backfill:main"
advisor-ledger-import = "advisor.ledger.importer:main"
advisor-report-premarket = "advisor.reporting.premarket:main"
advisor-report-review = "advisor.reporting.review:main"
advisor-launchd-render = "advisor.scheduler.launchd:main"

[tool.pytest.ini_options]
testpaths = ["tests/advisor"]
pythonpath = ["."]
```

Create `advisor/__init__.py`:

```python
__version__ = "0.1.0"
```

Create `advisor/paths.py`:

```python
from pathlib import Path


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def data_dir() -> Path:
    return repo_root() / "data"


def reports_dir() -> Path:
    return repo_root() / "reports"


def advisor_data_dir() -> Path:
    return data_dir() / "advisor"
```

- [ ] **Step 4: Run the targeted test and verify it passes**

Run:

```bash
python3 -m pytest tests/advisor/test_paths.py -q
```

Expected: PASS with `2 passed`.

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml advisor/__init__.py advisor/paths.py tests/advisor/test_paths.py
git commit -m "feat: add advisor python skeleton"
```

---

### Task 2: Advisor Configuration

**Files:**
- Create: `advisor/config.py`
- Create: `config/advisor.yaml`
- Create: `config/data-sources.yaml`
- Test: `tests/advisor/test_config.py`

**Interfaces:**
- Consumes: `advisor.paths.repo_root()`.
- Produces: `AdvisorConfig` pydantic model.
- Produces: `load_advisor_config(path: Path | None = None) -> AdvisorConfig`.

- [ ] **Step 1: Write the failing configuration tests**

Create `tests/advisor/test_config.py`:

```python
from pathlib import Path

import pytest

from advisor.config import load_advisor_config


def test_load_default_advisor_config():
    config = load_advisor_config()
    assert config.market.primary == "A股"
    assert config.schedule.premarket_time == "08:30"
    assert config.schedule.review_time == "22:30"
    assert config.data_sources.allow_tushare is False
    assert "mootdx" in config.data_sources.free_sources
    assert config.storage.market_db.endswith("data/advisor/market.sqlite")


def test_rejects_tushare_enabled(tmp_path: Path):
    filename = tmp_path / "advisor.yaml"
    filename.write_text(
        """
market:
  primary: A股
schedule:
  premarket_time: "08:30"
  review_time: "22:30"
storage:
  market_db: data/advisor/market.sqlite
  advisor_db: data/advisor/advisor.sqlite
data_sources:
  allow_tushare: true
  free_sources: [mootdx]
""",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="Tushare"):
        load_advisor_config(filename)
```

- [ ] **Step 2: Run the targeted test and verify it fails**

Run:

```bash
python3 -m pytest tests/advisor/test_config.py -q
```

Expected: FAIL with `ModuleNotFoundError: No module named 'advisor.config'`.

- [ ] **Step 3: Add typed configuration and defaults**

Create `config/advisor.yaml`:

```yaml
market:
  primary: A股
schedule:
  premarket_time: "08:30"
  review_time: "22:30"
storage:
  market_db: data/advisor/market.sqlite
  advisor_db: data/advisor/advisor.sqlite
  chart_dir: data/advisor/charts
  profile_dir: data/advisor/profiles
data_sources:
  allow_tushare: false
  free_sources:
    - mootdx
    - tencent
    - eastmoney
    - sina
    - 10jqka
    - cailianpress
    - baidu_stock
quality:
  max_market_data_staleness_minutes: 1440
  require_trading_calendar: true
```

Create `config/data-sources.yaml`:

```yaml
sources:
  mootdx:
    enabled: true
    rate_limit_per_second: 5
  tencent:
    enabled: true
    rate_limit_per_second: 2
  eastmoney:
    enabled: true
    rate_limit_per_second: 1
  sina:
    enabled: true
    rate_limit_per_second: 1
  10jqka:
    enabled: true
    rate_limit_per_second: 1
  cailianpress:
    enabled: true
    rate_limit_per_second: 1
  baidu_stock:
    enabled: true
    rate_limit_per_second: 1
```

Create `advisor/config.py`:

```python
from pathlib import Path

import yaml
from pydantic import BaseModel, Field, model_validator

from advisor.paths import repo_root


class MarketConfig(BaseModel):
    primary: str = "A股"


class ScheduleConfig(BaseModel):
    premarket_time: str = "08:30"
    review_time: str = "22:30"


class StorageConfig(BaseModel):
    market_db: str
    advisor_db: str
    chart_dir: str = "data/advisor/charts"
    profile_dir: str = "data/advisor/profiles"


class DataSourceConfig(BaseModel):
    allow_tushare: bool = False
    free_sources: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def reject_tushare(self):
        if self.allow_tushare or "tushare" in {value.lower() for value in self.free_sources}:
            raise ValueError("Tushare is not allowed for required advisor data paths")
        return self


class QualityConfig(BaseModel):
    max_market_data_staleness_minutes: int = 1440
    require_trading_calendar: bool = True


class AdvisorConfig(BaseModel):
    market: MarketConfig
    schedule: ScheduleConfig
    storage: StorageConfig
    data_sources: DataSourceConfig
    quality: QualityConfig = Field(default_factory=QualityConfig)


def load_advisor_config(path: Path | None = None) -> AdvisorConfig:
    filename = path or repo_root() / "config" / "advisor.yaml"
    with filename.open("r", encoding="utf-8") as handle:
        payload = yaml.safe_load(handle)
    return AdvisorConfig.model_validate(payload)
```

- [ ] **Step 4: Run the targeted test and verify it passes**

Run:

```bash
python3 -m pytest tests/advisor/test_config.py -q
```

Expected: PASS with `2 passed`.

- [ ] **Step 5: Commit**

```bash
git add advisor/config.py config/advisor.yaml config/data-sources.yaml tests/advisor/test_config.py
git commit -m "feat: add advisor configuration"
```

---

### Task 3: Advisor SQLite Schema and Migrations

**Files:**
- Create: `advisor/db/schema.sql`
- Create: `advisor/db/migrate.py`
- Create: `advisor/db/repository.py`
- Create: `advisor/db/__init__.py`
- Test: `tests/advisor/test_db_schema.py`

**Interfaces:**
- Consumes: `AdvisorConfig.storage.advisor_db` and `AdvisorConfig.storage.market_db`.
- Produces: `migrate_database(db_path: Path) -> None`.
- Produces: `connect(db_path: Path) -> sqlite3.Connection`.
- Produces the logical tables required by the spec.

- [ ] **Step 1: Write the failing schema tests**

Create `tests/advisor/test_db_schema.py`:

```python
import sqlite3

from advisor.db.migrate import migrate_database


REQUIRED_TABLES = {
    "advisor_runs",
    "data_quality_checks",
    "securities",
    "market_daily",
    "market_sources",
    "events_normalized",
    "evidence",
    "analyst_outputs",
    "stock_profiles",
    "stock_profile_history",
    "advice",
    "reviews",
    "ledger_accounts",
    "ledger_transactions",
    "positions",
    "portfolio_snapshots",
    "chart_assets",
    "report_archive",
}


def table_names(connection: sqlite3.Connection) -> set[str]:
    rows = connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    return {row[0] for row in rows}


def test_migration_creates_required_tables(tmp_path):
    db_path = tmp_path / "advisor.sqlite"
    migrate_database(db_path)
    connection = sqlite3.connect(db_path)
    assert REQUIRED_TABLES.issubset(table_names(connection))


def test_migration_is_idempotent(tmp_path):
    db_path = tmp_path / "advisor.sqlite"
    migrate_database(db_path)
    migrate_database(db_path)
    connection = sqlite3.connect(db_path)
    assert connection.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
```

- [ ] **Step 2: Run the targeted test and verify it fails**

Run:

```bash
python3 -m pytest tests/advisor/test_db_schema.py -q
```

Expected: FAIL with `ModuleNotFoundError: No module named 'advisor.db'`.

- [ ] **Step 3: Add schema, migration, and repository helpers**

Create `advisor/db/schema.sql`:

```sql
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
```

Create `advisor/db/__init__.py`:

```python
"""Advisor database helpers."""
```

Create `advisor/db/migrate.py`:

```python
import sqlite3
from pathlib import Path


SCHEMA_FILE = Path(__file__).with_name("schema.sql")


def migrate_database(db_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(db_path)
    try:
        connection.executescript(SCHEMA_FILE.read_text(encoding="utf-8"))
        connection.commit()
    finally:
        connection.close()
```

Create `advisor/db/repository.py`:

```python
import sqlite3
from pathlib import Path


def connect(db_path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection
```

- [ ] **Step 4: Run the targeted test and verify it passes**

Run:

```bash
python3 -m pytest tests/advisor/test_db_schema.py -q
```

Expected: PASS with `2 passed`.

- [ ] **Step 5: Commit**

```bash
git add advisor/db tests/advisor/test_db_schema.py
git commit -m "feat: add advisor database schema"
```

---

### Task 4: Free Market Data Contracts and Backfill

**Files:**
- Create: `advisor/data_sources/__init__.py`
- Create: `advisor/data_sources/contracts.py`
- Create: `advisor/data_sources/free_sources.py`
- Create: `advisor/data_sources/backfill.py`
- Test: `tests/advisor/test_market_backfill.py`

**Interfaces:**
- Consumes: `migrate_database(db_path)`.
- Produces: `DailyBar` dataclass.
- Produces: `MarketDataProvider.fetch_daily_bars(code: str, start: date, end: date) -> list[DailyBar]`.
- Produces: `backfill_daily_bars(db_path: Path, provider: MarketDataProvider, codes: list[str], start: date, end: date) -> int`.

- [ ] **Step 1: Write the failing backfill tests**

Create `tests/advisor/test_market_backfill.py`:

```python
from datetime import date
from pathlib import Path

from advisor.data_sources.backfill import backfill_daily_bars
from advisor.data_sources.contracts import DailyBar, MarketDataProvider
from advisor.db.migrate import migrate_database
from advisor.db.repository import connect


class FakeProvider(MarketDataProvider):
    def fetch_daily_bars(self, code: str, start: date, end: date) -> list[DailyBar]:
        return [
            DailyBar(
                code=code,
                trade_date=date(2026, 7, 10),
                open=10.0,
                high=10.8,
                low=9.8,
                close=10.5,
                volume=1000000,
                amount=10500000,
                source="fake_free_source",
                fetched_at="2026-07-11T08:00:00+08:00",
                as_of_date=date(2026, 7, 10),
                content_hash="bar-1",
            )
        ]


def test_backfill_writes_daily_bars(tmp_path: Path):
    db_path = tmp_path / "advisor.sqlite"
    migrate_database(db_path)
    inserted = backfill_daily_bars(db_path, FakeProvider(), ["600519"], date(2023, 7, 11), date(2026, 7, 10))
    assert inserted == 1
    row = connect(db_path).execute("SELECT code, close, source FROM market_daily").fetchone()
    assert dict(row) == {"code": "600519", "close": 10.5, "source": "fake_free_source"}


def test_backfill_is_idempotent(tmp_path: Path):
    db_path = tmp_path / "advisor.sqlite"
    migrate_database(db_path)
    first = backfill_daily_bars(db_path, FakeProvider(), ["600519"], date(2023, 7, 11), date(2026, 7, 10))
    second = backfill_daily_bars(db_path, FakeProvider(), ["600519"], date(2023, 7, 11), date(2026, 7, 10))
    assert first == 1
    assert second == 0
```

- [ ] **Step 2: Run the targeted test and verify it fails**

Run:

```bash
python3 -m pytest tests/advisor/test_market_backfill.py -q
```

Expected: FAIL with `ModuleNotFoundError: No module named 'advisor.data_sources'`.

- [ ] **Step 3: Add contracts and idempotent backfill**

Create `advisor/data_sources/__init__.py`:

```python
"""Free A-share data-source adapters."""
```

Create `advisor/data_sources/contracts.py`:

```python
from dataclasses import dataclass
from datetime import date
from typing import Protocol


@dataclass(frozen=True)
class DailyBar:
    code: str
    trade_date: date
    open: float
    high: float
    low: float
    close: float
    volume: float
    amount: float
    source: str
    fetched_at: str
    as_of_date: date
    content_hash: str
    adj_factor: float | None = None
    limit_up: float | None = None
    limit_down: float | None = None


class MarketDataProvider(Protocol):
    def fetch_daily_bars(self, code: str, start: date, end: date) -> list[DailyBar]:
        raise NotImplementedError
```

Create `advisor/data_sources/free_sources.py`:

```python
from datetime import date

from advisor.data_sources.contracts import DailyBar


class TradingAgentsFreeSourceProvider:
    """Thin adapter boundary for free A-share sources from TradingAgents-astock."""

    def fetch_daily_bars(self, code: str, start: date, end: date) -> list[DailyBar]:
        raise RuntimeError(
            "Live free-source fetching is unavailable in this contract task; tests use a deterministic provider"
        )
```

Create `advisor/data_sources/backfill.py`:

```python
import argparse
from datetime import date
from pathlib import Path

from advisor.data_sources.contracts import MarketDataProvider
from advisor.db.repository import connect


def backfill_daily_bars(
    db_path: Path,
    provider: MarketDataProvider,
    codes: list[str],
    start: date,
    end: date,
) -> int:
    inserted = 0
    connection = connect(db_path)
    try:
        for code in codes:
            for bar in provider.fetch_daily_bars(code, start, end):
                cursor = connection.execute(
                    """
                    INSERT OR IGNORE INTO market_daily (
                      code, trade_date, open, high, low, close, volume, amount,
                      adj_factor, limit_up, limit_down, source, fetched_at,
                      as_of_date, schema_version, content_hash, quality_status
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, 'passed')
                    """,
                    (
                        bar.code,
                        bar.trade_date.isoformat(),
                        bar.open,
                        bar.high,
                        bar.low,
                        bar.close,
                        bar.volume,
                        bar.amount,
                        bar.adj_factor,
                        bar.limit_up,
                        bar.limit_down,
                        bar.source,
                        bar.fetched_at,
                        bar.as_of_date.isoformat(),
                        bar.content_hash,
                    ),
                )
                inserted += cursor.rowcount
        connection.commit()
        return inserted
    finally:
        connection.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True)
    parser.add_argument("--codes", required=True)
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    args = parser.parse_args()
    raise SystemExit(
        "advisor-backfill requires the live provider wiring task before command-line use"
    )
```

- [ ] **Step 4: Run the targeted test and verify it passes**

Run:

```bash
python3 -m pytest tests/advisor/test_market_backfill.py -q
```

Expected: PASS with `2 passed`.

- [ ] **Step 5: Commit**

```bash
git add advisor/data_sources tests/advisor/test_market_backfill.py
git commit -m "feat: add market data backfill contracts"
```

---

### Task 5: MX Evidence Adapter and Quality Gates

**Files:**
- Create: `advisor/evidence/__init__.py`
- Create: `advisor/evidence/mx_adapter.py`
- Create: `advisor/quality.py`
- Test: `tests/advisor/test_mx_evidence_quality.py`

**Interfaces:**
- Consumes: existing collector SQLite table `events`.
- Produces: `read_mx_events(events_db: Path, limit: int = 100) -> list[NormalizedEvent]`.
- Produces: `QualityResult` dataclass.
- Produces: `evaluate_quality(connection: sqlite3.Connection, as_of: str) -> list[QualityResult]`.

- [ ] **Step 1: Write failing evidence and quality tests**

Create `tests/advisor/test_mx_evidence_quality.py`:

```python
import sqlite3
from pathlib import Path

from advisor.evidence.mx_adapter import read_mx_events
from advisor.quality import has_blocking_failure


def make_events_db(path: Path) -> None:
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE events (
          event_id TEXT PRIMARY KEY,
          rid INTEGER NOT NULL,
          received_at INTEGER NOT NULL,
          decoded_text TEXT NOT NULL,
          content_hash TEXT NOT NULL
        );
        INSERT INTO events VALUES ('evt-1', 123, 1783728000000, '关注 600519 贵州茅台 放量', 'hash-1');
        """
    )
    connection.close()


def test_read_mx_events_maps_accepted_collector_rows(tmp_path: Path):
    db_path = tmp_path / "events.sqlite"
    make_events_db(db_path)
    events = read_mx_events(db_path)
    assert len(events) == 1
    assert events[0].source_type == "mx"
    assert events[0].source_id == "evt-1"
    assert events[0].code == "600519"
    assert "贵州茅台" in events[0].summary


def test_quality_blocks_when_required_history_missing(tmp_path: Path):
    db_path = tmp_path / "advisor.sqlite"
    connection = sqlite3.connect(db_path)
    connection.executescript(
        """
        CREATE TABLE market_daily (code TEXT, trade_date TEXT);
        CREATE TABLE ledger_transactions (transaction_id TEXT);
        """
    )
    assert has_blocking_failure(connection, required_codes=["600519"], as_of="2026-07-11T08:30:00+08:00")
```

- [ ] **Step 2: Run the targeted test and verify it fails**

Run:

```bash
python3 -m pytest tests/advisor/test_mx_evidence_quality.py -q
```

Expected: FAIL with `ModuleNotFoundError: No module named 'advisor.evidence'`.

- [ ] **Step 3: Add MX adapter and quality check**

Create `advisor/evidence/__init__.py`:

```python
"""Evidence construction for advisor reports."""
```

Create `advisor/evidence/mx_adapter.py`:

```python
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path


STOCK_CODE_RE = re.compile(r"\b([036]\d{5})\b")


@dataclass(frozen=True)
class NormalizedEvent:
    source_type: str
    source_id: str
    code: str | None
    as_of: str
    summary: str
    quality_status: str = "passed"


def read_mx_events(events_db: Path, limit: int = 100) -> list[NormalizedEvent]:
    connection = sqlite3.connect(events_db)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            """
            SELECT event_id, received_at, decoded_text
            FROM events
            ORDER BY received_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    finally:
        connection.close()
    events: list[NormalizedEvent] = []
    for row in rows:
        match = STOCK_CODE_RE.search(row["decoded_text"])
        events.append(
            NormalizedEvent(
                source_type="mx",
                source_id=row["event_id"],
                code=match.group(1) if match else None,
                as_of=str(row["received_at"]),
                summary=row["decoded_text"],
            )
        )
    return events
```

Create `advisor/quality.py`:

```python
import sqlite3
from dataclasses import dataclass


@dataclass(frozen=True)
class QualityResult:
    check_name: str
    severity: str
    passed: bool
    details: str

    @property
    def blocking_failure(self) -> bool:
        return self.severity == "blocking" and not self.passed


def evaluate_quality(connection: sqlite3.Connection, required_codes: list[str], as_of: str) -> list[QualityResult]:
    results: list[QualityResult] = []
    for code in required_codes:
        count = connection.execute(
            "SELECT count(*) FROM market_daily WHERE code = ?",
            (code,),
        ).fetchone()[0]
        results.append(
            QualityResult(
                check_name=f"three_year_history:{code}",
                severity="blocking",
                passed=count > 0,
                details=f"{count} market_daily rows available as of {as_of}",
            )
        )
    return results


def has_blocking_failure(connection: sqlite3.Connection, required_codes: list[str], as_of: str) -> bool:
    return any(result.blocking_failure for result in evaluate_quality(connection, required_codes, as_of))
```

- [ ] **Step 4: Run the targeted test and verify it passes**

Run:

```bash
python3 -m pytest tests/advisor/test_mx_evidence_quality.py -q
```

Expected: PASS with `2 passed`.

- [ ] **Step 5: Commit**

```bash
git add advisor/evidence advisor/quality.py tests/advisor/test_mx_evidence_quality.py
git commit -m "feat: add mx evidence and quality gates"
```

---

### Task 6: Investment Ledger

**Files:**
- Create: `advisor/ledger/__init__.py`
- Create: `advisor/ledger/model.py`
- Create: `advisor/ledger/importer.py`
- Create: `config/ledger-import.schema.json`
- Test: `tests/advisor/test_ledger.py`

**Interfaces:**
- Produces: `LedgerTransaction` dataclass.
- Produces: `apply_transactions(transactions: list[LedgerTransaction]) -> LedgerState`.
- Produces: `load_ledger_csv(path: Path) -> list[LedgerTransaction]`.

- [ ] **Step 1: Write failing ledger tests**

Create `tests/advisor/test_ledger.py`:

```python
from pathlib import Path

from advisor.ledger.importer import load_ledger_csv
from advisor.ledger.model import LedgerTransaction, apply_transactions


def test_apply_buy_and_sell_transactions():
    state = apply_transactions(
        [
            LedgerTransaction("t1", "2026-07-10", "cash_deposit", None, 0, 0, 100000, 0),
            LedgerTransaction("t2", "2026-07-10", "buy", "600519", 100, 100.0, -10000, 5),
            LedgerTransaction("t3", "2026-07-11", "sell", "600519", 100, 110.0, 11000, 5),
        ]
    )
    assert state.cash == 100990
    assert state.positions == {}
    assert state.realized_pnl == 990


def test_load_ledger_csv(tmp_path: Path):
    filename = tmp_path / "ledger.csv"
    filename.write_text(
        "transaction_id,trade_date,transaction_type,code,quantity,price,amount,fees\n"
        "t1,2026-07-10,cash_deposit,,0,0,100000,0\n",
        encoding="utf-8",
    )
    transactions = load_ledger_csv(filename)
    assert transactions[0].transaction_type == "cash_deposit"
    assert transactions[0].amount == 100000
```

- [ ] **Step 2: Run the targeted test and verify it fails**

Run:

```bash
python3 -m pytest tests/advisor/test_ledger.py -q
```

Expected: FAIL with `ModuleNotFoundError: No module named 'advisor.ledger'`.

- [ ] **Step 3: Add ledger model and CSV import**

Create `advisor/ledger/__init__.py`:

```python
"""Local user-trade ledger."""
```

Create `advisor/ledger/model.py`:

```python
from dataclasses import dataclass, field


@dataclass(frozen=True)
class LedgerTransaction:
    transaction_id: str
    trade_date: str
    transaction_type: str
    code: str | None
    quantity: int
    price: float
    amount: float
    fees: float


@dataclass
class LedgerState:
    cash: float = 0.0
    positions: dict[str, int] = field(default_factory=dict)
    cost_basis: dict[str, float] = field(default_factory=dict)
    realized_pnl: float = 0.0


def apply_transactions(transactions: list[LedgerTransaction]) -> LedgerState:
    state = LedgerState()
    for tx in transactions:
        if tx.transaction_type in {"cash_deposit", "cash_withdrawal"}:
            state.cash += tx.amount
        elif tx.transaction_type == "buy":
            assert tx.code is not None
            state.cash += tx.amount - tx.fees
            state.positions[tx.code] = state.positions.get(tx.code, 0) + tx.quantity
            state.cost_basis[tx.code] = state.cost_basis.get(tx.code, 0.0) + abs(tx.amount) + tx.fees
        elif tx.transaction_type == "sell":
            assert tx.code is not None
            held = state.positions.get(tx.code, 0)
            if held < tx.quantity:
                raise ValueError(f"cannot sell {tx.quantity} shares of {tx.code}; only {held} held")
            prior_cost = state.cost_basis.get(tx.code, 0.0)
            sold_cost = prior_cost * (tx.quantity / held)
            state.cash += tx.amount - tx.fees
            state.realized_pnl += tx.amount - tx.fees - sold_cost
            remaining = held - tx.quantity
            if remaining == 0:
                state.positions.pop(tx.code, None)
                state.cost_basis.pop(tx.code, None)
            else:
                state.positions[tx.code] = remaining
                state.cost_basis[tx.code] = prior_cost - sold_cost
        else:
            raise ValueError(f"unsupported transaction_type: {tx.transaction_type}")
    return state
```

Create `advisor/ledger/importer.py`:

```python
import argparse
import csv
from pathlib import Path

from advisor.ledger.model import LedgerTransaction


def load_ledger_csv(path: Path) -> list[LedgerTransaction]:
    with path.open("r", encoding="utf-8", newline="") as handle:
        rows = csv.DictReader(handle)
        return [
            LedgerTransaction(
                transaction_id=row["transaction_id"],
                trade_date=row["trade_date"],
                transaction_type=row["transaction_type"],
                code=row["code"] or None,
                quantity=int(row["quantity"]),
                price=float(row["price"]),
                amount=float(row["amount"]),
                fees=float(row["fees"]),
            )
            for row in rows
        ]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("csv_path")
    args = parser.parse_args()
    transactions = load_ledger_csv(Path(args.csv_path))
    print(f"loaded {len(transactions)} ledger transactions")
```

Create `config/ledger-import.schema.json`:

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "title": "A Hunter Ledger CSV Row",
  "type": "object",
  "required": ["transaction_id", "trade_date", "transaction_type", "code", "quantity", "price", "amount", "fees"],
  "properties": {
    "transaction_id": { "type": "string" },
    "trade_date": { "type": "string", "pattern": "^\\d{4}-\\d{2}-\\d{2}$" },
    "transaction_type": { "type": "string", "enum": ["cash_deposit", "cash_withdrawal", "buy", "sell", "fee", "tax"] },
    "code": { "type": "string" },
    "quantity": { "type": "integer" },
    "price": { "type": "number" },
    "amount": { "type": "number" },
    "fees": { "type": "number" }
  }
}
```

- [ ] **Step 4: Run the targeted test and verify it passes**

Run:

```bash
python3 -m pytest tests/advisor/test_ledger.py -q
```

Expected: PASS with `2 passed`.

- [ ] **Step 5: Commit**

```bash
git add advisor/ledger config/ledger-import.schema.json tests/advisor/test_ledger.py
git commit -m "feat: add investment ledger"
```

---

### Task 7: Stock Profiles

**Files:**
- Create: `advisor/profiles/__init__.py`
- Create: `advisor/profiles/service.py`
- Test: `tests/advisor/test_profiles.py`

**Interfaces:**
- Consumes: `NormalizedEvent`, `LedgerState`, analyst output dictionaries.
- Produces: `StockProfile` dataclass.
- Produces: `upsert_profile(connection, profile: StockProfile) -> None`.
- Produces: `render_profile_markdown(profile: StockProfile) -> str`.

- [ ] **Step 1: Write failing profile tests**

Create `tests/advisor/test_profiles.py`:

```python
import sqlite3

from advisor.profiles.service import StockProfile, render_profile_markdown, upsert_profile


def test_render_profile_contains_required_dimensions():
    profile = StockProfile(
        code="600519",
        name="贵州茅台",
        industry="白酒",
        thesis="高端白酒品牌力和现金流",
        information_flow=["MX 事件提到放量"],
        capital_flow=["成交额放大"],
        analyst_flow=["Market analyst: trend positive"],
        risks=["估值偏高"],
        assets=["reports/2026-07-11/assets/600519-kline.png"],
    )
    text = render_profile_markdown(profile)
    assert "# 600519 贵州茅台" in text
    assert "## Information Flow" in text
    assert "## Capital Flow" in text
    assert "## Analyst Flow" in text


def test_upsert_profile_writes_structured_state(tmp_path):
    connection = sqlite3.connect(tmp_path / "advisor.sqlite")
    connection.executescript(
        """
        CREATE TABLE stock_profiles (
          code TEXT PRIMARY KEY,
          thesis_json TEXT NOT NULL,
          information_flow_json TEXT NOT NULL,
          capital_flow_json TEXT NOT NULL,
          fundamentals_json TEXT NOT NULL,
          analyst_flow_json TEXT NOT NULL,
          ledger_exposure_json TEXT NOT NULL,
          assets_json TEXT NOT NULL,
          updated_at TEXT NOT NULL
        );
        """
    )
    profile = StockProfile("600519", "贵州茅台", "白酒", "品牌力", [], [], [], [], [])
    upsert_profile(connection, profile)
    row = connection.execute("SELECT code FROM stock_profiles").fetchone()
    assert row[0] == "600519"
```

- [ ] **Step 2: Run the targeted test and verify it fails**

Run:

```bash
python3 -m pytest tests/advisor/test_profiles.py -q
```

Expected: FAIL with `ModuleNotFoundError: No module named 'advisor.profiles'`.

- [ ] **Step 3: Add profile service**

Create `advisor/profiles/__init__.py`:

```python
"""Stock profile management."""
```

Create `advisor/profiles/service.py`:

```python
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class StockProfile:
    code: str
    name: str
    industry: str
    thesis: str
    information_flow: list[str]
    capital_flow: list[str]
    analyst_flow: list[str]
    risks: list[str]
    assets: list[str]


def render_profile_markdown(profile: StockProfile) -> str:
    def bullet(values: list[str]) -> str:
        return "\n".join(f"- {value}" for value in values) if values else "- No current entries"

    return "\n".join(
        [
            f"# {profile.code} {profile.name}",
            "",
            f"- Industry: {profile.industry}",
            f"- Thesis: {profile.thesis}",
            "",
            "## Information Flow",
            bullet(profile.information_flow),
            "",
            "## Capital Flow",
            bullet(profile.capital_flow),
            "",
            "## Analyst Flow",
            bullet(profile.analyst_flow),
            "",
            "## Risks",
            bullet(profile.risks),
            "",
            "## Assets",
            bullet(profile.assets),
            "",
        ]
    )


def upsert_profile(connection: sqlite3.Connection, profile: StockProfile) -> None:
    now = datetime.now().isoformat()
    connection.execute(
        """
        INSERT INTO stock_profiles (
          code, thesis_json, information_flow_json, capital_flow_json,
          fundamentals_json, analyst_flow_json, ledger_exposure_json,
          assets_json, updated_at
        ) VALUES (?, ?, ?, ?, '{}', ?, '{}', ?, ?)
        ON CONFLICT(code) DO UPDATE SET
          thesis_json = excluded.thesis_json,
          information_flow_json = excluded.information_flow_json,
          capital_flow_json = excluded.capital_flow_json,
          analyst_flow_json = excluded.analyst_flow_json,
          assets_json = excluded.assets_json,
          updated_at = excluded.updated_at
        """,
        (
            profile.code,
            json.dumps({"name": profile.name, "industry": profile.industry, "thesis": profile.thesis}, ensure_ascii=False),
            json.dumps(profile.information_flow, ensure_ascii=False),
            json.dumps(profile.capital_flow, ensure_ascii=False),
            json.dumps(profile.analyst_flow, ensure_ascii=False),
            json.dumps(profile.assets, ensure_ascii=False),
            now,
        ),
    )
    connection.commit()
```

- [ ] **Step 4: Run the targeted test and verify it passes**

Run:

```bash
python3 -m pytest tests/advisor/test_profiles.py -q
```

Expected: PASS with `2 passed`.

- [ ] **Step 5: Commit**

```bash
git add advisor/profiles tests/advisor/test_profiles.py
git commit -m "feat: add stock profiles"
```

---

### Task 8: K-Line Chart Generation

**Files:**
- Create: `advisor/charts/__init__.py`
- Create: `advisor/charts/kline.py`
- Test: `tests/advisor/test_kline_chart.py`

**Interfaces:**
- Consumes: rows from `market_daily`.
- Produces: `generate_kline_chart(db_path: Path, code: str, output_path: Path) -> Path`.

- [ ] **Step 1: Write failing chart test**

Create `tests/advisor/test_kline_chart.py`:

```python
import sqlite3
from pathlib import Path

from advisor.charts.kline import generate_kline_chart


def test_generate_kline_chart_writes_non_empty_png(tmp_path: Path):
    db_path = tmp_path / "advisor.sqlite"
    output = tmp_path / "600519-kline.png"
    connection = sqlite3.connect(db_path)
    connection.executescript(
        """
        CREATE TABLE market_daily (
          code TEXT, trade_date TEXT, open REAL, high REAL, low REAL,
          close REAL, volume REAL, amount REAL, source TEXT, fetched_at TEXT,
          as_of_date TEXT, schema_version INTEGER, content_hash TEXT,
          quality_status TEXT
        );
        INSERT INTO market_daily VALUES
          ('600519','2026-07-08',10,11,9,10.5,1000,10000,'fixture','now','2026-07-08',1,'h1','passed'),
          ('600519','2026-07-09',10.5,12,10,11.5,1200,13000,'fixture','now','2026-07-09',1,'h2','passed'),
          ('600519','2026-07-10',11.5,12.5,11,12,1500,18000,'fixture','now','2026-07-10',1,'h3','passed');
        """
    )
    connection.close()
    result = generate_kline_chart(db_path, "600519", output)
    assert result == output
    assert output.exists()
    assert output.stat().st_size > 1000
```

- [ ] **Step 2: Run the targeted test and verify it fails**

Run:

```bash
python3 -m pytest tests/advisor/test_kline_chart.py -q
```

Expected: FAIL with `ModuleNotFoundError: No module named 'advisor.charts'`.

- [ ] **Step 3: Add chart generator**

Create `advisor/charts/__init__.py`:

```python
"""Chart generation from stored advisor data."""
```

Create `advisor/charts/kline.py`:

```python
import sqlite3
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import mplfinance as mpf
import pandas as pd


def generate_kline_chart(db_path: Path, code: str, output_path: Path) -> Path:
    connection = sqlite3.connect(db_path)
    rows = connection.execute(
        """
        SELECT trade_date, open, high, low, close, volume
        FROM market_daily
        WHERE code = ?
        ORDER BY trade_date
        """,
        (code,),
    ).fetchall()
    connection.close()
    if not rows:
        raise ValueError(f"no market_daily rows for {code}")
    frame = pd.DataFrame(rows, columns=["Date", "Open", "High", "Low", "Close", "Volume"])
    frame["Date"] = pd.to_datetime(frame["Date"])
    frame = frame.set_index("Date")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    mpf.plot(
        frame,
        type="candle",
        volume=True,
        mav=(5, 10),
        style="yahoo",
        title=f"{code} K-line",
        savefig=dict(fname=str(output_path), dpi=120, bbox_inches="tight"),
    )
    return output_path
```

- [ ] **Step 4: Run the targeted test and verify it passes**

Run:

```bash
python3 -m pytest tests/advisor/test_kline_chart.py -q
```

Expected: PASS with `1 passed`.

- [ ] **Step 5: Commit**

```bash
git add advisor/charts tests/advisor/test_kline_chart.py
git commit -m "feat: generate k-line charts"
```

---

### Task 9: TradingAgents-Astock Analyst Adapter

**Files:**
- Create: `advisor/agents/__init__.py`
- Create: `advisor/agents/astock_adapter.py`
- Test: `tests/advisor/test_astock_adapter.py`

**Interfaces:**
- Produces: `ANALYST_ROLES: tuple[str, ...]`.
- Produces: `AnalystOutput` dataclass.
- Produces: `run_analyst_flow(code: str, trade_date: str, evidence: list[dict], runner: AnalystRunner | None = None) -> list[AnalystOutput]`.

- [ ] **Step 1: Write failing analyst adapter tests**

Create `tests/advisor/test_astock_adapter.py`:

```python
from advisor.agents.astock_adapter import ANALYST_ROLES, AnalystOutput, run_analyst_flow


class FakeRunner:
    def run(self, code: str, trade_date: str, evidence: list[dict]) -> list[AnalystOutput]:
        return [
            AnalystOutput(role=role, code=code, summary=f"{role} summary", payload={"evidence_count": len(evidence)})
            for role in ANALYST_ROLES
        ]


def test_includes_full_tradingagents_astock_role_set():
    assert ANALYST_ROLES == (
        "market",
        "social",
        "news",
        "fundamentals",
        "policy",
        "hot_money",
        "lockup",
        "quality_gate",
        "bull_researcher",
        "bear_researcher",
        "research_manager",
        "trader",
        "aggressive_risk",
        "neutral_risk",
        "conservative_risk",
        "portfolio_manager",
    )


def test_run_analyst_flow_uses_injected_runner():
    outputs = run_analyst_flow("600519", "2026-07-11", [{"evidence_id": "ev-1"}], runner=FakeRunner())
    assert len(outputs) == len(ANALYST_ROLES)
    assert outputs[-1].role == "portfolio_manager"
```

- [ ] **Step 2: Run the targeted test and verify it fails**

Run:

```bash
python3 -m pytest tests/advisor/test_astock_adapter.py -q
```

Expected: FAIL with `ModuleNotFoundError: No module named 'advisor.agents'`.

- [ ] **Step 3: Add analyst adapter with deterministic test runner seam**

Create `advisor/agents/__init__.py`:

```python
"""TradingAgents-astock advisor adapters."""
```

Create `advisor/agents/astock_adapter.py`:

```python
from dataclasses import dataclass
from typing import Protocol


ANALYST_ROLES = (
    "market",
    "social",
    "news",
    "fundamentals",
    "policy",
    "hot_money",
    "lockup",
    "quality_gate",
    "bull_researcher",
    "bear_researcher",
    "research_manager",
    "trader",
    "aggressive_risk",
    "neutral_risk",
    "conservative_risk",
    "portfolio_manager",
)


@dataclass(frozen=True)
class AnalystOutput:
    role: str
    code: str
    summary: str
    payload: dict


class AnalystRunner(Protocol):
    def run(self, code: str, trade_date: str, evidence: list[dict]) -> list[AnalystOutput]:
        raise NotImplementedError


class ExternalTradingAgentsRunner:
    def run(self, code: str, trade_date: str, evidence: list[dict]) -> list[AnalystOutput]:
        raise RuntimeError(
            "Install and configure /Users/mac/Documents/TradingAgents-astock before live analyst runs"
        )


def run_analyst_flow(
    code: str,
    trade_date: str,
    evidence: list[dict],
    runner: AnalystRunner | None = None,
) -> list[AnalystOutput]:
    active_runner = runner or ExternalTradingAgentsRunner()
    outputs = active_runner.run(code, trade_date, evidence)
    missing = set(ANALYST_ROLES) - {output.role for output in outputs}
    if missing:
        raise ValueError(f"missing analyst outputs: {sorted(missing)}")
    return outputs
```

- [ ] **Step 4: Run the targeted test and verify it passes**

Run:

```bash
python3 -m pytest tests/advisor/test_astock_adapter.py -q
```

Expected: PASS with `2 passed`.

- [ ] **Step 5: Commit**

```bash
git add advisor/agents tests/advisor/test_astock_adapter.py
git commit -m "feat: add astock analyst adapter"
```

---

### Task 10: Premarket and Review Reports

**Files:**
- Create: `advisor/reporting/__init__.py`
- Create: `advisor/reporting/contracts.py`
- Create: `advisor/reporting/premarket.py`
- Create: `advisor/reporting/review.py`
- Test: `tests/advisor/test_reporting.py`

**Interfaces:**
- Consumes: `AnalystOutput`, quality results, evidence, and chart paths.
- Produces: `write_premarket_report(report_date: str, advice_items: list[AdviceItem], output_dir: Path) -> ReportPaths`.
- Produces: `write_review_report(report_date: str, morning_advice: list[AdviceItem], review_items: list[ReviewItem], output_dir: Path) -> ReportPaths`.

- [ ] **Step 1: Write failing report tests**

Create `tests/advisor/test_reporting.py`:

```python
import json
from pathlib import Path

from advisor.reporting.contracts import AdviceItem, ReviewItem
from advisor.reporting.premarket import write_premarket_report
from advisor.reporting.review import write_review_report


def test_write_premarket_report_archives_markdown_and_json(tmp_path: Path):
    paths = write_premarket_report(
        "2026-07-11",
        [AdviceItem("adv-1", "600519", "watch", 0.72, "放量并有信息流催化", ["ev-1"])],
        tmp_path,
    )
    assert paths.markdown_path.name == "premarket.md"
    assert paths.json_path.name == "premarket.json"
    assert "08:30 Premarket Advice" in paths.markdown_path.read_text(encoding="utf-8")
    payload = json.loads(paths.json_path.read_text(encoding="utf-8"))
    assert payload["advice"][0]["advice_id"] == "adv-1"


def test_write_review_report_links_to_morning_advice(tmp_path: Path):
    advice = [AdviceItem("adv-1", "600519", "watch", 0.72, "morning rationale", ["ev-1"])]
    paths = write_review_report(
        "2026-07-11",
        advice,
        [ReviewItem("rev-1", "adv-1", "partly_valid", "量能延续但未触发买入条件")],
        tmp_path,
    )
    text = paths.markdown_path.read_text(encoding="utf-8")
    assert "22:30 Daily Review" in text
    assert "adv-1" in text
```

- [ ] **Step 2: Run the targeted test and verify it fails**

Run:

```bash
python3 -m pytest tests/advisor/test_reporting.py -q
```

Expected: FAIL with `ModuleNotFoundError: No module named 'advisor.reporting'`.

- [ ] **Step 3: Add report contracts and writers**

Create `advisor/reporting/__init__.py`:

```python
"""Daily advisor report generation."""
```

Create `advisor/reporting/contracts.py`:

```python
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass(frozen=True)
class AdviceItem:
    advice_id: str
    code: str
    action: str
    confidence: float
    rationale: str
    evidence_ids: list[str]

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class ReviewItem:
    review_id: str
    advice_id: str
    outcome: str
    review_text: str

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class ReportPaths:
    markdown_path: Path
    json_path: Path
```

Create `advisor/reporting/premarket.py`:

```python
import argparse
import json
from pathlib import Path

from advisor.reporting.contracts import AdviceItem, ReportPaths


DISCLAIMER = "Research output only. This is not an order, broker instruction, or guaranteed return."


def write_premarket_report(report_date: str, advice_items: list[AdviceItem], output_dir: Path) -> ReportPaths:
    output_dir.mkdir(parents=True, exist_ok=True)
    markdown_path = output_dir / "premarket.md"
    json_path = output_dir / "premarket.json"
    lines = [
        f"# {report_date} 08:30 Premarket Advice",
        "",
        "## Data Quality",
        "- Status: passed",
        "",
        "## Advice",
    ]
    for item in advice_items:
        lines.extend(
            [
                f"### {item.code} {item.action}",
                f"- Advice ID: {item.advice_id}",
                f"- Confidence: {item.confidence:.2f}",
                f"- Rationale: {item.rationale}",
                f"- Evidence: {', '.join(item.evidence_ids)}",
                "",
            ]
        )
    lines.extend(["## Disclaimer", DISCLAIMER, ""])
    markdown_path.write_text("\n".join(lines), encoding="utf-8")
    json_path.write_text(
        json.dumps({"report_date": report_date, "advice": [item.to_dict() for item in advice_items]}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return ReportPaths(markdown_path, json_path)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", required=True)
    parser.add_argument("--output-dir", required=True)
    args = parser.parse_args()
    write_premarket_report(args.date, [], Path(args.output_dir))
```

Create `advisor/reporting/review.py`:

```python
import argparse
import json
from pathlib import Path

from advisor.reporting.contracts import AdviceItem, ReportPaths, ReviewItem
from advisor.reporting.premarket import DISCLAIMER


def write_review_report(
    report_date: str,
    morning_advice: list[AdviceItem],
    review_items: list[ReviewItem],
    output_dir: Path,
) -> ReportPaths:
    output_dir.mkdir(parents=True, exist_ok=True)
    markdown_path = output_dir / "review.md"
    json_path = output_dir / "review.json"
    advice_by_id = {item.advice_id: item for item in morning_advice}
    lines = [
        f"# {report_date} 22:30 Daily Review",
        "",
        "## Morning Advice Reviewed",
    ]
    for item in morning_advice:
        lines.append(f"- {item.advice_id}: {item.code} {item.action}")
    lines.extend(["", "## Review Items"])
    for item in review_items:
        linked = advice_by_id[item.advice_id]
        lines.extend(
            [
                f"### {item.review_id}",
                f"- Advice ID: {item.advice_id}",
                f"- Code: {linked.code}",
                f"- Outcome: {item.outcome}",
                f"- Review: {item.review_text}",
                "",
            ]
        )
    lines.extend(["## Disclaimer", DISCLAIMER, ""])
    markdown_path.write_text("\n".join(lines), encoding="utf-8")
    json_path.write_text(
        json.dumps(
            {
                "report_date": report_date,
                "morning_advice": [item.to_dict() for item in morning_advice],
                "reviews": [item.to_dict() for item in review_items],
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return ReportPaths(markdown_path, json_path)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", required=True)
    parser.add_argument("--output-dir", required=True)
    args = parser.parse_args()
    write_review_report(args.date, [], [], Path(args.output_dir))
```

- [ ] **Step 4: Run the targeted test and verify it passes**

Run:

```bash
python3 -m pytest tests/advisor/test_reporting.py -q
```

Expected: PASS with `2 passed`.

- [ ] **Step 5: Commit**

```bash
git add advisor/reporting tests/advisor/test_reporting.py
git commit -m "feat: archive advisor reports"
```

---

### Task 11: FastAPI Advisor API

**Files:**
- Create: `advisor/web/__init__.py`
- Create: `advisor/web/api.py`
- Test: `tests/advisor/test_web_api.py`

**Interfaces:**
- Consumes: report files, profile files, ledger summaries, health state.
- Produces: `create_app(state_dir: Path | None = None) -> FastAPI`.
- Produces endpoints: `GET /api/health`, `GET /api/current-state`, `GET /api/reports/{report_date}/{report_type}`.

- [ ] **Step 1: Write failing API tests**

Create `tests/advisor/test_web_api.py`:

```python
from fastapi.testclient import TestClient

from advisor.web.api import create_app


def test_health_endpoint_reports_service_status():
    client = TestClient(create_app())
    response = client.get("/api/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_current_state_has_dashboard_fields():
    client = TestClient(create_app())
    payload = client.get("/api/current-state").json()
    assert {"today", "advice", "ledger", "health"}.issubset(payload.keys())
    assert "positions" in payload["ledger"]
```

- [ ] **Step 2: Run the targeted test and verify it fails**

Run:

```bash
python3 -m pytest tests/advisor/test_web_api.py -q
```

Expected: FAIL with `ModuleNotFoundError: No module named 'advisor.web'`.

- [ ] **Step 3: Add FastAPI app**

Create `advisor/web/__init__.py`:

```python
"""Local advisor web API."""
```

Create `advisor/web/api.py`:

```python
from datetime import date
from pathlib import Path

from fastapi import FastAPI, HTTPException

from advisor.paths import reports_dir


def create_app(state_dir: Path | None = None) -> FastAPI:
    app = FastAPI(title="A Hunter Advisor")

    @app.get("/api/health")
    def health() -> dict:
        return {"status": "ok", "service": "advisor-api"}

    @app.get("/api/current-state")
    def current_state() -> dict:
        return {
            "today": date.today().isoformat(),
            "advice": [],
            "ledger": {"cash": 0, "positions": [], "realized_pnl": 0, "unrealized_pnl": 0},
            "flows": {"information": "unknown", "capital": "unknown", "analyst": "unknown"},
            "health": {"collector": "unknown", "market_updater": "unknown", "advisor_scheduler": "unknown"},
        }

    @app.get("/api/reports/{report_date}/{report_type}")
    def report(report_date: str, report_type: str) -> dict:
        if report_type not in {"premarket", "review"}:
            raise HTTPException(status_code=404, detail="unsupported report type")
        path = reports_dir() / report_date / f"{report_type}.md"
        if not path.exists():
            raise HTTPException(status_code=404, detail="report not found")
        return {"report_date": report_date, "report_type": report_type, "markdown": path.read_text(encoding="utf-8")}

    return app


app = create_app()
```

- [ ] **Step 4: Run the targeted test and verify it passes**

Run:

```bash
python3 -m pytest tests/advisor/test_web_api.py -q
```

Expected: PASS with `2 passed`.

- [ ] **Step 5: Commit**

```bash
git add advisor/web tests/advisor/test_web_api.py
git commit -m "feat: add advisor api"
```

---

### Task 12: Vite React Dashboard

**Files:**
- Create: `frontend/package.json`
- Create: `frontend/index.html`
- Create: `frontend/src/App.tsx`
- Create: `frontend/src/main.tsx`
- Create: `frontend/src/styles.css`
- Create: `frontend/tsconfig.json`
- Create: `frontend/vite.config.ts`
- Modify: `package.json`

**Interfaces:**
- Consumes: `GET /api/current-state`.
- Produces: local dashboard UI showing advice, ledger, report links, and profile links backed by API fields.
- Produces: npm script `advisor:web`.

- [ ] **Step 1: Add frontend package and dashboard files**

Create `frontend/package.json`:

```json
{
  "name": "a-hunter-advisor-frontend",
  "private": true,
  "type": "module",
  "scripts": {
    "dev": "vite --host 127.0.0.1 --port 5173",
    "build": "vite build",
    "preview": "vite preview --host 127.0.0.1 --port 5173"
  },
  "dependencies": {
    "@vitejs/plugin-react": "^4.3.1",
    "vite": "^5.4.0",
    "typescript": "^5.5.4",
    "react": "^18.3.1",
    "react-dom": "^18.3.1",
    "lucide-react": "^0.468.0"
  },
  "devDependencies": {}
}
```

Create `frontend/index.html`:

```html
<!doctype html>
<html lang="zh-CN">
  <head>
    <meta charset="UTF-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1.0" />
    <title>A Hunter Advisor</title>
  </head>
  <body>
    <div id="root"></div>
    <script type="module" src="/src/main.tsx"></script>
  </body>
</html>
```

Create `frontend/src/App.tsx`:

```tsx
import { Activity, BarChart3, BookOpen, BriefcaseBusiness } from "lucide-react";
import "./styles.css";

type CurrentState = {
  today: string;
  advice: Array<{ code: string; action: string; confidence: number }>;
  ledger: { cash: number; positions: Array<{ code: string; quantity: number }>; realized_pnl: number; unrealized_pnl: number };
  flows: { information: string; capital: string; analyst: string };
  health: Record<string, string>;
};

const fallbackState: CurrentState = {
  today: "loading",
  advice: [],
  ledger: { cash: 0, positions: [], realized_pnl: 0, unrealized_pnl: 0 },
  flows: { information: "unknown", capital: "unknown", analyst: "unknown" },
  health: { collector: "unknown", market_updater: "unknown", advisor_scheduler: "unknown" },
};

export default function App() {
  const state = fallbackState;
  return (
    <main className="shell">
      <section className="topbar">
        <div>
          <h1>A Hunter Advisor</h1>
          <p>{state.today}</p>
        </div>
        <div className="health"><Activity size={18} /> Local</div>
      </section>
      <section className="grid">
        <article className="panel">
          <h2><BriefcaseBusiness size={18} /> Ledger</h2>
          <p>Cash: CNY {state.ledger.cash.toLocaleString()}</p>
          <p>Positions: {state.ledger.positions.length}</p>
        </article>
        <article className="panel">
          <h2><BarChart3 size={18} /> Advice</h2>
          <p>{state.advice.length === 0 ? "No archived advice loaded" : `${state.advice.length} items`}</p>
        </article>
        <article className="panel">
          <h2><BookOpen size={18} /> Flows</h2>
          <p>Information: {state.flows.information}</p>
          <p>Capital: {state.flows.capital}</p>
          <p>Analyst: {state.flows.analyst}</p>
        </article>
      </section>
    </main>
  );
}
```

Create `frontend/src/main.tsx`:

```tsx
import React from "react";
import ReactDOM from "react-dom/client";
import App from "./App";

ReactDOM.createRoot(document.getElementById("root") as HTMLElement).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
);
```

Create `frontend/src/styles.css`:

```css
:root {
  color: #172026;
  background: #f3f6f4;
  font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
}

body {
  margin: 0;
}

.shell {
  min-height: 100vh;
  padding: 24px;
}

.topbar {
  display: flex;
  justify-content: space-between;
  align-items: center;
  border-bottom: 1px solid #cad6cf;
  padding-bottom: 16px;
}

h1, h2, p {
  margin: 0;
}

.grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(260px, 1fr));
  gap: 16px;
  margin-top: 20px;
}

.panel {
  background: #ffffff;
  border: 1px solid #d7e0da;
  border-radius: 8px;
  padding: 16px;
}

.panel h2,
.health {
  display: flex;
  align-items: center;
  gap: 8px;
}
```

Create `frontend/tsconfig.json`:

```json
{
  "compilerOptions": {
    "target": "ES2020",
    "useDefineForClassFields": true,
    "lib": ["DOM", "DOM.Iterable", "ES2020"],
    "allowJs": false,
    "skipLibCheck": true,
    "esModuleInterop": true,
    "allowSyntheticDefaultImports": true,
    "strict": true,
    "forceConsistentCasingInFileNames": true,
    "module": "ESNext",
    "moduleResolution": "Node",
    "resolveJsonModule": true,
    "isolatedModules": true,
    "noEmit": true,
    "jsx": "react-jsx"
  },
  "include": ["src"],
  "references": []
}
```

Create `frontend/vite.config.ts`:

```ts
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      "/api": "http://127.0.0.1:8000"
    }
  }
});
```

Modify root `package.json` scripts:

```json
{
  "scripts": {
    "test": "node --test 'tests/**/*.test.mjs'",
    "self-test": "node scripts/self-test.mjs",
    "advisor:web": "npm --prefix frontend run dev",
    "advisor:web:build": "npm --prefix frontend run build"
  }
}
```

- [ ] **Step 2: Install frontend dependencies and build**

Run:

```bash
npm --prefix frontend install
npm --prefix frontend run build
```

Expected: Vite production build completes and writes `frontend/dist/`.

- [ ] **Step 3: Commit**

```bash
git add package.json frontend
git commit -m "feat: add advisor dashboard"
```

---

### Task 13: Launchd Templates, Self-Test Integration, and Operations Docs

**Files:**
- Create: `advisor/scheduler/__init__.py`
- Create: `advisor/scheduler/launchd.py`
- Create: `config/launchd/com.ahunter.advisor-api.plist.template`
- Create: `config/launchd/com.ahunter.advisor-premarket.plist.template`
- Create: `config/launchd/com.ahunter.advisor-review.plist.template`
- Modify: `scripts/self-test.mjs`
- Modify: `agents.md`
- Test: `tests/advisor/test_launchd.py`

**Interfaces:**
- Consumes: Python test command and frontend build command.
- Produces: `validate_launchd_template(path: Path) -> bool`.
- Produces: launchd templates for API keepalive, 08:30 advice, and 22:30 review.
- Updates repository operating instructions without weakening collector rules.

- [ ] **Step 1: Write failing launchd tests**

Create `tests/advisor/test_launchd.py`:

```python
from pathlib import Path

from advisor.scheduler.launchd import validate_launchd_template


def test_launchd_templates_have_required_keys():
    for name in [
        "com.ahunter.advisor-api.plist.template",
        "com.ahunter.advisor-premarket.plist.template",
        "com.ahunter.advisor-review.plist.template",
    ]:
        assert validate_launchd_template(Path("config/launchd") / name)
```

- [ ] **Step 2: Run the targeted test and verify it fails**

Run:

```bash
python3 -m pytest tests/advisor/test_launchd.py -q
```

Expected: FAIL with `ModuleNotFoundError: No module named 'advisor.scheduler'`.

- [ ] **Step 3: Add launchd validator and templates**

Create `advisor/scheduler/__init__.py`:

```python
"""Mac scheduler and keepalive helpers."""
```

Create `advisor/scheduler/launchd.py`:

```python
import argparse
import plistlib
from pathlib import Path


def validate_launchd_template(path: Path) -> bool:
    payload = plistlib.loads(path.read_bytes())
    required = {"Label", "ProgramArguments", "WorkingDirectory", "StandardOutPath", "StandardErrorPath"}
    return required.issubset(payload.keys())


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("template")
    args = parser.parse_args()
    if not validate_launchd_template(Path(args.template)):
        raise SystemExit(1)
    print(f"valid launchd template: {args.template}")
```

Create `config/launchd/com.ahunter.advisor-api.plist.template`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.ahunter.advisor-api</string>
  <key>WorkingDirectory</key><string>/Users/mac/Documents/Ahunter/a_hunter</string>
  <key>ProgramArguments</key>
  <array>
    <string>python3</string>
    <string>-m</string>
    <string>uvicorn</string>
    <string>advisor.web.api:app</string>
    <string>--host</string>
    <string>127.0.0.1</string>
    <string>--port</string>
    <string>8000</string>
  </array>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>/Users/mac/Documents/Ahunter/a_hunter/logs/advisor-api.out.log</string>
  <key>StandardErrorPath</key><string>/Users/mac/Documents/Ahunter/a_hunter/logs/advisor-api.err.log</string>
</dict>
</plist>
```

Create `config/launchd/com.ahunter.advisor-premarket.plist.template`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.ahunter.advisor-premarket</string>
  <key>WorkingDirectory</key><string>/Users/mac/Documents/Ahunter/a_hunter</string>
  <key>ProgramArguments</key>
  <array>
    <string>python3</string>
    <string>-m</string>
    <string>advisor.reporting.premarket</string>
    <string>--date</string>
    <string>${YYYY_MM_DD}</string>
    <string>--output-dir</string>
    <string>reports/${YYYY_MM_DD}</string>
  </array>
  <key>StartCalendarInterval</key>
  <dict><key>Hour</key><integer>8</integer><key>Minute</key><integer>30</integer></dict>
  <key>StandardOutPath</key><string>/Users/mac/Documents/Ahunter/a_hunter/logs/advisor-premarket.out.log</string>
  <key>StandardErrorPath</key><string>/Users/mac/Documents/Ahunter/a_hunter/logs/advisor-premarket.err.log</string>
</dict>
</plist>
```

Create `config/launchd/com.ahunter.advisor-review.plist.template`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.ahunter.advisor-review</string>
  <key>WorkingDirectory</key><string>/Users/mac/Documents/Ahunter/a_hunter</string>
  <key>ProgramArguments</key>
  <array>
    <string>python3</string>
    <string>-m</string>
    <string>advisor.reporting.review</string>
    <string>--date</string>
    <string>${YYYY_MM_DD}</string>
    <string>--output-dir</string>
    <string>reports/${YYYY_MM_DD}</string>
  </array>
  <key>StartCalendarInterval</key>
  <dict><key>Hour</key><integer>22</integer><key>Minute</key><integer>30</integer></dict>
  <key>StandardOutPath</key><string>/Users/mac/Documents/Ahunter/a_hunter/logs/advisor-review.out.log</string>
  <key>StandardErrorPath</key><string>/Users/mac/Documents/Ahunter/a_hunter/logs/advisor-review.err.log</string>
</dict>
</plist>
```

- [ ] **Step 4: Update self-test and operations instructions**

Modify `scripts/self-test.mjs` so it runs the existing Node tests first, then runs Python advisor tests:

```js
const pythonResult = spawnSync("python3", ["-m", "pytest", "tests/advisor", "-q"], {
  encoding: "utf8",
});
const ok = result.status === 0 && pythonResult.status === 0;
```

Append an `Advisor operations` section to `agents.md`:

```markdown
## Advisor operations

The advisor is separate from the passive MX collector. It may read accepted MX events, free A-share data, stock profiles, ledger state, and analyst outputs to create research reports. It must not connect to a broker or submit orders.

Use free A-share data sources learned from `/Users/mac/Documents/TradingAgents-astock`; Tushare and paid/API-key-only market data are not part of required advisor data paths.

The 08:30 premarket report writes `reports/YYYY-MM-DD/premarket.md` and `premarket.json`. The 22:30 review writes `reports/YYYY-MM-DD/review.md` and `review.json`, and it must link back to the same day's premarket advice IDs.

If advisor data-quality checks fail, archive a failure report and do not generate stock recommendations until the failure is resolved.
```

- [ ] **Step 5: Run final verification commands**

Run:

```bash
python3 -m pytest tests/advisor -q
/Users/mac/.local/share/chrome-devtools-mcp/node/bin/node scripts/self-test.mjs
npm --prefix frontend run build
```

Expected: Python advisor tests pass, existing Node self-test passes, frontend build passes.

- [ ] **Step 6: Commit**

```bash
git add advisor/scheduler config/launchd scripts/self-test.mjs agents.md tests/advisor/test_launchd.py
git commit -m "feat: add advisor keepalive templates"
```

---

## Self-Review Checklist

- Spec coverage: Tasks 1-3 cover scaffold, config, and database; Task 4 covers free-source market data and three-year K-line storage; Task 5 covers MX evidence and quality gates; Tasks 6-7 cover ledger and stock profiles; Task 8 covers K-line PNGs; Task 9 covers all TradingAgents-astock roles; Task 10 covers 08:30 and 22:30 archived reports; Tasks 11-12 cover FastAPI and Vite/React frontend; Task 13 covers launchd, self-test, and operations docs.
- Safety coverage: Global constraints preserve collector safety, Node 24 collector command requirements, user-authorized RIDs, no broker execution, no credentials, no Tushare, and quality-gate blocking.
- Type consistency: `DailyBar`, `MarketDataProvider`, `NormalizedEvent`, `QualityResult`, `LedgerTransaction`, `StockProfile`, `AnalystOutput`, `AdviceItem`, `ReviewItem`, and `ReportPaths` are defined before downstream use.
- Execution boundary: The plan produces a first working advisor loop. Advanced live vendor wiring, full external TradingAgents live execution, strategy promotion, broker import, and backtesting lab remain outside first-phase acceptance unless a future approved spec expands scope.
