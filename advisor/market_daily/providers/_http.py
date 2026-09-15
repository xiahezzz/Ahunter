"""Small HTTPS routing helpers shared by public-source adapters."""

from __future__ import annotations

from typing import Any

import requests
from requests.adapters import HTTPAdapter


class _PinnedHTTPSAdapter(HTTPAdapter):
    def __init__(self, hostname: str, address: str) -> None:
        self._hostname = hostname
        self._address = address
        super().__init__()

    def get_connection_with_tls_context(
        self,
        request: Any,
        verify: Any,
        proxies: dict[str, str] | None = None,
        cert: Any = None,
    ) -> Any:
        host_params, pool_kwargs = self.build_connection_pool_key_attributes(request, verify, cert)
        host_params.update(host=self._address, port=443)
        pool_kwargs.update(server_hostname=self._hostname, assert_hostname=self._hostname)
        return self.poolmanager.connection_from_host(**host_params, pool_kwargs=pool_kwargs)


def pinned_https_session(hostname: str, address: str) -> requests.Session:
    """Connect to one address while preserving Host, SNI and certificate checks."""

    session = requests.Session()
    session.trust_env = False
    session.headers["Host"] = hostname
    session.mount(f"https://{hostname}/", _PinnedHTTPSAdapter(hostname, address))
    return session
