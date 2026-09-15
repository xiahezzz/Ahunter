from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json

from advisor.research.providers.information import (
    MarketInformationSource,
    PublicMarketInformationSources,
    normalize_information_rows,
)


AS_OF = datetime(2026, 8, 6, 8, 30, tzinfo=timezone.utc)
FETCHED = datetime(2026, 8, 6, 8, 31, tzinfo=timezone.utc)


def _hash(payload):
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def test_information_rows_are_time_bounded_stable_and_deduplicated_with_source_trace():
    rows = [
        {"title": "same", "summary": "content", "url": "fixture://a", "published_at": "2026-08-05T10:00:00Z", "source": "A"},
        {"title": "same", "summary": "content", "url": "fixture://b", "published_at": "2026-08-05T10:00:00Z", "source": "B"},
        {"title": "future", "summary": "not visible", "published_at": "2026-08-07T10:00:00Z"},
        {"title": "missing time", "summary": "invalid"},
    ]
    first = normalize_information_rows(rows, code="600519", product="company_news", as_of=AS_OF, source_locator="fixture://feed", fetched_at=FETCHED)
    second = normalize_information_rows(list(reversed(rows)), code="600519", product="company_news", as_of=AS_OF, source_locator="fixture://feed", fetched_at=FETCHED)
    assert len(first.payload["items"]) == 1
    assert first.payload["items"][0]["source_variants"] == ["A", "B"]
    assert set(first.payload["items"][0]["source_locators"]) == {"fixture://a", "fixture://b", "fixture://feed"}
    assert _hash(first.payload) == _hash(second.payload)
    assert first.payload["invalid_rows"] == 2


def test_empty_information_window_is_not_a_source_failure():
    result = normalize_information_rows([], code="600519", product="macro_news", as_of=AS_OF, source_locator="fixture://feed", fetched_at=FETCHED)
    assert result.payload["status"] == "empty"
    assert result.quality_status == "passed"


class _Response:
    def __init__(self, markup: str) -> None:
        self.text = markup

    def raise_for_status(self) -> None:
        return None


class _Session:
    def __init__(self, markup: str) -> None:
        self.markup = markup
        self.calls: list[tuple[str, float]] = []

    def get(self, endpoint: str, *, timeout: float, headers: dict[str, str]):
        self.calls.append((endpoint, timeout))
        assert headers["User-Agent"] == "A-Hunter/1.0"
        return _Response(self.markup)


def test_public_market_information_adapter_keeps_only_same_origin_dated_listing_rows():
    endpoint = "https://official.example/news"
    session = _Session(
        """
        <ul>
          <li><a href="/notice/1">公开市场通知</a><span>2026年08月05日 14:30</span></li>
          <li><a href="/same-day-date-only">当天未标时分</a><span>2026-08-06</span></li>
          <li><a href="https://other.example/notice">跨域链接</a><span>2026-08-05 14:30</span></li>
          <li><a href="/undated">没有日期</a></li>
        </ul>
        """
    )
    sources = PublicMarketInformationSources(
        session=session,
        timeout=1.5,
        max_attempts=1,
        minimum_interval_seconds=0,
        official_sources=(MarketInformationSource(endpoint, "官方来源", "policy"),),
        media_sources=(),
    )

    rows = sources.official()

    assert rows == [{
        "title": "公开市场通知",
        "summary": "公开市场通知 2026年08月05日 14:30",
        "published_at": "2026-08-05T14:30:00+08:00",
        "original_url": "https://official.example/notice/1",
        "publisher": "官方来源",
        "category": "policy",
    }]
    assert session.calls == [(endpoint, 1.5)]


def test_public_market_information_accepts_prior_date_only_rows_at_safe_day_end():
    endpoint = "https://official.example/news"
    session = _Session(
        """
        <meta name="others" content="页面生成时间 2026-08-09 18:00:00" />
        <ul>
          <li><a href="/notice/1">前一日政策</a><span class="time">08-08</span></li>
          <li><a href="/notice/2">当日日期不完整</a><span class="time">08-09</span></li>
        </ul>
        """
    )
    sources = PublicMarketInformationSources(
        session=session,
        timeout=1,
        max_attempts=1,
        minimum_interval_seconds=0,
        official_sources=(MarketInformationSource(endpoint, "官方来源", "policy"),),
        media_sources=(),
    )

    rows = sources.official()

    assert rows[0]["published_at"] == "2026-08-08T23:59:59+08:00"
    assert rows[1]["published_at"] == "2026-08-09T23:59:59+08:00"


def test_public_market_information_combines_url_date_with_visible_clock():
    endpoint = "https://media.example/"
    session = _Session(
        """
        <ul><li><em>17:28</em><a href="/2026/08/09/detail_1.html">市场消息</a></li></ul>
        """
    )
    sources = PublicMarketInformationSources(
        session=session,
        timeout=1,
        max_attempts=1,
        minimum_interval_seconds=0,
        official_sources=(),
        media_sources=(MarketInformationSource(endpoint, "媒体来源", "market_media"),),
    )

    rows = sources.media()

    assert rows[0]["published_at"] == "2026-08-09T17:28:00+08:00"
