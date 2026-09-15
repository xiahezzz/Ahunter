from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
from subprocess import CompletedProcess

import pytest

from advisor.research.providers.eastmoney_transport import (
    EASTMONEY_CLIST_ENDPOINT,
    EastmoneyNodeSession,
)
from advisor.research.providers.industry_taxonomy import (
    EastmoneyIndustryTaxonomyProvider,
    normalize_industry_taxonomy_rows,
)
from advisor.research.contracts import ResearchBoundary, ResearchSubject, VersionRef
from advisor.research.data_products.engine import ProductRequest
from advisor.research.providers.whole_market_intraday import StaticExpectedUniverse
from advisor.research.repository import ResearchRepository


class _Response:
    def __init__(self, payload: object) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> object:
        return self._payload


class _TaxonomySession:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []
        self.endpoints: list[str] = []

    def get(self, endpoint: str, *, params: dict[str, object], **_kwargs: object) -> _Response:
        self.endpoints.append(endpoint)
        self.calls.append(dict(params))
        if str(params["fs"]).startswith("m:90"):
            return _Response({"data": {"diff": [{"f12": "BK0001", "f14": "样例行业"}]}})
        return _Response({"data": {"diff": [{"f12": "600000"}]}})


def test_taxonomy_uses_eastmoney_first_level_board_filter(tmp_path: Path):
    repository = ResearchRepository.open(tmp_path / "advisor.sqlite")
    session = _TaxonomySession()
    provider = EastmoneyIndustryTaxonomyProvider(
        repository,
        StaticExpectedUniverse(["600000"]),
        session=session,  # type: ignore[arg-type]
    )

    rows = tuple(provider._fetch_first_level())

    assert session.calls[0]["fs"] == "m:90+s:4"
    assert session.endpoints == [EASTMONEY_CLIST_ENDPOINT, EASTMONEY_CLIST_ENDPOINT]
    assert rows == ({"industry_id": "BK0001", "name": "样例行业", "level": "first", "members": ["600000"]},)
    repository.close()


def test_default_taxonomy_transport_uses_bounded_node_fetch(tmp_path: Path):
    repository = ResearchRepository.open(tmp_path / "advisor.sqlite")
    provider = EastmoneyIndustryTaxonomyProvider(repository, StaticExpectedUniverse(["600000"]))

    assert isinstance(provider._session, EastmoneyNodeSession)
    repository.close()


def test_taxonomy_refresh_failure_keeps_a_bounded_exception_chain(tmp_path: Path):
    repository = ResearchRepository.open(tmp_path / "advisor.sqlite")

    def unavailable():
        try:
            raise TimeoutError("fixture transport detail must not be persisted")
        except TimeoutError as error:
            raise RuntimeError("fixture wrapper detail must not be persisted") from error

    provider = EastmoneyIndustryTaxonomyProvider(
        repository,
        StaticExpectedUniverse(["600000"]),
        fetcher=unavailable,
    )
    request = ProductRequest(
        VersionRef.parse("industry_sector_taxonomy@1"),
        ResearchSubject(scope="market"),
        ResearchBoundary(as_of=datetime(2026, 8, 6, 8, 30, tzinfo=timezone.utc)),
    )

    observation = provider.fetch(request)

    assert observation.quality_status == "blocked"
    assert observation.quality_message is not None
    assert "RuntimeError<-TimeoutError" in observation.quality_message
    assert "fixture" not in observation.quality_message
    repository.close()


def test_taxonomy_ignores_a_small_valid_membership_set_outside_the_sealed_universe():
    expected = {f"600{index:03d}" for index in range(100)}
    normalized = normalize_industry_taxonomy_rows(
        [{
            "industry_id": "BK0001",
            "name": "样例行业",
            "members": [*sorted(expected), "601123"],
        }],
        expected_codes=expected,
        as_of=datetime(2026, 9, 6, tzinfo=timezone.utc),
        fetched_at=datetime(2026, 9, 6, tzinfo=timezone.utc),
    )

    assert normalized["coverage"] == 1.0
    assert normalized["assigned_count"] == 100
    assert normalized["out_of_scope_member_count"] == 1
    assert "601123" not in normalized["industries"][0]["members"]


def test_node_transport_preserves_batch_response_order(tmp_path: Path):
    node = tmp_path / "node"
    script = tmp_path / "fetch.mjs"
    node.write_text("fixture", encoding="utf-8")
    script.write_text("fixture", encoding="utf-8")
    captured: dict[str, object] = {}

    def runner(command, **kwargs):
        captured["command"] = command
        captured["payload"] = json.loads(kwargs["input"])
        return CompletedProcess(
            command,
            0,
            stdout=json.dumps({"responses": [{"page": 1}, {"page": 2}]}),
            stderr="",
        )

    session = EastmoneyNodeSession(
        node_path=node,
        script_path=script,
        runner=runner,
        clock=lambda: datetime(2026, 8, 9, tzinfo=timezone.utc),
    )

    responses = session.get_many(
        EASTMONEY_CLIST_ENDPOINT,
        ({"pn": 1}, {"pn": 2}),
        timeout=3,
        headers={"User-Agent": "fixture"},
    )

    assert captured["command"] == [str(node), str(script)]
    assert captured["payload"]["requests"] == [{"params": {"pn": 1}}, {"params": {"pn": 2}}]
    assert [response.json()["page"] for response in responses] == [1, 2]


def test_node_transport_rejects_the_live_quote_host_before_spawning(tmp_path: Path):
    node = tmp_path / "node"
    script = tmp_path / "fetch.mjs"
    node.write_text("fixture", encoding="utf-8")
    script.write_text("fixture", encoding="utf-8")

    def runner(*_args, **_kwargs):
        raise AssertionError("live endpoint must be rejected before spawning Node")

    session = EastmoneyNodeSession(node_path=node, script_path=script, runner=runner)

    with pytest.raises(ValueError, match="allowlisted"):
        session.get(
            "https://push2.eastmoney.com/api/qt/clist/get",
            params={"pn": 1},
            timeout=3,
        )
