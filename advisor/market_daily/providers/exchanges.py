"""Official SSE/SZSE universe adapters with separately testable parsers."""

from __future__ import annotations

import html
import math
import re
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Callable, Iterable, Protocol
from zoneinfo import ZoneInfo

import requests

from advisor.market_daily.contracts import MarketContractError, MarketSecurity, exchange_for_code
from advisor.market_daily.providers._http import pinned_https_session as _pinned_https_session


_SHANGHAI = ZoneInfo("Asia/Shanghai")
_CODE_RE = re.compile(r"[036]\d{5}\Z")
_TAG_RE = re.compile(r"<[^>]*>")


class UniverseSourceError(RuntimeError):
    """An exchange payload cannot safely define the eligible stock universe."""


class _HttpSession(Protocol):
    def get(self, url: str, **kwargs: Any) -> Any: ...


def _now() -> datetime:
    return datetime.now(tz=_SHANGHAI)


def _text(value: object, field_name: str) -> str:
    if not isinstance(value, str):
        raise UniverseSourceError(f"交易所返回的 {field_name} 不是文本")
    normalized = html.unescape(_TAG_RE.sub("", value)).replace("\u00a0", " ").strip()
    normalized = " ".join(normalized.split())
    if not normalized or normalized == "-":
        raise UniverseSourceError(f"交易所返回的 {field_name} 缺失")
    return normalized


def _code(value: object, exchange: str) -> str | None:
    if not isinstance(value, str):
        return None
    code = value.strip()
    if not _CODE_RE.fullmatch(code) or code.startswith("689"):
        return None
    try:
        return code if exchange_for_code(code) == exchange else None
    except MarketContractError:
        return None


def _date(value: object, field_name: str) -> date:
    if not isinstance(value, str):
        raise UniverseSourceError(f"交易所返回的 {field_name} 缺失")
    value = value.strip()
    for pattern in ("%Y-%m-%d", "%Y%m%d"):
        try:
            return datetime.strptime(value, pattern).date()
        except ValueError:
            continue
    raise UniverseSourceError(f"交易所返回的 {field_name} 无效")


def _source_at(value: object, fetched_at: datetime) -> datetime:
    if value is None or value == "" or value == "-":
        return fetched_at
    if not isinstance(value, str):
        raise UniverseSourceError("交易所返回的来源时间无效")
    normalized = value.strip().replace("/", "-")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        try:
            parsed = datetime.combine(_date(normalized, "来源日期"), datetime.min.time())
        except UniverseSourceError as error:
            raise UniverseSourceError("交易所返回的来源时间无效") from error
    return parsed.replace(tzinfo=_SHANGHAI) if parsed.tzinfo is None else parsed


def _is_st(name: str) -> bool:
    return bool(re.search(r"(?:^|[ *])ST", name.upper()))


@dataclass(frozen=True)
class ExchangeSecurity:
    """A normalized common A-share record from an official exchange source."""

    code: str
    name: str
    exchange: str
    list_date: date
    delist_date: date | None
    status: str
    source: str
    source_at: datetime
    fetched_at: datetime
    is_st: bool

    def __post_init__(self) -> None:
        try:
            expected_exchange = exchange_for_code(self.code)
        except MarketContractError as error:
            raise UniverseSourceError("证券代码不属于沪深普通 A 股范围") from error
        if self.code.startswith("689") or self.exchange != expected_exchange:
            raise UniverseSourceError("证券代码与交易所不匹配或属于 CDR")
        if not isinstance(self.list_date, date) or isinstance(self.list_date, datetime):
            raise UniverseSourceError("上市日期无效")
        if self.delist_date is not None:
            if not isinstance(self.delist_date, date) or isinstance(self.delist_date, datetime):
                raise UniverseSourceError("退市日期无效")
            if self.delist_date < self.list_date:
                raise UniverseSourceError("退市日期早于上市日期")
        if self.status not in {"active", "suspended", "delisted"}:
            raise UniverseSourceError("证券状态无效")
        if self.status == "delisted" and self.delist_date is None:
            raise UniverseSourceError("退市证券缺少退市日期")
        if not self.name or not self.source:
            raise UniverseSourceError("证券名称或来源缺失")
        for value in (self.source_at, self.fetched_at):
            if value.tzinfo is None or value.utcoffset() is None:
                raise UniverseSourceError("来源时间必须带时区")

    def to_market_security(self) -> MarketSecurity:
        return MarketSecurity(
            code=self.code,
            name=self.name,
            exchange=self.exchange,
            list_date=self.list_date,
            delist_date=self.delist_date,
            status=self.status,
            is_st=self.is_st,
            source=self.source,
            source_at=self.source_at,
            fetched_at=self.fetched_at,
        )


def parse_sse_current_page(payload: object, fetched_at: datetime) -> tuple[ExchangeSecurity, ...]:
    rows = _sse_rows(payload)
    records: list[ExchangeSecurity] = []
    for row in rows:
        code = _code(row.get("SECURITY_CODE_A"), "SH")
        if code is None:
            continue
        name = _text(row.get("SECURITY_ABBR_A") or row.get("COMPANY_ABBR"), "证券简称")
        records.append(
            ExchangeSecurity(
                code=code,
                name=name,
                exchange="SH",
                list_date=_date(row.get("LISTING_DATE"), "上市日期"),
                delist_date=None,
                status="active",
                source="sse",
                source_at=_source_at(row.get("QIANYI_DATE"), fetched_at),
                fetched_at=fetched_at,
                is_st=_is_st(name),
            )
        )
    return tuple(records)


def parse_sse_suspended_page(payload: object, fetched_at: datetime) -> tuple[ExchangeSecurity, ...]:
    return _parse_sse_status_page(payload, fetched_at, status="suspended")


def parse_sse_delisted_page(payload: object, fetched_at: datetime) -> tuple[ExchangeSecurity, ...]:
    return _parse_sse_status_page(payload, fetched_at, status="delisted")


def _parse_sse_status_page(
    payload: object, fetched_at: datetime, *, status: str
) -> tuple[ExchangeSecurity, ...]:
    records: list[ExchangeSecurity] = []
    for row in _sse_rows(payload):
        code = _code(row.get("A_STOCK_CODE") or row.get("COMPANY_CODE"), "SH")
        if code is None:
            continue
        name = _text(row.get("COMPANY_ABBR"), "证券简称")
        records.append(
            ExchangeSecurity(
                code=code,
                name=name,
                exchange="SH",
                list_date=_date(row.get("LIST_DATE"), "上市日期"),
                delist_date=_date(row.get("DELIST_DATE"), "退市日期") if status == "delisted" else None,
                status=status,
                source="sse",
                source_at=fetched_at,
                fetched_at=fetched_at,
                is_st=_is_st(name),
            )
        )
    return tuple(records)


def parse_szse_current_response(payload: object, fetched_at: datetime) -> tuple[ExchangeSecurity, ...]:
    rows = _szse_rows(payload, "tab1")
    records: list[ExchangeSecurity] = []
    for row in rows:
        code = _code(row.get("agdm"), "SZ")
        if code is None:
            continue
        name = _text(row.get("agjc"), "证券简称")
        records.append(
            ExchangeSecurity(
                code=code,
                name=name,
                exchange="SZ",
                list_date=_date(row.get("agssrq"), "上市日期"),
                delist_date=None,
                status="active",
                source="szse",
                source_at=fetched_at,
                fetched_at=fetched_at,
                is_st=_is_st(name),
            )
        )
    return tuple(records)


def parse_szse_suspended_response(payload: object, fetched_at: datetime) -> tuple[ExchangeSecurity, ...]:
    return _parse_szse_status_response(payload, fetched_at, tabkey="tab1", status="suspended")


def parse_szse_delisted_response(payload: object, fetched_at: datetime) -> tuple[ExchangeSecurity, ...]:
    return _parse_szse_status_response(payload, fetched_at, tabkey="tab2", status="delisted")


def _parse_szse_status_response(
    payload: object, fetched_at: datetime, *, tabkey: str, status: str
) -> tuple[ExchangeSecurity, ...]:
    records: list[ExchangeSecurity] = []
    for row in _szse_rows(payload, tabkey):
        code = _code(row.get("zqdm"), "SZ")
        if code is None:
            continue
        name = _text(row.get("zqjc"), "证券简称")
        records.append(
            ExchangeSecurity(
                code=code,
                name=name,
                exchange="SZ",
                list_date=_date(row.get("ssrq"), "上市日期"),
                delist_date=_date(row.get("zzrq"), "终止上市日期") if status == "delisted" else None,
                status=status,
                source="szse",
                source_at=fetched_at,
                fetched_at=fetched_at,
                is_st=_is_st(name),
            )
        )
    return tuple(records)


def _sse_rows(payload: object) -> tuple[dict[str, object], ...]:
    if not isinstance(payload, dict) or not isinstance(payload.get("pageHelp"), dict):
        raise UniverseSourceError("上交所响应缺少分页数据")
    rows = payload["pageHelp"].get("data")
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise UniverseSourceError("上交所响应行集合无效")
    return tuple(rows)


def _szse_rows(payload: object, tabkey: str) -> tuple[dict[str, object], ...]:
    if not isinstance(payload, list):
        raise UniverseSourceError("深交所响应不是报表列表")
    report = next(
        (
            entry
            for entry in payload
            if isinstance(entry, dict)
            and isinstance(entry.get("metadata"), dict)
            and entry["metadata"].get("tabkey") == tabkey
        ),
        None,
    )
    if report is None:
        raise UniverseSourceError("深交所响应缺少目标报表页")
    rows = report.get("data")
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise UniverseSourceError("深交所响应行集合无效")
    return tuple(rows)


def _page_count(total: object, page_size: object, source_name: str) -> int:
    if isinstance(total, bool) or not isinstance(total, int) or total < 0:
        raise UniverseSourceError(f"{source_name} 分页总数无效")
    if isinstance(page_size, bool) or not isinstance(page_size, int) or page_size <= 0:
        raise UniverseSourceError(f"{source_name} 分页大小无效")
    return max(1, math.ceil(total / page_size))


class SSEUniverseAdapter:
    """Fetch current, suspended and delisted ordinary A shares from SSE."""

    source = "sse"
    current_endpoint = "https://query.sse.com.cn/security/stock/getStockListData2.do"
    status_endpoint = "https://query.sse.com.cn/commonQuery.do"
    _headers = {
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Referer": "https://www.sse.com.cn/assortment/stock/list/",
        "User-Agent": "A-Hunter-Market-Daily/1.0",
    }

    def __init__(
        self,
        session: _HttpSession | None = None,
        clock: Callable[[], datetime] | None = None,
        *,
        page_size: int = 500,
        timeout_seconds: float = 20,
    ) -> None:
        if not isinstance(page_size, int) or page_size <= 0:
            raise ValueError("page_size must be positive")
        self._session = session if session is not None else requests.Session()
        self._clock = clock or _now
        self._page_size = page_size
        self._timeout_seconds = timeout_seconds

    def fetch(self) -> tuple[ExchangeSecurity, ...]:
        records: list[ExchangeSecurity] = []
        for stock_type in ("1", "8"):
            records.extend(self._fetch_current(stock_type))
            records.extend(self._fetch_status(stock_type, company_status="5", parser=parse_sse_suspended_page))
            records.extend(self._fetch_status(stock_type, company_status="3", parser=parse_sse_delisted_page))
        return tuple(records)

    def _fetch_current(self, stock_type: str) -> tuple[ExchangeSecurity, ...]:
        base = {
            "isPagination": "true",
            "stockCode": "",
            "csrcCode": "",
            "areaName": "",
            "stockType": stock_type,
        }
        return self._fetch_sse_pages(self.current_endpoint, base, parse_sse_current_page)

    def _fetch_status(
        self,
        stock_type: str,
        *,
        company_status: str,
        parser: Callable[[object, datetime], tuple[ExchangeSecurity, ...]],
    ) -> tuple[ExchangeSecurity, ...]:
        base = {
            "sqlId": "COMMON_SSE_CP_GPJCTPZ_GPLB_ZZGP_L" if company_status == "3" else "COMMON_SSE_CP_GPJCTPZ_GPLB_ZTGP_L",
            "type": "inParams",
            "CSRC_CODE": "",
            "STOCK_CODE": "",
            "REG_PROVINCE": "",
            "STOCK_TYPE": stock_type,
            "COMPANY_STATUS": company_status,
            "isPagination": "true",
        }
        return self._fetch_sse_pages(self.status_endpoint, base, parser)

    def _fetch_sse_pages(
        self,
        endpoint: str,
        base: dict[str, str],
        parser: Callable[[object, datetime], tuple[ExchangeSecurity, ...]],
    ) -> tuple[ExchangeSecurity, ...]:
        first = self._sse_response(endpoint, {**base, **self._sse_page_params(1)})
        page_help = first.get("pageHelp") if isinstance(first, dict) else None
        if not isinstance(page_help, dict):
            raise UniverseSourceError("上交所响应缺少分页数据")
        total_pages = _page_count(page_help.get("total"), page_help.get("pageSize"), "上交所")
        records = list(parser(first, self._clock()))
        for page_number in range(2, total_pages + 1):
            payload = self._sse_response(endpoint, {**base, **self._sse_page_params(page_number)})
            records.extend(parser(payload, self._clock()))
        return tuple(records)

    def _sse_response(self, endpoint: str, params: dict[str, str | int]) -> object:
        try:
            response = self._session.get(
                endpoint,
                params=params,
                headers=self._headers,
                timeout=self._timeout_seconds,
            )
            response.raise_for_status()
            payload = response.json()
        except (requests.RequestException, ValueError, OSError, AttributeError) as error:
            raise UniverseSourceError("上交所股票池请求失败") from error
        if not isinstance(payload, dict) or payload.get("success") is False:
            raise UniverseSourceError("上交所股票池响应无效")
        return payload

    def _sse_page_params(self, page_number: int) -> dict[str, int]:
        return {
            "pageHelp.cacheSize": 1,
            "pageHelp.beginPage": page_number,
            "pageHelp.pageNo": page_number,
            "pageHelp.pageSize": self._page_size,
            "pageHelp.endPage": page_number * 10 + 1,
        }


class SZSEUniverseAdapter:
    """Fetch current, suspended and delisted ordinary A shares from SZSE."""

    source = "szse"
    endpoint = "https://www.szse.cn/api/report/ShowReport/data"
    _hostname = "www.szse.cn"
    _fallback_addresses = ("114.94.127.138", "183.193.77.10")
    _headers = {
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Referer": "https://www.szse.cn/market/product/stock/list/index.html",
        "User-Agent": "A-Hunter-Market-Daily/1.0",
    }

    def __init__(
        self,
        session: _HttpSession | None = None,
        fallback_sessions: Iterable[_HttpSession] = (),
        clock: Callable[[], datetime] | None = None,
        *,
        page_size: int = 500,
        timeout_seconds: float = 20,
    ) -> None:
        if not isinstance(page_size, int) or page_size <= 0:
            raise ValueError("page_size must be positive")
        primary_session = session if session is not None else requests.Session()
        fallback_sessions = tuple(fallback_sessions)
        if session is None and not fallback_sessions:
            fallback_sessions = tuple(
                _pinned_https_session(self._hostname, address)
                for address in self._fallback_addresses
            )
        self._sessions = (primary_session, *fallback_sessions)
        self._active_session_index = 0
        self._clock = clock or _now
        self._page_size = page_size
        self._timeout_seconds = timeout_seconds

    def fetch(self) -> tuple[ExchangeSecurity, ...]:
        records: list[ExchangeSecurity] = []
        records.extend(self._fetch_catalog("1110", "tab1", parse_szse_current_response))
        records.extend(self._fetch_catalog("1793_ssgs", "tab1", parse_szse_suspended_response))
        records.extend(self._fetch_catalog("1793_ssgs", "tab2", parse_szse_delisted_response))
        return tuple(records)

    def _fetch_catalog(
        self,
        catalog_id: str,
        tabkey: str,
        parser: Callable[[object, datetime], tuple[ExchangeSecurity, ...]],
    ) -> tuple[ExchangeSecurity, ...]:
        first = self._szse_response(catalog_id, tabkey, 1)
        metadata = _szse_metadata(first, tabkey)
        total_pages = _page_count(metadata.get("recordcount"), metadata.get("pagesize"), "深交所")
        records = list(parser(first, self._clock()))
        for page_number in range(2, total_pages + 1):
            payload = self._szse_response(catalog_id, tabkey, page_number)
            records.extend(parser(payload, self._clock()))
        return tuple(records)

    def _szse_response(self, catalog_id: str, tabkey: str, page_number: int) -> object:
        params = {
            "SHOWTYPE": "JSON",
            "CATALOGID": catalog_id,
            "TABKEY": tabkey,
            "PAGENO": page_number,
            "PAGESIZE": self._page_size,
            f"{tabkey}PAGESIZE": self._page_size,
        }
        last_error: Exception | None = None
        saw_invalid_response = False
        for offset in range(len(self._sessions)):
            index = (self._active_session_index + offset) % len(self._sessions)
            try:
                response = self._sessions[index].get(
                    self.endpoint,
                    params=params,
                    headers=self._headers,
                    timeout=self._timeout_seconds,
                )
                response.raise_for_status()
                payload = response.json()
            except (requests.RequestException, ValueError, OSError, AttributeError) as error:
                last_error = error
                continue
            if not isinstance(payload, list):
                saw_invalid_response = True
                continue
            self._active_session_index = index
            return payload
        if saw_invalid_response:
            raise UniverseSourceError("深交所股票池响应无效") from last_error
        raise UniverseSourceError("深交所股票池请求失败") from last_error


def _szse_metadata(payload: object, tabkey: str) -> dict[str, object]:
    if not isinstance(payload, list):
        raise UniverseSourceError("深交所响应不是报表列表")
    for entry in payload:
        if isinstance(entry, dict) and isinstance(entry.get("metadata"), dict):
            metadata = entry["metadata"]
            if metadata.get("tabkey") == tabkey:
                return metadata
    raise UniverseSourceError("深交所响应缺少目标报表页")
