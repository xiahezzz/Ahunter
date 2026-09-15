from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest
import requests

from advisor.market_daily.contracts import MarketSecurity
from advisor.market_daily.providers.exchanges import (
    ExchangeSecurity,
    SZSEUniverseAdapter,
    UniverseSourceError,
    parse_sse_current_page,
    parse_sse_delisted_page,
    parse_sse_suspended_page,
    parse_szse_current_response,
    parse_szse_delisted_response,
    parse_szse_suspended_response,
)
from advisor.market_daily.repository import MarketDailyRepository
from advisor.market_daily.universe import HistoricalUniverseService, UniverseBuilder, UniverseError


SHANGHAI = ZoneInfo("Asia/Shanghai")
NOW = datetime(2026, 8, 7, 21, 0, tzinfo=SHANGHAI)


def record(
    code: str,
    name: str,
    exchange: str,
    listed: date,
    *,
    delisted: date | None = None,
    status: str = "active",
) -> ExchangeSecurity:
    return ExchangeSecurity(
        code=code,
        name=name,
        exchange=exchange,
        list_date=listed,
        delist_date=delisted,
        status=status,
        source="sse" if exchange == "SH" else "szse",
        source_at=NOW,
        fetched_at=NOW,
        is_st="ST" in name.upper(),
    )


def test_sse_parser_keeps_only_common_sh_a_share_rows():
    payload = {
        "pageHelp": {
            "data": [
                {"SECURITY_CODE_A": "600070", "SECURITY_ABBR_A": "*ST 富润", "LISTING_DATE": "1997-06-04"},
                {"SECURITY_CODE_A": "688001", "SECURITY_ABBR_A": "华兴源创", "LISTING_DATE": "2019-07-22"},
                {"SECURITY_CODE_A": "689009", "SECURITY_ABBR_A": "九号公司-WD", "LISTING_DATE": "2020-10-29"},
                {"SECURITY_CODE_A": "900901", "SECURITY_ABBR_A": "云赛B股", "LISTING_DATE": "1990-12-19"},
                {"SECURITY_CODE_A": "920001", "SECURITY_ABBR_A": "北交所样例", "LISTING_DATE": "2021-11-15"},
            ]
        }
    }

    parsed = parse_sse_current_page(payload, NOW)

    assert [security.code for security in parsed] == ["600070", "688001"]
    assert parsed[0].is_st is True


def _szse_payload(tabkey: str, rows: list[dict[str, object]]) -> list[dict[str, object]]:
    return [{"metadata": {"tabkey": tabkey}, "data": rows}]


def test_exchange_fixtures_cover_sh_sz_current_st_suspended_delisted_and_new_listings():
    current_sh = parse_sse_current_page(
        {
            "pageHelp": {
                "data": [
                    {"SECURITY_CODE_A": "600070", "SECURITY_ABBR_A": "*ST 富润", "LISTING_DATE": "1997-06-04"},
                    {"SECURITY_CODE_A": "689009", "SECURITY_ABBR_A": "CDR", "LISTING_DATE": "2020-10-29"},
                    {"SECURITY_CODE_A": "900901", "SECURITY_ABBR_A": "B 股", "LISTING_DATE": "1990-12-19"},
                ]
            }
        },
        NOW,
    )
    suspended_sh = parse_sse_suspended_page(
        {"pageHelp": {"data": [{"A_STOCK_CODE": "600068", "COMPANY_ABBR": "葛洲坝", "LIST_DATE": "1997-05-26"}]}},
        NOW,
    )
    delisted_sh = parse_sse_delisted_page(
        {
            "pageHelp": {
                "data": [
                    {"COMPANY_CODE": "600001", "COMPANY_ABBR": "邯郸钢铁", "LIST_DATE": "1998-01-22", "DELIST_DATE": "2009-12-29"}
                ]
            }
        },
        NOW,
    )
    current_sz = parse_szse_current_response(
        _szse_payload(
            "tab1",
            [
                {"agdm": "000001", "agjc": "平安银行", "agssrq": "1991-04-03"},
                {"agdm": "001000", "agjc": "新上市样例", "agssrq": "2026-08-10"},
                {"agdm": "200001", "agjc": "B 股", "agssrq": "1991-04-03"},
                {"agdm": "920001", "agjc": "北交所", "agssrq": "2021-11-15"},
            ],
        ),
        NOW,
    )
    suspended_sz = parse_szse_suspended_response(
        _szse_payload("tab1", [{"zqdm": "300111", "zqjc": "向日葵", "ssrq": "2010-08-27"}]),
        NOW,
    )
    delisted_sz = parse_szse_delisted_response(
        _szse_payload(
            "tab2",
            [{"zqdm": "000999", "zqjc": "窗口内退市", "ssrq": "2010-01-01", "zzrq": "2023-06-30"}],
        ),
        NOW,
    )

    snapshot = UniverseBuilder().seal(
        (*current_sh, *suspended_sh, *delisted_sh, *current_sz, *suspended_sz, *delisted_sz)
    )
    selected = snapshot.for_window(date(2021, 8, 7), date(2026, 8, 7))

    assert [security.code for security in snapshot.securities] == ["000001", "000999", "001000", "300111", "600001", "600068", "600070"]
    assert [security.code for security in selected] == ["000001", "000999", "300111", "600068", "600070"]
    assert next(security for security in snapshot.securities if security.code == "600070").is_st is True
    assert next(security for security in snapshot.securities if security.code == "300111").status == "suspended"
    assert next(security for security in snapshot.securities if security.code == "000999").status == "delisted"


def test_szse_parser_rejects_missing_listing_or_delisting_dates():
    with pytest.raises(UniverseSourceError, match="上市日期"):
        parse_szse_current_response(_szse_payload("tab1", [{"agdm": "000001", "agjc": "平安银行"}]), NOW)
    with pytest.raises(UniverseSourceError, match="终止上市日期"):
        parse_szse_delisted_response(
            _szse_payload("tab2", [{"zqdm": "000999", "zqjc": "退市样例", "ssrq": "2010-01-01"}]),
            NOW,
        )


def test_szse_adapter_reuses_fallback_after_primary_route_fails():
    class PrimaryRoute:
        def __init__(self):
            self.calls = 0

        def get(self, _url, **_kwargs):
            self.calls += 1
            raise requests.ConnectionError("unreachable CDN node")

    class Response:
        def __init__(self, payload):
            self._payload = payload

        def raise_for_status(self):
            return None

        def json(self):
            return self._payload

    class FallbackRoute:
        def __init__(self):
            self.calls = 0

        def get(self, _url, **kwargs):
            self.calls += 1
            params = kwargs["params"]
            rows = (
                [{"agdm": "000001", "agjc": "平安银行", "agssrq": "1991-04-03"}]
                if params["CATALOGID"] == "1110"
                else []
            )
            return Response(
                [
                    {
                        "metadata": {
                            "tabkey": params["TABKEY"],
                            "recordcount": len(rows),
                            "pagesize": 20,
                        },
                        "data": rows,
                    }
                ]
            )

    primary = PrimaryRoute()
    fallback = FallbackRoute()
    adapter = SZSEUniverseAdapter(
        session=primary,
        fallback_sessions=(fallback,),
        clock=lambda: NOW,
    )

    records = adapter.fetch()

    assert [record.code for record in records] == ["000001"]
    assert primary.calls == 1
    assert fallback.calls == 3


def test_szse_adapter_requests_large_pages_for_each_report_tab():
    class Response:
        def __init__(self, payload):
            self._payload = payload

        def raise_for_status(self):
            return None

        def json(self):
            return self._payload

    class Route:
        def __init__(self):
            self.params: list[dict[str, object]] = []

        def get(self, _url, **kwargs):
            params = dict(kwargs["params"])
            self.params.append(params)
            return Response(
                [
                    {
                        "metadata": {
                            "tabkey": params["TABKEY"],
                            "recordcount": 0,
                            "pagesize": 500,
                        },
                        "data": [],
                    }
                ]
            )

    route = Route()
    SZSEUniverseAdapter(session=route, page_size=500, clock=lambda: NOW).fetch()

    assert len(route.params) == 3
    assert all(params["PAGESIZE"] == 500 for params in route.params)
    assert all(params[f"{params['TABKEY']}PAGESIZE"] == 500 for params in route.params)


def test_szse_pinned_session_keeps_official_host_and_tls_identity():
    from advisor.market_daily.providers import exchanges

    factory = getattr(exchanges, "_pinned_https_session", None)
    assert callable(factory)

    session = factory("www.szse.cn", "114.94.127.138")
    request = session.prepare_request(requests.Request("GET", SZSEUniverseAdapter.endpoint))
    connection = session.get_adapter(request.url).get_connection_with_tls_context(request, True)

    assert session.trust_env is False
    assert request.headers["Host"] == "www.szse.cn"
    assert connection.host == "114.94.127.138"
    assert connection.assert_hostname == "www.szse.cn"
    assert connection.conn_kw["server_hostname"] == "www.szse.cn"


def test_default_szse_adapter_builds_verified_official_cdn_fallbacks(monkeypatch):
    from advisor.market_daily.providers import exchanges

    class PrimaryRoute:
        def get(self, _url, **_kwargs):
            raise requests.ConnectionError("unreachable local CDN node")

    class Response:
        def __init__(self, payload):
            self._payload = payload

        def raise_for_status(self):
            return None

        def json(self):
            return self._payload

    class WorkingRoute:
        def get(self, _url, **kwargs):
            params = kwargs["params"]
            rows = (
                [{"agdm": "000001", "agjc": "平安银行", "agssrq": "1991-04-03"}]
                if params["CATALOGID"] == "1110"
                else []
            )
            return Response(
                [
                    {
                        "metadata": {
                            "tabkey": params["TABKEY"],
                            "recordcount": len(rows),
                            "pagesize": 20,
                        },
                        "data": rows,
                    }
                ]
            )

    pinned: list[tuple[str, str]] = []
    working = WorkingRoute()
    monkeypatch.setattr(exchanges.requests, "Session", lambda: PrimaryRoute())
    monkeypatch.setattr(
        exchanges,
        "_pinned_https_session",
        lambda hostname, address: pinned.append((hostname, address)) or working,
    )

    records = SZSEUniverseAdapter(clock=lambda: NOW).fetch()

    assert [record.code for record in records] == ["000001"]
    assert pinned == [
        ("www.szse.cn", "114.94.127.138"),
        ("www.szse.cn", "183.193.77.10"),
    ]


def test_snapshot_is_order_independent_and_selects_historical_window():
    records = (
        record("000001", "平安银行", "SZ", date(1991, 4, 3)),
        record("300111", "向日葵", "SZ", date(2010, 8, 27), status="suspended"),
        record("600068", "葛洲坝", "SH", date(1997, 5, 26), delisted=date(2021, 9, 13), status="delisted"),
        record("600001", "邯郸钢铁", "SH", date(1998, 1, 22), delisted=date(2009, 12, 29), status="delisted"),
        record("001000", "新上市样例", "SZ", date(2026, 8, 10)),
    )

    first = UniverseBuilder().seal(records)
    second = UniverseBuilder().seal(tuple(reversed(records)))
    selected = first.for_window(date(2021, 8, 8), date(2026, 8, 7))

    assert first.content_hash == second.content_hash
    assert [security.code for security in selected] == ["000001", "300111", "600068"]


def test_missing_listing_date_or_conflicting_interval_fails_closed():
    with pytest.raises(UniverseSourceError):
        parse_sse_current_page(
            {"pageHelp": {"data": [{"SECURITY_CODE_A": "600000", "SECURITY_ABBR_A": "浦发银行"}]}},
            NOW,
        )
    with pytest.raises(UniverseError, match="上市区间"):
        UniverseBuilder().seal(
            (
                record("600000", "浦发银行", "SH", date(1999, 11, 10)),
                record("600000", "浦发银行", "SH", date(1999, 11, 11)),
            )
        )


def test_current_row_does_not_conflict_with_matching_delisted_interval():
    snapshot = UniverseBuilder().seal(
        (
            record("600193", "退市创兴", "SH", date(1999, 5, 27)),
            record(
                "600193",
                "退市创兴",
                "SH",
                date(1999, 5, 27),
                delisted=date(2026, 7, 6),
                status="delisted",
            ),
        )
    )

    assert snapshot.securities == (
        record(
            "600193",
            "退市创兴",
            "SH",
            date(1999, 5, 27),
            delisted=date(2026, 7, 6),
            status="delisted",
        ).to_market_security(),
    )


def test_historical_universe_normalizes_adapter_source_failure(tmp_path):
    class FailingAdapter:
        def fetch(self):
            raise UniverseSourceError("fixture source unavailable")

    service = HistoricalUniverseService(
        MarketDailyRepository(tmp_path / "advisor.sqlite"),
        (FailingAdapter(),),
    )

    with pytest.raises(UniverseError, match="fixture source unavailable"):
        service.refresh()


def test_repository_does_not_erase_saved_delisting_interval(tmp_path):
    repository = MarketDailyRepository(tmp_path / "advisor.sqlite")
    historical = record(
        "600068",
        "葛洲坝",
        "SH",
        date(1997, 5, 26),
        delisted=date(2021, 9, 13),
        status="delisted",
    ).to_market_security()
    current_claim = MarketSecurity(
        code="600068",
        name="葛洲坝",
        exchange="SH",
        list_date=date(1997, 5, 26),
        delist_date=None,
        status="active",
        is_st=False,
        source="sse",
        source_at=NOW,
        fetched_at=NOW,
    )

    assert repository.upsert_security(historical) == "inserted"
    assert repository.upsert_security(current_claim) == "retained"
