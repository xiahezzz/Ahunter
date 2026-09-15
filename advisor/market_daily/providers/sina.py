"""Sina-only public daily-bar and forward-adjustment adapter.

The public daily endpoint reports OHLCV but no historical turnover amount.  A
missing amount therefore remains ``None`` all the way into the canonical
contract; this module never estimates or fabricates it.
"""

from __future__ import annotations

import math
import re
import time
from datetime import date, datetime
from typing import Any, Callable, Protocol
from zoneinfo import ZoneInfo

import requests

from advisor.market_daily.adjustments import AdjustmentError, normalize_forward_factors
from advisor.market_daily.contracts import (
    AdjustmentFactor,
    CanonicalDailyBar,
    MarketAbsence,
    MarketContractError,
    exchange_for_code,
)
from advisor.market_daily.providers.contracts import MarketProviderError
from advisor.market_daily.providers.exchanges import ExchangeSecurity, UniverseSourceError
from advisor.sina_rate_limit import PROCESS_SINA_LIMITER, SinaRateLimiter


SINA_DAILY_ENDPOINT = "https://quotes.sina.cn/cn/api/openapi.php/CN_MarketDataService.getKLineData"
SINA_QFQ_ENDPOINT_TEMPLATE = (
    "https://finance.sina.com.cn/realstock/newcompany/{symbol}/pqfq.js"
)
SINA_MARKET_NODE_ENDPOINT = (
    "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
    "Market_Center.getHQNodeData"
)
SINA_MARKET_NODE_COUNT_ENDPOINT = (
    "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
    "Market_Center.getHQNodeStockCount"
)
SINA_FACTOR_ALGORITHM = "sina-qfq-ratio@1"
_MAX_DATALEN = 2_000
_MIN_DATALEN = 10
_SHANGHAI = ZoneInfo("Asia/Shanghai")
_QFQ_ENTRY = re.compile(r'_(\d{4})_(\d{2})_(\d{2}):"([^\"]+)"')


class _HttpSession(Protocol):
    def get(self, url: str, **kwargs: Any) -> Any: ...


class _RateLimiter(Protocol):
    def acquire(self) -> None: ...


class SinaDailyBarProvider:
    """Fetch complete raw daily bars and Sina qfq ratios through one limiter."""

    source = "sina"
    factor_source = "sina_adjustment"
    endpoint = SINA_DAILY_ENDPOINT
    max_concurrency = 1
    allows_empty_daily_bars = True
    _headers = {
        "Accept": "application/json, text/javascript, */*;q=0.8",
        "Referer": "https://finance.sina.com.cn/",
        "User-Agent": "A-Hunter-Market-Daily/1.0",
    }

    def __init__(
        self,
        session: _HttpSession | None = None,
        limiter: _RateLimiter | None = None,
        clock: Callable[[], datetime] | None = None,
        *,
        timeout_seconds: float = 20.0,
        max_attempts: int = 3,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not math.isfinite(float(timeout_seconds))
            or timeout_seconds <= 0
        ):
            raise ValueError("timeout_seconds must be a positive finite number")
        if (
            not isinstance(max_attempts, int)
            or isinstance(max_attempts, bool)
            or not 1 <= max_attempts <= 3
        ):
            raise ValueError("max_attempts must be within 1..3")
        current_session = session if session is not None else requests.Session()
        if session is None and hasattr(current_session, "trust_env"):
            current_session.trust_env = False
        self._session = current_session
        self._limiter = limiter or PROCESS_SINA_LIMITER
        self._clock = clock or (lambda: datetime.now(_SHANGHAI))
        self._sleep = sleep
        self.timeout_seconds = float(timeout_seconds)
        self.max_attempts = max_attempts
        self._cached_key: tuple[str, str, date, date] | None = None
        self._cached_bars: tuple[CanonicalDailyBar, ...] | None = None
        self._cached_fetched_at: datetime | None = None

    def fetch_daily_bars(
        self, code: str, start: date, end: date
    ) -> tuple[CanonicalDailyBar, ...]:
        try:
            exchange = exchange_for_code(code)
        except MarketContractError as error:
            raise MarketProviderError("证券代码不属于沪深 A 股范围") from error
        if code.startswith("689"):
            raise MarketProviderError("CDR 不在 Market Daily 范围内")
        symbol = f"{'sh' if exchange == 'SH' else 'sz'}{code}"
        return self._fetch_symbol(symbol, code, start, end)

    def fetch_index_daily_bars(
        self, index_code: str, market: str, start: date, end: date
    ) -> tuple[CanonicalDailyBar, ...]:
        if not isinstance(index_code, str) or re.fullmatch(r"\d{6}", index_code) is None:
            raise MarketProviderError("新浪基准指数代码无效")
        if market not in {"SH", "SZ"}:
            raise MarketProviderError("新浪基准指数市场无效")
        symbol = f"{'sh' if market == 'SH' else 'sz'}{index_code}"
        return self._fetch_symbol(symbol, index_code, start, end)

    def fetch_adjustment_factors(
        self, code: str, start: date, end: date
    ) -> tuple[AdjustmentFactor, ...]:
        try:
            exchange = exchange_for_code(code)
        except MarketContractError as error:
            raise MarketProviderError("证券代码不属于沪深 A 股范围") from error
        symbol = f"{'sh' if exchange == 'SH' else 'sz'}{code}"
        key = (symbol, code, start, end)
        bars = self._cached_bars if self._cached_key == key else None
        if bars is None:
            bars = self.fetch_daily_bars(code, start, end)
        values = self._fetch_qfq_values(symbol)
        ratios: list[tuple[date, float]] = []
        for bar in bars:
            qfq_value = values.get(bar.trade_date)
            if qfq_value is None:
                raise MarketProviderError("新浪前复权没有完整覆盖原始日线")
            ratio = qfq_value / bar.close
            if not math.isfinite(ratio) or ratio <= 0:
                raise MarketProviderError("新浪前复权比率无效")
            ratios.append((bar.trade_date, ratio))
        fetched_at = self._now()
        try:
            return normalize_forward_factors(
                code,
                tuple(ratios),
                source=self.factor_source,
                source_at=fetched_at,
                fetched_at=fetched_at,
                algorithm_version=SINA_FACTOR_ALGORITHM,
            )
        except AdjustmentError as error:
            raise MarketProviderError("新浪前复权因子规范化失败") from error

    def fetch_market_node(self, node: str) -> tuple[dict[str, object], ...]:
        """Return one complete current Shanghai or Shenzhen A-share node."""

        if node not in {"sh_a", "sz_a"}:
            raise MarketProviderError("新浪股票池节点无效")
        rows: list[dict[str, object]] = []
        page_size = 100
        count = self.fetch_market_node_count(node)
        total_pages = math.ceil(count / page_size)
        for page in range(1, total_pages + 1):
            response = self._request(
                SINA_MARKET_NODE_ENDPOINT,
                params={
                    "page": str(page),
                    "num": str(page_size),
                    "sort": "symbol",
                    "asc": "1",
                    "node": node,
                    "symbol": "",
                    "_s_r_a": "page",
                },
                purpose="新浪股票池",
            )
            if not _content_type(response).startswith("application/json"):
                raise MarketProviderError("新浪股票池返回了非 JSON 内容")
            try:
                payload = response.json()
            except (TypeError, ValueError, AttributeError) as error:
                raise MarketProviderError("新浪股票池 JSON 无法解析") from error
            if not isinstance(payload, list) or any(not isinstance(row, dict) for row in payload):
                raise MarketProviderError("新浪股票池响应行无效")
            rows.extend(payload)
        if len(rows) != count:
            raise MarketProviderError("新浪股票池分页数量不完整")
        return tuple(rows)

    def fetch_market_node_count(self, node: str) -> int:
        if node not in {"sh_a", "sz_a"}:
            raise MarketProviderError("新浪股票池节点无效")
        response = self._request(
            SINA_MARKET_NODE_COUNT_ENDPOINT,
            params={"node": node},
            purpose="新浪股票池计数",
        )
        if not _content_type(response).startswith("application/json"):
            raise MarketProviderError("新浪股票池计数返回了非 JSON 内容")
        try:
            raw = response.json()
            count = int(raw)
        except (TypeError, ValueError, OverflowError, AttributeError) as error:
            raise MarketProviderError("新浪股票池计数无效") from error
        if isinstance(raw, bool) or count <= 0 or count > 10_000:
            raise MarketProviderError("新浪股票池计数超出安全范围")
        return count

    def fetch_listing_date(self, symbol: str) -> date:
        values = self._fetch_qfq_values(symbol)
        return min(values)

    def absences_for(
        self, code: str, sessions: tuple[date, ...], now: datetime
    ) -> tuple[MarketAbsence, ...]:
        """Treat missing rows in one complete Sina response as suspension evidence."""

        if not sessions:
            return ()
        if (
            self._cached_key is None
            or self._cached_key[1] != code
            or self._cached_fetched_at is None
            or any(not self._cached_key[2] <= item <= self._cached_key[3] for item in sessions)
        ):
            return ()
        if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
            raise MarketProviderError("新浪停牌观察时间必须带时区")
        observed = {bar.trade_date for bar in self._cached_bars or ()}
        return tuple(
            MarketAbsence(
                code=code,
                trade_date=trade_date,
                reason="suspended",
                source="sina_no_daily_bar",
                source_at=self._cached_fetched_at,
                fetched_at=self._cached_fetched_at,
            )
            for trade_date in sessions
            if trade_date not in observed
        )

    def _fetch_symbol(
        self, symbol: str, code: str, start: date, end: date
    ) -> tuple[CanonicalDailyBar, ...]:
        fetched_at = self._now()
        _validate_window(start, end, fetched_at)
        params = {
            "symbol": symbol,
            "scale": "240",
            "ma": "no",
            "datalen": str(_datalen(start, end)),
        }
        response = self._request(self.endpoint, params=params, purpose="新浪日线")
        content_type = _content_type(response)
        if not content_type.startswith("application/json"):
            raise MarketProviderError("新浪日线返回了非 JSON 内容")
        try:
            payload = response.json()
        except (TypeError, ValueError, AttributeError) as error:
            raise MarketProviderError("新浪日线 JSON 无法解析") from error
        payload = _openapi_daily_rows(payload)
        bars = parse_sina_daily_bars(
            payload,
            code=code,
            start=start,
            end=end,
            fetched_at=fetched_at,
            source=self.source,
            allow_empty=True,
        )
        self._cached_key = (symbol, code, start, end)
        self._cached_bars = bars
        self._cached_fetched_at = fetched_at
        return bars

    def _fetch_qfq_values(self, symbol: str) -> dict[date, float]:
        response = self._request(
            SINA_QFQ_ENDPOINT_TEMPLATE.format(symbol=symbol),
            params=None,
            purpose="新浪前复权",
        )
        content_type = _content_type(response)
        if content_type and not any(
            marker in content_type for marker in ("javascript", "text/plain", "text/html")
        ):
            raise MarketProviderError("新浪前复权返回了非脚本内容")
        return parse_sina_qfq_values(getattr(response, "text", None), symbol=symbol)

    def _request(self, url: str, *, params: dict[str, str] | None, purpose: str) -> Any:
        last_error: BaseException | None = None
        for attempt in range(1, self.max_attempts + 1):
            self._limiter.acquire()
            try:
                response = self._session.get(
                    url,
                    params=params,
                    headers=self._headers,
                    timeout=self.timeout_seconds,
                )
            except (requests.RequestException, OSError, AttributeError) as error:
                last_error = error
                if attempt < self.max_attempts:
                    self._sleep(_retry_delay(attempt, None))
                    continue
                break
            status = getattr(response, "status_code", None)
            if status == 429 or isinstance(status, int) and 500 <= status <= 599:
                last_error = MarketProviderError(f"{purpose}暂时不可用（HTTP {status}）")
                if attempt < self.max_attempts:
                    self._sleep(_retry_delay(attempt, response))
                    continue
                break
            try:
                response.raise_for_status()
            except (requests.RequestException, OSError, AttributeError) as error:
                raise MarketProviderError(f"{purpose}请求被拒绝") from error
            return response
        raise MarketProviderError(f"{purpose}在有界重试后仍不可用") from last_error

    def _now(self) -> datetime:
        value = self._clock()
        if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
            raise MarketProviderError("新浪抓取时钟必须带时区")
        return value


def _openapi_daily_rows(payload: object) -> object:
    result = payload.get("result") if isinstance(payload, dict) else None
    status = result.get("status") if isinstance(result, dict) else None
    rows = result.get("data") if isinstance(result, dict) else None
    code = status.get("code") if isinstance(status, dict) else None
    if code not in {0, "0"} or not isinstance(rows, list):
        raise MarketProviderError("新浪日线响应状态或数据无效")
    return rows


def parse_sina_daily_bars(
    payload: object,
    *,
    code: str,
    start: date,
    end: date,
    fetched_at: datetime,
    source: str = "sina",
    allow_empty: bool = False,
) -> tuple[CanonicalDailyBar, ...]:
    """Parse one response atomically and retain only the requested closed interval."""

    _validate_window(start, end, fetched_at)
    if not isinstance(payload, list):
        raise MarketProviderError("新浪没有返回日线集合")
    if not payload:
        if allow_empty:
            return ()
        raise MarketProviderError("新浪没有返回日线集合")
    parsed: list[CanonicalDailyBar] = []
    previous: date | None = None
    allowed_fields = {"day", "open", "high", "low", "close", "volume"}
    for row in payload:
        if not isinstance(row, dict) or set(row) != allowed_fields:
            raise MarketProviderError("新浪日线字段集合不符合公开接口契约")
        try:
            trade_date = date.fromisoformat(str(row["day"]))
        except (TypeError, ValueError) as error:
            raise MarketProviderError("新浪日线日期无效") from error
        if previous is not None and trade_date <= previous:
            raise MarketProviderError("新浪日线日期必须严格升序且唯一")
        previous = trade_date
        if trade_date > fetched_at.astimezone(_SHANGHAI).date():
            raise MarketProviderError("新浪日线包含未来日期")
        if _is_suspension_placeholder(row):
            # Sina normally omits suspended sessions, but for a small number
            # of securities it emits a zero-volume row whose open/high/low
            # are zero and whose close carries the previous valid close.
            # Treat that provider-specific shape exactly like an omitted row;
            # ``absences_for`` will turn the missing requested session into
            # explicit, auditable suspension evidence.
            continue
        open_price = _positive_number(row["open"], "开盘价")
        high = _positive_number(row["high"], "最高价")
        low = _positive_number(row["low"], "最低价")
        close = _positive_number(row["close"], "收盘价")
        volume = _share_volume(row["volume"])
        if not start <= trade_date <= end:
            continue
        try:
            parsed.append(
                CanonicalDailyBar(
                    code=code,
                    trade_date=trade_date,
                    open=open_price,
                    high=high,
                    low=low,
                    close=close,
                    volume=volume,
                    amount=None,
                    source=source,
                    source_at=fetched_at,
                    fetched_at=fetched_at,
                    as_of_date=trade_date,
                )
            )
        except MarketContractError as error:
            raise MarketProviderError("新浪日线不满足规范契约") from error
    if not parsed and not allow_empty:
        raise MarketProviderError("新浪没有返回请求区间日线")
    return tuple(parsed)


def _is_suspension_placeholder(row: dict[str, object]) -> bool:
    """Recognize only Sina's evidenced zero-volume suspension placeholder."""

    raw_prices = tuple(row.get(field) for field in ("open", "high", "low"))
    if any(isinstance(value, bool) for value in (*raw_prices, row.get("close"), row.get("volume"))):
        return False
    try:
        prices = tuple(float(value) for value in raw_prices)
        close = _positive_number(row["close"], "收盘价")
        volume = _share_volume(row["volume"])
    except (TypeError, ValueError, OverflowError, KeyError, MarketProviderError):
        return False
    return (
        all(math.isfinite(value) for value in prices)
        and prices[0] == prices[1] == prices[2] == 0
        and volume == 0
    )


class SinaUniverseAdapter:
    """Current Shanghai-Shenzhen A-share universe, with Sina-derived listing dates."""

    source = "sina_universe"

    def __init__(
        self,
        provider: object,
        *,
        known_listing_dates: Callable[[], dict[str, date]] | None = None,
        clock: Callable[[], datetime] | None = None,
        progress: Callable[[], None] | None = None,
    ) -> None:
        self._provider = provider
        self._known_listing_dates = known_listing_dates or (lambda: {})
        self._clock = clock or (lambda: datetime.now(_SHANGHAI))
        self._progress = progress

    def probe(self) -> dict[str, int]:
        try:
            sh = self._provider.fetch_market_node_count("sh_a")
            self._tick()
            sz = self._provider.fetch_market_node_count("sz_a")
            self._tick()
        except Exception as error:
            raise UniverseSourceError("新浪股票池计数请求失败") from error
        if not isinstance(sh, int) or not isinstance(sz, int) or sh <= 0 or sz <= 0:
            raise UniverseSourceError("新浪股票池计数无效")
        return {"SH": sh, "SZ": sz}

    def fetch(self) -> tuple[ExchangeSecurity, ...]:
        rows = (*self._node("sh_a"), *self._node("sz_a"))
        try:
            known = self._known_listing_dates()
        except Exception as error:
            raise UniverseSourceError("无法读取本地新浪上市日期") from error
        if not isinstance(known, dict) or any(
            not isinstance(code, str)
            or not isinstance(listed, date)
            or isinstance(listed, datetime)
            for code, listed in known.items()
        ):
            raise UniverseSourceError("本地新浪上市日期无效")
        fetched_at = self._clock()
        if not isinstance(fetched_at, datetime) or fetched_at.tzinfo is None or fetched_at.utcoffset() is None:
            raise UniverseSourceError("新浪股票池抓取时间必须带时区")
        records: list[ExchangeSecurity] = []
        seen: set[str] = set()
        for row in sorted(rows, key=lambda item: str(item.get("code", ""))):
            normalized = _sina_universe_identity(row)
            if normalized is None:
                continue
            symbol, code, name, exchange = normalized
            if code in seen:
                raise UniverseSourceError(f"新浪股票池包含重复代码 {code}")
            seen.add(code)
            listed = known.get(code)
            if listed is None:
                try:
                    listed = self._provider.fetch_listing_date(symbol)
                except Exception as error:
                    raise UniverseSourceError(f"新浪未能确认 {code} 的上市日期") from error
                self._tick()
            if listed > fetched_at.astimezone(_SHANGHAI).date():
                raise UniverseSourceError(f"新浪返回了未来上市日期 {code}")
            records.append(
                ExchangeSecurity(
                    code=code,
                    name=name,
                    exchange=exchange,
                    list_date=listed,
                    delist_date=None,
                    status="active",
                    source=self.source,
                    source_at=fetched_at,
                    fetched_at=fetched_at,
                    is_st=bool(re.search(r"(?:^|[ *])ST", name.upper())),
                )
            )
        if not records:
            raise UniverseSourceError("新浪没有返回沪深普通 A 股")
        return tuple(records)

    def _node(self, node: str) -> tuple[dict[str, object], ...]:
        try:
            rows = self._provider.fetch_market_node(node)
        except Exception as error:
            raise UniverseSourceError("新浪股票池请求失败") from error
        self._tick()
        if not isinstance(rows, tuple) or any(not isinstance(row, dict) for row in rows):
            raise UniverseSourceError("新浪股票池响应无效")
        return rows

    def _tick(self) -> None:
        if self._progress is not None:
            self._progress()


def _sina_universe_identity(
    row: dict[str, object],
) -> tuple[str, str, str, str] | None:
    symbol = row.get("symbol")
    code = row.get("code")
    name = row.get("name")
    if not isinstance(symbol, str) or not isinstance(code, str) or not isinstance(name, str):
        raise UniverseSourceError("新浪股票池身份字段无效")
    symbol = symbol.strip().lower()
    code = code.strip()
    name = name.strip()
    if not name:
        raise UniverseSourceError("新浪股票池证券简称为空")
    if re.fullmatch(r"(?:sh|sz)\d{6}", symbol) is None or symbol[2:] != code:
        return None
    if re.fullmatch(r"[036]\d{5}", code) is None or code.startswith("689"):
        return None
    exchange = "SH" if symbol.startswith("sh") else "SZ"
    expected = "SH" if code.startswith("6") else "SZ"
    return (symbol, code, name, exchange) if exchange == expected else None


def parse_sina_qfq_values(payload: object, *, symbol: str) -> dict[date, float]:
    if not isinstance(payload, str) or not payload.strip():
        raise MarketProviderError("新浪前复权响应为空")
    if re.fullmatch(r"(?:sh|sz)\d{6}", symbol) is None:
        raise MarketProviderError("新浪前复权证券标识无效")
    pattern = re.compile(
        rf"\bvar\s+{re.escape(symbol)}qfq=\[\{{total:(\d+),data:\{{(.*?)\}}\}}\]",
        re.DOTALL,
    )
    matched = pattern.search(payload)
    if matched is None:
        raise MarketProviderError("新浪前复权变量与请求证券不一致")
    expected_count = int(matched.group(1))
    values: dict[date, float] = {}
    for year, month, day, raw_value in _QFQ_ENTRY.findall(matched.group(2)):
        try:
            trade_date = date(int(year), int(month), int(day))
            value = float(raw_value)
        except (TypeError, ValueError, OverflowError) as error:
            raise MarketProviderError("新浪前复权字段无效") from error
        if trade_date in values or not math.isfinite(value) or value <= 0:
            raise MarketProviderError("新浪前复权日期重复或数值无效")
        values[trade_date] = value
    if len(values) != expected_count or not values:
        raise MarketProviderError("新浪前复权响应数量不完整")
    return dict(sorted(values.items()))


def _validate_window(start: date, end: date, fetched_at: datetime) -> None:
    if not isinstance(start, date) or isinstance(start, datetime):
        raise MarketProviderError("新浪日线开始日期无效")
    if not isinstance(end, date) or isinstance(end, datetime) or end < start:
        raise MarketProviderError("新浪日线结束日期无效")
    if not isinstance(fetched_at, datetime) or fetched_at.tzinfo is None or fetched_at.utcoffset() is None:
        raise MarketProviderError("新浪抓取时间必须带时区")
    if end > fetched_at.astimezone(_SHANGHAI).date():
        raise MarketProviderError("不能请求未来日期的新浪日线")
    if (end - start).days + _MIN_DATALEN > _MAX_DATALEN:
        raise MarketProviderError("新浪公开接口单次请求不能完整覆盖该日期范围")


def _datalen(start: date, end: date) -> int:
    return min(_MAX_DATALEN, max(_MIN_DATALEN, (end - start).days + 1))


def _positive_number(value: object, field_name: str) -> float:
    if isinstance(value, bool):
        raise MarketProviderError(f"新浪{field_name}无效")
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError) as error:
        raise MarketProviderError(f"新浪{field_name}无效") from error
    if not math.isfinite(result) or result <= 0:
        raise MarketProviderError(f"新浪{field_name}无效")
    return result


def _share_volume(value: object) -> int:
    if isinstance(value, bool):
        raise MarketProviderError("新浪成交量无效")
    text = str(value).strip()
    if re.fullmatch(r"\d+", text) is None:
        raise MarketProviderError("新浪成交量必须是整数股")
    return int(text)


def _content_type(response: object) -> str:
    headers = getattr(response, "headers", {})
    if not hasattr(headers, "get"):
        return ""
    value = headers.get("Content-Type", "")
    return value.lower() if isinstance(value, str) else ""


def _retry_delay(attempt: int, response: object | None) -> float:
    if response is not None and getattr(response, "status_code", None) == 429:
        headers = getattr(response, "headers", {})
        raw = headers.get("Retry-After") if hasattr(headers, "get") else None
        try:
            parsed = float(raw)
        except (TypeError, ValueError, OverflowError):
            parsed = 0.0
        if math.isfinite(parsed) and parsed > 0:
            return min(parsed, 30.0)
    return min(float(2 ** (attempt - 1)), 8.0)
