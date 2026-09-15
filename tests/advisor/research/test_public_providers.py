from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from advisor.research.data_products.engine import ProductRequest
from advisor.research.contracts import ResearchBoundary, ResearchSubject, VersionRef
from advisor.research.providers.information import MarketInformationProvider, PublicMarketInformationSources
from advisor.research.providers.public import PublicAStockProvider, build_default_provider_registry
from advisor.research.providers.whole_market_intraday import HybridTradingSessionAuthority, SinaWholeMarketIntradayProvider
from advisor.research.repository import ResearchRepository


def _request(product: str) -> ProductRequest:
    return ProductRequest(VersionRef.parse(product), ResearchSubject(code="600519"), ResearchBoundary(as_of=datetime(2026, 8, 6, 8, 30, tzinfo=timezone.utc)))


def _market_request(product: str) -> ProductRequest:
    return ProductRequest(
        VersionRef.parse(product),
        ResearchSubject(scope="market"),
        ResearchBoundary(as_of=datetime(2026, 8, 6, 8, 30, tzinfo=timezone.utc)),
    )


def test_mx_provider_uses_only_the_supplied_quality_checked_snapshot():
    event = SimpleNamespace(
        evidence_id="evidence-1", rid=123, summary="关注 600519", received_at=datetime(2026, 8, 6, 8, 0, tzinfo=timezone.utc),
        source_created_at=None, content_hash="a" * 64, source_type="mx", source_id="event-1", media=()
    )
    snapshot = SimpleNamespace(events=(event,), quality=SimpleNamespace(blocking_failure=False))
    provider = PublicAStockProvider(mx_snapshot=snapshot)

    observation = provider.fetch(_request("mx_events@1"))

    assert observation.quality_status == "passed"
    assert observation.payload["items"][0]["evidence_id"] == "evidence-1"
    assert observation.payload["items"][0]["summary"] == "关注 600519"


def test_mx_provider_blocks_when_collector_quality_is_blocking():
    snapshot = SimpleNamespace(events=(), quality=SimpleNamespace(blocking_failure=True))
    observation = PublicAStockProvider(mx_snapshot=snapshot).fetch(_request("mx_events@1"))

    assert observation.quality_status == "blocked"


def test_default_registry_never_registers_a_network_daily_bar_fallback():
    registry = build_default_provider_registry()

    assert len(registry.providers_for("market_daily_bars@1")) == 0
    assert len(registry.providers_for("lockup_calendar@1")) == 1


def test_unapproved_market_structure_products_fail_closed_without_network():
    class NoNetwork:
        def __init__(self) -> None:
            self.calls = 0

        def get(self, *_args, **_kwargs):
            self.calls += 1
            raise AssertionError("unapproved market structure source must not be called")

    session = NoNetwork()
    observation = PublicAStockProvider(session=session).fetch(_request("lockup_calendar@1"))  # type: ignore[arg-type]

    assert session.calls == 0
    assert observation.quality_status == "warning"
    assert observation.coverage == 0.0


def test_default_registry_wires_bounded_official_and_media_market_information_sources():
    registry = build_default_provider_registry()

    provider = registry.providers_for("market_information@1")[0]

    assert isinstance(provider, MarketInformationProvider)
    assert isinstance(provider.official_fetcher.__self__, PublicMarketInformationSources)
    assert isinstance(provider.media_fetcher.__self__, PublicMarketInformationSources)


def test_market_information_failure_keeps_a_bounded_exception_chain():
    def unavailable():
        try:
            raise TimeoutError("fixture transport detail must not be persisted")
        except TimeoutError as error:
            raise RuntimeError("fixture wrapper detail must not be persisted") from error

    observation = MarketInformationProvider(
        official_fetcher=unavailable,
        media_fetcher=unavailable,
    ).fetch(_market_request("market_information@1"))

    assert observation.quality_status == "warning"
    assert observation.quality_message == (
        "official:RuntimeError<-TimeoutError; media:RuntimeError<-TimeoutError"
    )
    assert "fixture" not in observation.quality_message


def test_market_information_retains_one_source_failure_when_other_source_is_usable():
    def unavailable():
        try:
            raise TimeoutError("fixture transport detail must not be persisted")
        except TimeoutError as error:
            raise RuntimeError("fixture wrapper detail must not be persisted") from error

    observation = MarketInformationProvider(
        official_fetcher=unavailable,
        media_fetcher=lambda: [{
            "title": "样例市场信息",
            "summary": "可用媒体来源",
            "published_at": "2026-08-06T08:00:00+00:00",
            "publisher": "样例媒体",
            "url": "https://example.test/market-information",
        }],
    ).fetch(_market_request("market_information@1"))

    assert observation.quality_status == "passed"
    assert len(observation.payload["items"]) == 1
    assert observation.quality_message == "official:RuntimeError<-TimeoutError"
    assert "fixture" not in observation.quality_message


def test_default_registry_wires_the_observed_session_authority_for_bulk_market_sources(tmp_path):
    database = tmp_path / "advisor.sqlite"
    ResearchRepository.open(database).close()

    registry = build_default_provider_registry(database_path=database)
    providers = registry.providers_for("whole_market_intraday_snapshot@1")

    assert len(providers) == 1
    assert isinstance(providers[0], SinaWholeMarketIntradayProvider)
    assert all(isinstance(provider.session_authority, HybridTradingSessionAuthority) for provider in providers)
