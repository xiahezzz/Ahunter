"""Named, non-interactive commands over the existing A Hunter Web API."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
from typing import Sequence
from urllib.parse import quote

from advisor.cli.catalog import COMMANDS, Command
from advisor.cli.client import ApiClient, CliError


class Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise CliError("usage", message)


def build_parser() -> Parser:
    parser = Parser(description="A Hunter agent CLI: local HTTP API, JSON output, no prompts or automatic retries", allow_abbrev=False)
    # Common options on every level support both `ahunter --timeout 5 health`
    # and `ahunter health --timeout 5` without child defaults erasing values.
    def common(p):
        p.add_argument("--base-url", default=argparse.SUPPRESS, help="HTTP loopback origin; AHUNTER_BASE_URL or http://127.0.0.1:8000")
        p.add_argument("--timeout", type=float, default=argparse.SUPPRESS, help="HTTP connect/read timeout seconds (default 30)")
        p.add_argument("--pretty", action="store_true", default=argparse.SUPPRESS, help="Indent JSON output")

    common(parser)
    groups = {(): parser.add_subparsers(dest="group_0", required=True)}
    discovery = groups[()].add_parser("commands", help="Describe all commands and input fields as JSON", allow_abbrev=False)
    common(discovery)
    discovery.set_defaults(discovery=True)
    for spec in COMMANDS:
        parts = tuple(spec.command.split())
        for depth, part in enumerate(parts):
            prefix = parts[:depth + 1]
            if prefix in groups:
                continue
            leaf = depth == len(parts) - 1
            child = groups[parts[:depth]].add_parser(part, help=spec.help if leaf else f"{part} commands", description=spec.help if leaf else None, allow_abbrev=False)
            common(child)
            if not leaf:
                groups[prefix] = child.add_subparsers(dest=f"group_{depth + 1}", required=True)
                continue
            child.set_defaults(spec=spec)
            for name in spec.path_fields:
                child.add_argument(name, help=f"Exact {name}; path segment, not a URL")
            for field in spec.query:
                child.add_argument(field.option, dest="query_" + field.name, type={"int": int, "float": float}.get(field.kind, str),
                                   action="append" if field.repeated else "store", help=field.help or field.name)
            if spec.body or spec.json_only:
                child.add_argument("--json", metavar="FILE", help="Complete JSON request body from UTF-8 file; - reads stdin. Do not combine with body flags.")
            if not spec.json_only:
                for field in spec.body:
                    child.add_argument(field.option, dest="body_" + field.name, type={"int": int, "float": float}.get(field.kind, str),
                                       action="append" if field.repeated else "store",
                                       help=(field.help or field.name) + (" (required unless --json)" if field.required else ""))
            if spec.download:
                child.add_argument("--output", required=True, help="New destination file; parent must exist; never overwrites")
    return parser


def _read_text(source: str) -> str:
    try:
        return sys.stdin.read() if source == "-" else Path(source).expanduser().read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        raise CliError("input_file", "Cannot read UTF-8 input", 2) from None


def _invalid_constant(value: str):
    raise ValueError(f"Invalid JSON constant: {value}")


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key")
        result[key] = value
    return result


def request_body(spec: Command, args: argparse.Namespace):
    source = getattr(args, "json", None)
    fields = {f.name: getattr(args, "body_" + f.name, None) for f in spec.body}
    if source is not None:
        if any(value is not None for value in fields.values()):
            raise CliError("usage", "--json cannot be combined with body flags")
        try:
            payload = json.loads(_read_text(source), parse_constant=_invalid_constant, object_pairs_hook=_unique_object)
        except (ValueError, RecursionError):
            raise CliError("invalid_input", "Input must be strict JSON without duplicate keys", 2) from None
    elif spec.json_only:
        raise CliError("usage", "This command requires --json FILE (or --json -)")
    else:
        payload = {}
        for field in spec.body:
            value = fields[field.name]
            if value is not None:
                payload[field.name] = _read_text(value) if field.kind == "text-file" else value
    if spec.json_only == "array":
        if not isinstance(payload, list):
            raise CliError("invalid_input", "Expected a JSON array of transactions")
    else:
        if not isinstance(payload, dict):
            raise CliError("invalid_input", "Expected a JSON object")
        allowed = {f.name for f in spec.body}
        if set(payload) - allowed:
            raise CliError("invalid_input", "Unexpected request body fields: " + ", ".join(sorted(set(payload) - allowed)))
        missing = [f.name for f in spec.body if f.required and f.name not in payload]
        if missing:
            raise CliError("usage", "Missing required body fields: " + ", ".join(missing))
        # The API requires an explicit nullable code field for research requests.
        if spec.command in {"research requests create", "research lagent requests create"}:
            payload.setdefault("code", None)
    try:
        json.dumps(payload, allow_nan=False)
    except (ValueError, RecursionError):
        raise CliError("invalid_input", "Request values must be finite JSON values") from None
    return payload if spec.method != "GET" else None


def main(argv: Sequence[str] | None = None) -> int:
    args = None
    try:
        args = build_parser().parse_args(argv)
        if getattr(args, "discovery", False):
            result = {"ok": True, "status": None, "data": {"commands": [c.describe() for c in COMMANDS]}}
        else:
            spec = args.spec
            path = spec.path
            for field in spec.path_fields:
                value = getattr(args, field)
                if not value or value in {".", ".."} or any(c in value for c in "/\\\x00\r\n"):
                    raise CliError("usage", f"{field} must be a non-empty single path segment")
                path = path.replace("{" + field + "}", quote(value, safe=""))
            query = {f.name: getattr(args, "query_" + f.name) for f in spec.query if getattr(args, "query_" + f.name) is not None}
            body = request_body(spec, args)
            client = ApiClient(getattr(args, "base_url", os.environ.get("AHUNTER_BASE_URL", "http://127.0.0.1:8000")), getattr(args, "timeout", 30))
            try:
                result = client.request(spec.method, path, query=query, body=body, output=getattr(args, "output", None))
            finally:
                client.close()
        print(json.dumps(result, ensure_ascii=False, allow_nan=False, indent=2 if getattr(args, "pretty", False) else None))
        return 0
    except CliError as error:
        print(json.dumps(error.payload(), ensure_ascii=False, allow_nan=False), file=sys.stderr)
        return error.exit_code
    except KeyboardInterrupt:
        print(json.dumps(CliError("interrupted", "Interrupted; a submitted write may still complete", 130).payload()), file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
