"""Safety checks shared by every Market-scope output boundary.

Market research may discuss a broad index, sector, or policy condition.  It
must never turn that discussion into a recommendation for a named security.
These checks deliberately inspect reader-visible text rather than relying on
schema field names: a model could otherwise hide a recommendation in a
methodology or free-form details field.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any, Iterable

if TYPE_CHECKING:
    from advisor.research.data_products.engine import SnapshotResult


# The common A-share, STAR, ChiNext, and Beijing-exchange code families.  The
# boundary guards keep dates and longer numeric identifiers from being treated
# as securities while still matching codes such as ``600519.SH``.
_A_SHARE_CODE = re.compile(
    r"(?<![0-9A-Za-z])(?:(?:sh|sz|bj)[._-]?)?(?:00[0-3]|30[0-1]|60[0135]|68[89]|(?:4|8|9)[0-9]{2})[0-9]{3}(?![0-9A-Za-z])",
    re.IGNORECASE,
)
# Fractional seconds can have the same six digits as a security code. Preserve
# the surrounding prose while excluding complete timestamps from entity scans.
_TIMESTAMP = re.compile(
    r"(?<![0-9A-Za-z])\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}"
    r"(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})?(?![0-9A-Za-z])"
)
_SECURITY_ACTION = re.compile(
    r"(?:买入|卖出|持有|加仓|减仓|建仓|清仓|推荐|看多|看空|看好|做多|做空|关注|回避|布局|"
    r"增持|减持|超配|低配|介入|止损|持仓|值得买|可以买|可买入|适合买入|适合做多|低吸|上车|"
    r"加大配置|配置|首选|优选|投资价值|高估|低估|上涨|下跌|跑赢|跑输|强势|弱势|"
    r"买|卖|buy|sell|hold|long|short|bullish|bearish|top[ -]?pick|overweight|underweight)",
    re.IGNORECASE,
)
# A recommendation can be expressed without an explicit order verb: "建议
# 投资" and "值得持仓" are still per-security stances. Keep this as a
# structural phrase instead of blindly classifying every occurrence of
# "投资" (which can describe a company's business activity).
_RECOMMENDATION_CONSTRUCT = re.compile(
    r"(?:建议|推荐|值得|适合|应该|应当|宜|可以|"
    r"可(?:投资|持仓|配置|布局|建仓|买入|卖出|加仓|减仓|做多|做空|关注))"
    r"[\s\S]*(?:投资|持仓|配置|布局|建仓|买入|卖出|加仓|减仓|做多|做空|关注)",
    re.IGNORECASE,
)
FORBIDDEN_MARKET_CONCLUSION_KEYS = frozenset({
    "candidate", "candidates", "research_candidate", "stance", "price_range",
    "target_price", "position", "position_limit", "trade", "trade_action",
    "security_recommendation", "child_request", "child_requests", "buy", "sell",
})
_FORBIDDEN_MARKET_TEXT = re.compile(
    r"(?:候选股|股票候选|个股推荐|(?:建议|推荐)(?:买入|卖出|加仓|减仓)|目标价|仓位|交易指令|"
    r"(?:watch_buy|watch_add|watch_reduce|watch_exit|buy|sell)\b)",
    re.IGNORECASE,
)


def snapshot_security_names(snapshot: "SnapshotResult | Any") -> frozenset[str]:
    """Return the local, sealed A-share name mapping for a Market Snapshot.

    The complete intraday Snapshot already defines the Market universe.
    Reading its name fields introduces no mutable dictionary, network lookup,
    or extra Agent visibility.  Names shorter than two characters are too
    ambiguous to use as a security-entity signal.
    """
    products = getattr(snapshot, "products", None)
    if not isinstance(products, dict):
        return frozenset()
    product = products.get("whole_market_intraday_snapshot@1")
    payload = product.payload if product is not None else None
    if not isinstance(payload, dict):
        return frozenset()
    rows = payload.get("rows")
    expected = payload.get("expected_universe")
    expected_securities = expected.get("securities") if isinstance(expected, dict) else None
    identity_rows = [
        *(rows if isinstance(rows, list) else []),
        *(expected_securities if isinstance(expected_securities, list) else []),
    ]
    return frozenset(
        name
        for row in identity_rows
        if isinstance(row, dict)
        for code in (str(row.get("code", "")).strip(),)
        if _A_SHARE_CODE.fullmatch(code)
        for raw_name in (row.get("name"),)
        if isinstance(raw_name, str)
        for name in (raw_name.strip(),)
        if len(name) >= 2
    )


def contains_per_security_action(text: str, *, security_names: Iterable[str] = ()) -> bool:
    """Whether one reader-visible text value recommends an individual stock.

    There is intentionally no character-distance allowance between the code
    and action.  A long explanatory phrase must not make an otherwise direct
    recommendation acceptable.
    """
    text = _TIMESTAMP.sub(" ", text)
    if not (_SECURITY_ACTION.search(text) or _RECOMMENDATION_CONSTRUCT.search(text)):
        return False
    if _A_SHARE_CODE.search(text):
        return True
    return any(name and name in text for name in security_names)


def contains_forbidden_market_text(text: str) -> bool:
    """Return whether prose itself contains an explicitly forbidden output."""
    return bool(_FORBIDDEN_MARKET_TEXT.search(text))


def contains_forbidden_market_fields(value: Any) -> bool:
    """Inspect nested schema-like values for disallowed Security fields."""
    if isinstance(value, dict):
        return bool(FORBIDDEN_MARKET_CONCLUSION_KEYS & {str(key).lower() for key in value}) or any(
            contains_forbidden_market_fields(child) for child in value.values()
        )
    if isinstance(value, (list, tuple)):
        return any(contains_forbidden_market_fields(child) for child in value)
    return False


def reader_text_values(value: Any) -> list[str]:
    """Return every reader-visible string nested in a structured payload.

    Structured method objects may be rendered as text or JSON by a report
    consumer, so their keys are reader-visible too.  Inspecting both keys and
    values prevents a recommendation from being hidden in a free-form key.
    """
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [
            text
            for key, child in value.items()
            for text in ([key] if isinstance(key, str) else []) + reader_text_values(child)
        ]
    if isinstance(value, (list, tuple)):
        return [text for child in value for text in reader_text_values(child)]
    return []


def contains_per_security_action_in_values(
    values: list[str],
    *,
    security_names: Iterable[str] = (),
) -> bool:
    """Check leaves and their rendered aggregate for an individual action.

    The aggregate catches a recommendation split across adjacent structured
    fields (for example, a code in one details value and its action in the
    next).  Leaf checks retain clear attribution for ordinary prose.
    """
    names = tuple(security_names)
    return any(contains_per_security_action(value, security_names=names) for value in values) or contains_per_security_action(
        "\n".join(values), security_names=names
    )


def contains_unsafe_market_output(value: Any, *, security_names: Iterable[str] = ()) -> bool:
    """Return whether a reader-visible Market output crosses into Security advice.

    This is the common boundary predicate for a Market Finding, the fixed
    Market Pipeline, and publication.  Keeping the final writer on the same
    predicate means a caller cannot bypass the pipeline merely by directly
    constructing a typed ``MarketTeamReport``.
    """
    text_values = reader_text_values(value)
    names = tuple(security_names)
    return (
        contains_forbidden_market_fields(value)
        or any(contains_forbidden_market_text(text) for text in text_values)
        or any(contains_per_security_action(text, security_names=names) for text in text_values)
        or contains_per_security_action_in_values(
            reader_text_values(_without_evidence_provenance(value)), security_names=names,
        )
    )


def _without_evidence_provenance(value: Any) -> Any:
    """Keep citation identity out of cross-field recommendation matching.

    A data provider can also be a listed company. Its name in an EvidenceRef
    is not the target of an action in another Insight. Every original leaf
    is still checked above, including explicit advice in a source field.
    Only contracted top-level/Insight evidence is treated as provenance;
    arbitrary nested method fields retain cross-field checks.
    """
    if not isinstance(value, dict):
        return value
    result = dict(value)
    if isinstance(value.get("evidence"), (list, tuple)):
        result["evidence"] = [
            {key: child for key, child in item.items()
             if key not in {"source", "locator", "evidence_id", "observed_at"}}
            if isinstance(item, dict) else item
            for item in value["evidence"]
        ]
    if isinstance(value.get("insights"), (list, tuple)):
        result["insights"] = [_without_evidence_provenance(item) for item in value["insights"]]
    return result
