import html
import re
from typing import Any

import requests

# Matches a "client/version" string anywhere in a row of the /clients/execution
# HTML page. Captures e.g. "ethrex/v12.0.0-HEAD-…/x86_64-…" or "reth/v2.2.0-…".
_VERSION_RE = re.compile(r'([A-Za-z][\w.+-]*/v?[0-9][^<"\s]*)')
_ROW_RE = re.compile(r'id="clientRow-([^"]+)"(.*?)</tr>', re.S)


class DoraClient:
    def __init__(self, base_url: str, timeout: int = 10):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._session = requests.Session()

    def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        url = f"{self.base_url}/api{path}"
        r = self._session.get(url, params=params, timeout=self.timeout)
        r.raise_for_status()
        data = r.json()
        if isinstance(data, dict) and data.get("status") and data["status"] != "OK":
            raise RuntimeError(f"dora API error at {path}: {data}")
        return data

    def slots(self, limit: int = 64, with_orphaned: int = 1, with_missing: int = 1) -> list[dict]:
        data = self._get(
            "/v1/slots",
            params={
                "limit": limit,
                "with_orphaned": with_orphaned,
                "with_missing": with_missing,
            },
        )
        payload = data.get("data") if isinstance(data, dict) else None
        if isinstance(payload, dict):
            return payload.get("slots") or []
        if isinstance(data, dict):
            return data.get("slots") or []
        return []

    def client_head_forks(self) -> dict:
        data = self._get("/v1/network/client_head_forks")
        return (data.get("data") if isinstance(data, dict) else {}) or {}

    def splits(self) -> dict:
        data = self._get("/v1/network/splits")
        return (data.get("data") if isinstance(data, dict) else {}) or {}

    def execution_versions(self) -> dict[str, str]:
        """Scrape /clients/execution HTML for per-EL version strings.

        The v1 JSON API does not expose the EL version that Dora's RPC scrape
        discovers (e.g. "ethrex/v12.0.0-..."), only the devp2p-crawler view
        which is empty for ethrex. So we parse the rendered table.
        """
        url = f"{self.base_url}/clients/execution"
        r = self._session.get(url, timeout=self.timeout)
        r.raise_for_status()
        body = r.text
        out: dict[str, str] = {}
        for name, row in _ROW_RE.findall(body):
            m = _VERSION_RE.search(row)
            if not m:
                continue
            out[name] = html.unescape(m.group(1))
        return out
