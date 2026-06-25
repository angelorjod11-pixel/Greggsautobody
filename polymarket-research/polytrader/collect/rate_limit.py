"""HTTP plumbing for the live collectors: token-bucket rate limiting + retry.

All Polymarket endpoints are public but rate-limited.  We throttle client-side
with a token bucket and retry transient failures (429 / 5xx / network) with
exponential backoff via tenacity.  ``httpx`` honors ``HTTPS_PROXY`` and the
system CA bundle through ``trust_env=True`` (default), so this works behind a
corporate egress proxy without extra configuration.
"""
from __future__ import annotations

import threading
import time
from typing import Any, Dict, Optional

import httpx
from tenacity import (retry, retry_if_exception_type, stop_after_attempt,
                      wait_exponential)


class TokenBucket:
    """Simple thread-safe token bucket for client-side rate limiting."""

    def __init__(self, rate_per_sec: float, capacity: Optional[float] = None):
        self.rate = max(rate_per_sec, 0.01)
        self.capacity = capacity or max(rate_per_sec, 1.0)
        self._tokens = self.capacity
        self._last = time.monotonic()
        self._lock = threading.Lock()

    def take(self, n: float = 1.0) -> None:
        with self._lock:
            while True:
                now = time.monotonic()
                self._tokens = min(self.capacity, self._tokens + (now - self._last) * self.rate)
                self._last = now
                if self._tokens >= n:
                    self._tokens -= n
                    return
                sleep_for = (n - self._tokens) / self.rate
                time.sleep(min(sleep_for, 1.0))


class HttpClient:
    """Rate-limited JSON HTTP client with retry/backoff."""

    def __init__(self, rate_per_sec: float = 4.0, max_retries: int = 5,
                 backoff_base: float = 1.5, timeout: float = 30.0):
        self.bucket = TokenBucket(rate_per_sec)
        self.max_retries = max_retries
        self.backoff_base = backoff_base
        self._client = httpx.Client(timeout=timeout, follow_redirects=True,
                                    headers={"User-Agent": "polytrader/0.1"})

    def get_json(self, url: str, params: Optional[Dict[str, Any]] = None) -> Any:
        @retry(reraise=True,
               stop=stop_after_attempt(self.max_retries),
               wait=wait_exponential(multiplier=self.backoff_base, min=1, max=60),
               retry=retry_if_exception_type((httpx.TransportError, httpx.HTTPStatusError)))
        def _do() -> Any:
            self.bucket.take()
            r = self._client.get(url, params=params)
            if r.status_code == 429 or r.status_code >= 500:
                r.raise_for_status()  # triggers retry
            r.raise_for_status()
            return r.json()

        return _do()

    def post_json(self, url: str, json_body: Dict[str, Any]) -> Any:
        @retry(reraise=True,
               stop=stop_after_attempt(self.max_retries),
               wait=wait_exponential(multiplier=self.backoff_base, min=1, max=60),
               retry=retry_if_exception_type((httpx.TransportError, httpx.HTTPStatusError)))
        def _do() -> Any:
            self.bucket.take()
            r = self._client.post(url, json=json_body)
            if r.status_code == 429 or r.status_code >= 500:
                r.raise_for_status()
            r.raise_for_status()
            return r.json()

        return _do()

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "HttpClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
