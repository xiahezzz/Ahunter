from __future__ import annotations

import argparse
import json
import re
import time
from dataclasses import asdict, dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Callable, Sequence

from advisor.config import load_advisor_config, resolve_state_db
from advisor.data_sources.contracts import DailyBar, MarketDataProvider, MarketSourceError
from advisor.data_sources.free_sources import ConfiguredProviderRegistry
from advisor.db.migrate import migrate_database
from advisor.db.repository import connect, record_market_source_attempt, upsert_daily_bar


_CODE_RE = re.compile(r"[03468]\d{5}\Z")


@dataclass(frozen=True)
class BackfillResult:
    requested_codes: tuple[str, ...]
    completed_codes: tuple[str, ...]
    failed_codes: tuple[str, ...]
    inserted_rows: int


def update_market_database(
    db_path: Path,
    provider: MarketDataProvider,
    codes: Sequence[str],
    start: date,
    end: date,
    *,
    sleep: Callable[[float], None] = time.sleep,
) -> BackfillResult:
    requested = _validate_backfill_request(codes, start, end)
    migrate_database(db_path)
    completed: list[str] = []
    failed: list[str] = []
    inserted = 0
    rate = getattr(provider, "rate_limit_per_second", 1.0)

    for index, code in enumerate(requested):
        attempted_at = datetime.now().astimezone().isoformat()
        try:
            bars = provider.fetch_daily_bars(code, start, end)
            _validate_bars(code, bars, start, end)
        except MarketSourceError as error:
            _record_failed_attempt(db_path, provider, code, start, end, attempted_at, error)
            failed.append(code)
        else:
            inserted += _commit_code(db_path, provider, code, start, end, bars, attempted_at)
            completed.append(code)
        if index + 1 < len(requested):
            sleep(1.0 / rate)
    return BackfillResult(requested, tuple(completed), tuple(failed), inserted)


def backfill_daily_bars(
    db_path: Path,
    provider: MarketDataProvider,
    codes: list[str],
    start: date,
    end: date,
) -> int:
    return update_market_database(db_path, provider, codes, start, end).inserted_rows


def _validate_backfill_request(codes: Sequence[str], start: date, end: date) -> tuple[str, ...]:
    if isinstance(codes, (str, bytes)):
        raise ValueError("codes must be an explicit sequence")
    requested = tuple(codes)
    if not requested or len(requested) > 200:
        raise ValueError("between 1 and 200 explicit codes are required")
    if len(set(requested)) != len(requested):
        raise ValueError("codes must be unique")
    if any(not isinstance(code, str) or not _CODE_RE.fullmatch(code) for code in requested):
        raise ValueError("codes must be supported six-digit A-share codes")
    if not isinstance(start, date) or not isinstance(end, date) or start > end:
        raise ValueError("invalid backfill date range")
    return requested


def _validate_bars(code: str, bars: list[DailyBar], start: date, end: date) -> None:
    if not isinstance(bars, list) or not bars:
        raise MarketSourceError("provider returned no daily bars")
    dates: list[date] = []
    for bar in bars:
        if not isinstance(bar, DailyBar) or bar.code != code:
            raise MarketSourceError("provider returned a mismatched daily bar")
        if not start <= bar.trade_date <= end or bar.as_of_date > end:
            raise MarketSourceError("provider returned a daily bar outside the requested interval")
        dates.append(bar.trade_date)
    if dates != sorted(dates) or len(dates) != len(set(dates)):
        raise MarketSourceError("provider returned duplicate or descending daily bars")


def _commit_code(
    db_path: Path,
    provider: MarketDataProvider,
    code: str,
    start: date,
    end: date,
    bars: list[DailyBar],
    attempted_at: str,
) -> int:
    connection = connect(db_path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        changed = sum(upsert_daily_bar(connection, bar) for bar in bars)
        record_market_source_attempt(
            connection, provider, code, start, end, status="passed", fetched_at=attempted_at
        )
        connection.commit()
        return changed
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def _record_failed_attempt(
    db_path: Path,
    provider: MarketDataProvider,
    code: str,
    start: date,
    end: date,
    attempted_at: str,
    error: MarketSourceError,
) -> None:
    connection = connect(db_path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        record_market_source_attempt(
            connection,
            provider,
            code,
            start,
            end,
            status="failed",
            fetched_at=attempted_at,
            error=str(error),
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--codes", required=True)
    parser.add_argument("--start", required=True, type=date.fromisoformat)
    parser.add_argument("--end", required=True, type=date.fromisoformat)
    args = parser.parse_args(argv)
    config_path = args.config.resolve()
    root = config_path.parent.parent if config_path.parent.name == "config" else config_path.parent
    try:
        config = load_advisor_config(config_path)
        db_path = resolve_state_db(config, root)
        registry = ConfiguredProviderRegistry.from_yaml(config_path.with_name("data-sources.yaml"))
        result = update_market_database(
            db_path,
            registry.historical_provider,
            tuple(args.codes.split(",")),
            args.start,
            args.end,
            sleep=time.sleep,
        )
    except (OSError, ValueError, MarketSourceError) as error:
        parser.error(str(error))
    print(json.dumps(asdict(result), ensure_ascii=True))
    raise SystemExit(1 if result.failed_codes else 0)
