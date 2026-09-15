"""Shared report publication and reader-boundary validation."""
import re


_RESEARCH_REPORT_FORBIDDEN_KEYS = frozenset({
    "raw_payload", "raw_payload_hash", "prompt", "prompts", "log", "logs",
    "local_path", "path", "cookie", "cookies", "token", "authorization",
    "debugger_url", "websocket_debugger_url", "codex_session", "session_material",
})
_RESEARCH_REPORT_UNSAFE_TEXT = re.compile(r"(?:file://|(?:^|[\\/])(?:Users|private|tmp)(?:[\\/])|127\.0\.0\.1|localhost)", re.I)


def is_public_report(payload: object, markdown: str, *, depth: int = 0) -> bool:
    """Keep Record detail on the public, bounded report contract only."""
    if depth > 20 or not isinstance(markdown, str) or _RESEARCH_REPORT_UNSAFE_TEXT.search(markdown):
        return False
    if isinstance(payload, dict):
        for key, value in payload.items():
            if not isinstance(key, str) or key.lower() in _RESEARCH_REPORT_FORBIDDEN_KEYS:
                return False
            if isinstance(value, str) and (len(value) > 16_000 or _RESEARCH_REPORT_UNSAFE_TEXT.search(value)):
                return False
            if not is_public_report(value, "", depth=depth + 1):
                return False
        return True
    if isinstance(payload, list):
        return len(payload) <= 1_000 and all(is_public_report(item, "", depth=depth + 1) for item in payload)
    if isinstance(payload, str):
        return len(payload) <= 16_000 and not _RESEARCH_REPORT_UNSAFE_TEXT.search(payload)
    return payload is None or isinstance(payload, (bool, int, float))
