"""Loopback-only HTTP transport with explicit errors and atomic downloads."""

from __future__ import annotations

from hashlib import sha256
from ipaddress import ip_address
import json
import math
import os
from pathlib import Path
import tempfile
from urllib.parse import urlsplit

import requests


class CliError(Exception):
    def __init__(self, code: str, message: str, exit_code: int = 2, *, status: int | None = None, detail=None):
        super().__init__(message)
        self.code = code
        self.exit_code = exit_code
        self.status = status
        self.detail = detail

    def payload(self) -> dict:
        return {"ok": False, "status": self.status, "error": {
            "code": self.code, "message": str(self), "detail": self.detail,
        }}


def validate_base_url(value: str) -> str:
    try:
        parsed = urlsplit(value)
        host = parsed.hostname or ""
        local = host == "localhost" or ip_address(host).is_loopback
        if (not local or parsed.scheme != "http" or parsed.username is not None
                or parsed.password is not None or parsed.path not in {"", "/"}
                or parsed.query or parsed.fragment or not 0 < (parsed.port if parsed.port is not None else 80) < 65536):
            raise ValueError
    except ValueError:
        raise CliError("usage", "--base-url must be an HTTP loopback origin, e.g. http://127.0.0.1:8000") from None
    return value.rstrip("/")


class ApiClient:
    def __init__(self, base_url: str, timeout: float = 30):
        self.base_url = validate_base_url(base_url)
        if not math.isfinite(timeout) or timeout <= 0:
            raise CliError("usage", "--timeout must be a positive finite number of seconds")
        self.timeout = timeout
        self.session = requests.Session()
        # Local traffic must not inherit proxies, netrc credentials, or cookies.
        self.session.trust_env = False
        self.session.headers.update({"Accept": "application/json", "User-Agent": "ahunter-cli/1"})

    def close(self) -> None:
        self.session.close()

    def request(self, method: str, path: str, *, query: dict, body, output: str | None = None) -> dict:
        destination = None
        if output is not None:
            destination = Path(output).expanduser().absolute()
            if os.path.lexists(destination):
                raise CliError("output_exists", "Output already exists; choose a new path", 5)
            if not destination.parent.is_dir():
                raise CliError("output_directory", "Output parent directory does not exist", 5)
        try:
            with self.session.request(
                method, self.base_url + path, params=query, json=body,
                timeout=self.timeout, allow_redirects=False, stream=True,
            ) as response:
                status = response.status_code
                if not 200 <= status < 300:
                    try:
                        detail = response.json()
                        json.dumps(detail, allow_nan=False)
                    except (ValueError, RecursionError):
                        detail = None
                    raise CliError("http_error", "API request failed; no automatic retry was made", 4, status=status, detail=detail)
                if destination is not None:
                    return {"ok": True, "status": status, "data": _download(response, destination)}
                try:
                    data = response.json()
                    # Emit strict JSON even if a malfunctioning server sends NaN.
                    json.dumps(data, allow_nan=False)
                except (ValueError, RecursionError):
                    raise CliError("invalid_response", "API returned invalid JSON", 5, status=status) from None
                return {"ok": True, "status": status, "data": data}
        except requests.RequestException:
            message = "Cannot complete the local API request. Check API status; a write may already have been accepted. Do not blindly resubmit."
            raise CliError("transport_error", message, 3) from None
        except OSError:
            raise CliError("file_error", "Could not write the download; no existing output was replaced", 5) from None


def _download(response, destination: Path) -> dict:
    """Publish a complete file without replacing existing files or symlinks."""
    digest = sha256()
    size = 0
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=destination.parent, prefix=".ahunter-", delete=False) as stream:
            temporary = Path(stream.name)
            for chunk in response.iter_content(chunk_size=64 * 1024):
                if chunk:
                    stream.write(chunk)
                    digest.update(chunk)
                    size += len(chunk)
            stream.flush()
            os.fsync(stream.fileno())
        # Hard-link publication is atomic and fails if destination appeared mid-download.
        os.link(temporary, destination)
        return {"path": str(destination), "bytes": size, "sha256": digest.hexdigest(),
                "content_type": response.headers.get("Content-Type")}
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
