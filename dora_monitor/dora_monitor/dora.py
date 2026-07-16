import html
import logging
import re
import time
from typing import Any

import requests

log = logging.getLogger(__name__)

# Matches a "client/version" string anywhere in a row of the /clients/execution
# HTML page. Captures e.g. "ethrex/v12.0.0-HEAD-…/x86_64-…" or "reth/v2.2.0-…".
_VERSION_RE = re.compile(r'([A-Za-z][\w.+-]*/v?[0-9][^<"\s]*)')
_ROW_RE = re.compile(r'id="clientRow-([^"]+)"(.*?)</tr>', re.S)


class DoraClient:
    def __init__(
        self,
        base_url: str,
        timeout: int = 10,
        retries: int = 2,
        retry_backoff: float = 1.0,
    ):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        # Extra attempts after the first on a transient network error (timeout
        # or connection failure). Dora's public endpoints occasionally stall for
        # a few seconds; a couple of retries turns a would-be aborted tick into
        # a successful one without waiting a whole poll interval to try again.
        self.retries = max(retries, 0)
        self.retry_backoff = max(retry_backoff, 0.0)
        self._session = requests.Session()

    def _request(self, url: str, **kwargs: Any) -> requests.Response:
        """GET with bounded retry on transient network errors.

        Only timeouts and connection errors are retried — an HTTP status error
        or malformed body is deterministic and propagates on the first attempt.
        """
        last_exc: Exception | None = None
        for attempt in range(self.retries + 1):
            try:
                r = self._session.get(url, timeout=self.timeout, **kwargs)
                r.raise_for_status()
                return r
            except (requests.Timeout, requests.ConnectionError) as e:
                last_exc = e
                if attempt >= self.retries:
                    break
                delay = self.retry_backoff * (attempt + 1)
                log.warning(
                    "dora GET %s failed (%s); retry %d/%d in %.1fs",
                    url,
                    type(e).__name__,
                    attempt + 1,
                    self.retries,
                    delay,
                )
                time.sleep(delay)
        assert last_exc is not None
        raise last_exc

    def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        url = f"{self.base_url}/api{path}"
        r = self._request(url, params=params)
        data = r.json()
        if isinstance(data, dict):
            api_status = data.get("status")
            if api_status and api_status != "OK":
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
        # Cap the read at 512 KB. Dora's real page is ~220 KB; this guards
        # against a malformed/runaway response triggering pathological
        # regex backtracking or a huge in-memory string.
        r = self._request(url, stream=True)
        body = r.raw.read(512 * 1024, decode_content=True).decode(
            r.encoding or "utf-8", errors="replace"
        )
        out: dict[str, str] = {}
        for name, row in _ROW_RE.findall(body):
            m = _VERSION_RE.search(row)
            if not m:
                continue
            out[name] = html.unescape(m.group(1))
        return out
