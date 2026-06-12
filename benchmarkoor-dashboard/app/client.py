"""HTTP client for the Benchmarkoor API with retry/backoff.

The API intermittently returns 500/502/503/504/524 under load, so every request
is retried with exponential backoff. `query()` unwraps the PostgREST-style
`{data, limit, offset}` envelope; `paginate()` pulls every page of a table.
"""

from __future__ import annotations

from typing import Any

import httpx
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
    wait_random,
)

from . import config

RETRY_STATUS = {500, 502, 503, 504, 524}


class RetryableStatus(Exception):
    pass


class Client:
    def __init__(
        self, base: str | None = None, key: str | None = None, timeout: float = 60.0
    ):
        self.base = (base or config.API_BASE).rstrip("/")
        self.key = key or config.require_key()
        self._http = httpx.Client(
            timeout=timeout,
            headers={"Authorization": f"Bearer {self.key}"},
        )

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "Client":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @retry(
        retry=retry_if_exception_type((RetryableStatus, httpx.TransportError)),
        wait=wait_exponential(multiplier=1, min=1, max=30) + wait_random(0, 2),
        stop=stop_after_attempt(10),
        reraise=True,
    )
    def _get(self, path: str, params: dict[str, Any] | None = None) -> httpx.Response:
        if not path.startswith("/"):
            path = "/" + path
        if not path.startswith("/api/"):
            path = "/api/v1" + path
        resp = self._http.get(self.base + path, params=params)
        if resp.status_code in RETRY_STATUS:
            raise RetryableStatus(f"{resp.status_code} for {path}")
        resp.raise_for_status()
        return resp

    def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        return self._get(path, params).json()

    def query(self, table: str, params: dict[str, Any] | None = None) -> list[dict]:
        """One page of /index/query/<table>; returns the unwrapped data list."""
        data = self.get(f"/index/query/{table}", params)
        if isinstance(data, dict) and "data" in data:
            return data["data"]
        return data  # some endpoints return a bare list

    def paginate(
        self, table: str, params: dict[str, Any] | None = None, page: int = 250
    ) -> list[dict]:
        """Pull every row of a query table by walking limit/offset.

        page is capped at 250: the API 500s on heavy tables (e.g. runs, with its
        large steps_json blobs) when limit >= 500, so we stay under that cap."""
        params = dict(params or {})
        offset = 0
        out: list[dict] = []
        while True:
            params.update(limit=page, offset=offset)
            rows = self.query(table, params)
            out.extend(rows)
            if len(rows) < page:
                break
            offset += page
        return out
