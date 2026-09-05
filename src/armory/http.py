"""HTTP client factory.

Two drivers:
  http        — plain httpx (public sites)
  impersonate — curl_cffi with a Chrome TLS fingerprint (Cloudflare-fronted
                sites reject httpx's TLS handshake regardless of cookies)
"""

from __future__ import annotations

import time
from dataclasses import dataclass

BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)


class FetchError(Exception):
    pass


@dataclass(slots=True)
class FetchResponse:
    status_code: int
    text: str
    url: str


class Fetcher:
    def __init__(self, driver: str = "http", cookies: dict[str, str] | None = None, headers: dict[str, str] | None = None):
        self.driver = driver
        base_headers = {
            "User-Agent": BROWSER_UA,
            "Accept-Language": "en-US,en;q=0.9",
        }
        if headers:
            base_headers.update(headers)
        self.headers = base_headers
        if driver == "impersonate":
            from curl_cffi import requests as cffi_requests

            self._session = cffi_requests.Session(impersonate="chrome")
        else:
            import httpx

            self._session = httpx.Client(follow_redirects=True, timeout=30.0)
        # Seed the SESSION jar (not per-request dicts): responses update this
        # jar, so rotated cookies persist and get sent on later requests.
        for name, value in (cookies or {}).items():
            self._session.cookies.set(name, value)

    def get(self, url: str, params: dict | None = None, retries: int = 2, timeout: float = 30.0) -> FetchResponse:
        last_exc: Exception | None = None
        for attempt in range(retries + 1):
            try:
                r = self._session.get(url, params=params, headers=self.headers, timeout=timeout)
                return FetchResponse(r.status_code, r.text, str(r.url))
            except Exception as exc:  # noqa: BLE001 — network layer raises many types
                last_exc = exc
                if attempt < retries:
                    time.sleep(1.5 * (attempt + 1))
        raise FetchError(f"GET {url} failed: {last_exc}")

    def post(self, url: str, json_payload, headers: dict[str, str] | None = None) -> FetchResponse:
        merged = dict(self.headers)
        if headers:
            merged.update(headers)
        if self.driver == "impersonate":
            r = self._session.post(url, json=json_payload, headers=merged, timeout=30.0)
        else:
            r = self._session.post(url, json=json_payload, headers=merged)
        return FetchResponse(r.status_code, r.text, str(r.url))

    def close(self) -> None:
        try:
            self._session.close()
        except Exception:  # noqa: BLE001
            pass
