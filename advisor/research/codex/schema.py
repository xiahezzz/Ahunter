"""Compile application JSON Schemas for Codex Structured Outputs.

Pydantic emits general-purpose JSON Schema.  Codex Structured Outputs accepts
only a strict subset, so every generated schema passes through this module
before it is written into a Run Capsule.  Application-side Pydantic validation
still owns the complete output contract after generation.
"""

from __future__ import annotations

from typing import Any


def compile_output_schema(value: dict[str, Any]) -> dict[str, Any]:
    """Return a detached, strict Codex-compatible output schema.

    Defaults are input-construction metadata rather than output validation.
    They are removed everywhere, including next to ``$ref`` where the Codex
    schema validator rejects them.  Object fields are made explicitly required
    and closed to undeclared properties, matching Structured Outputs rules.
    """
    if not isinstance(value, dict):
        raise ValueError("structured output schema must be an object")
    compiled = _compile_node(value)
    if not isinstance(compiled, dict):  # pragma: no cover - guarded above
        raise ValueError("structured output schema must be an object")
    if not compiled:
        compiled = {"type": "object"}
    if compiled.get("type") != "object" and "properties" not in compiled:
        raise ValueError("structured output root schema must be an object")
    return _close_object(compiled)


def _compile_node(value: Any) -> Any:
    if isinstance(value, list):
        return [_compile_node(item) for item in value]
    if not isinstance(value, dict):
        return value

    reference = value.get("$ref")
    if reference is not None:
        if not isinstance(reference, str) or not reference.startswith("#"):
            raise ValueError("structured output schema requires a local $ref")
        # Codex rejects validation or annotation keywords alongside $ref.
        # Pydantic validation still enforces those constraints after output.
        return {"$ref": reference}

    compiled = {
        key: _compile_node(child)
        for key, child in value.items()
        if key != "default"
    }
    if compiled.get("type") == "array" and not isinstance(compiled.get("items"), dict):
        raise ValueError("structured output array schema requires items")
    if compiled.get("type") == "object" or "properties" in compiled:
        return _close_object(compiled)
    return compiled


def _close_object(value: dict[str, Any]) -> dict[str, Any]:
    properties = value.get("properties")
    if properties is None:
        properties = {}
    if not isinstance(properties, dict) or any(not isinstance(name, str) for name in properties):
        raise ValueError("structured output object properties must be a mapping")
    value["type"] = "object"
    value["properties"] = properties
    value["additionalProperties"] = False
    value["required"] = sorted(properties)
    return value
