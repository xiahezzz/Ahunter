"""Small, dependency-free normalizers shared by public Research Providers.

The provider boundary is deliberately boring: source-specific JSON/HTML is
converted into bounded dictionaries before it reaches the Data Product Engine.
Keeping this code separate from HTTP transport makes fixture testing cheap and
makes a new public source an adapter change rather than an Agent change.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from html.parser import HTMLParser
import math
import re
from typing import Any, Iterable
from zoneinfo import ZoneInfo


_UTC = timezone.utc
_A_SHARE_TZ = ZoneInfo("Asia/Shanghai")


def parse_public_time(value: Any) -> datetime | None:
    """Parse public-feed timestamps; timezone-less A-share values use Asia/Shanghai."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)) and math.isfinite(float(value)):
        number = float(value)
        if abs(number) > 100_000_000_000:
            number /= 1000
        try:
            return datetime.fromtimestamp(number, tz=_UTC)
        except (OverflowError, OSError, ValueError):
            return None
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    if re.fullmatch(r"\d{10,13}", text):
        return parse_public_time(int(text))
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        parsed = None
    if parsed is not None:
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            parsed = parsed.replace(tzinfo=_A_SHARE_TZ)
        return parsed
    for pattern in ("%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S", "%Y-%m-%d", "%Y/%m/%d"):
        try:
            return datetime.strptime(text[:19], pattern).replace(tzinfo=_A_SHARE_TZ)
        except ValueError:
            continue
    return None


def finite_number(value: Any, *, allow_negative: bool = True) -> float | None:
    if value in (None, "", "--", "-", "None", "null"):
        return None
    try:
        number = float(str(value).replace(",", "").replace("%", ""))
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or (not allow_negative and number < 0):
        return None
    return number


def first_value(row: dict[str, Any], *names: str) -> Any:
    for name in names:
        if name in row and row[name] not in (None, ""):
            return row[name]
    lowered = {str(key).strip().lower(): value for key, value in row.items()}
    for name in names:
        value = lowered.get(name.lower())
        if value not in (None, ""):
            return value
    return None


class _TableParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[list[str]] = []
        self._row: list[str] | None = None
        self._cell: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag == "tr":
            self._row = []
        elif tag in {"td", "th"} and self._row is not None:
            self._cell = []

    def handle_data(self, data: str) -> None:
        if self._cell is not None:
            self._cell.append(data)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in {"td", "th"} and self._row is not None and self._cell is not None:
            self._row.append(" ".join("".join(self._cell).split()))
            self._cell = None
        elif tag == "tr" and self._row is not None:
            if any(self._row):
                self.rows.append(self._row)
            self._row = None
            self._cell = None


def parse_html_table(value: str) -> list[dict[str, str]]:
    """Parse the first useful HTML table into dictionaries.

    The parser intentionally supports only table cells. It does not execute,
    retain, or return page markup, which keeps source retention bounded.
    """
    parser = _TableParser()
    parser.feed(value)
    parser.close()
    rows = [row for row in parser.rows if row]
    if len(rows) < 2:
        return []
    headers = [re.sub(r"\s+", "_", item.strip().lower()) or f"column_{index}" for index, item in enumerate(rows[0])]
    result: list[dict[str, str]] = []
    for values in rows[1:]:
        if len(values) < 1:
            continue
        padded = values + [""] * (len(headers) - len(values))
        result.append({header: padded[index].strip() for index, header in enumerate(headers)})
    return result


def rows_from_payload(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    if isinstance(value, dict):
        for key in ("data", "rows", "items", "result"):
            candidate = value.get(key)
            if isinstance(candidate, dict):
                nested = rows_from_payload(candidate)
                if nested:
                    return nested
            if isinstance(candidate, list):
                return [item for item in candidate if isinstance(item, dict)]
    return []


def iso_or_none(value: Any) -> str | None:
    parsed = parse_public_time(value)
    return parsed.isoformat() if parsed is not None else None


def stable_key(*values: Any) -> str:
    return "|".join(str(value or "").strip().lower() for value in values)


@dataclass(frozen=True)
class NormalizationResult:
    payload: dict[str, Any]
    quality_status: str = "passed"
    quality_message: str | None = None


def bounded_text(value: Any, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit]


def bounded_items(items: Iterable[dict[str, Any]], limit: int = 100) -> list[dict[str, Any]]:
    return list(items)[:limit]
