"""Bounded, non-sensitive Provider failure summaries for durable audit state."""

from __future__ import annotations


def exception_type_chain(error: BaseException, *, maximum_depth: int = 4) -> str:
    """Return exception class provenance without persisting transport text."""

    if not isinstance(maximum_depth, int) or isinstance(maximum_depth, bool) or maximum_depth < 1:
        raise ValueError("maximum_depth must be a positive integer")
    names: list[str] = []
    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and len(names) < maximum_depth and id(current) not in seen:
        seen.add(id(current))
        name = type(current).__name__
        names.append(name[:80] if name else "Exception")
        current = current.__cause__ or current.__context__
    return "<-".join(names)


__all__ = ["exception_type_chain"]
