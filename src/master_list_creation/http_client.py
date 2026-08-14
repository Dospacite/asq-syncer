"""Small, dependency-free HTTP and cache helpers."""

from __future__ import annotations

import json
import os
from pathlib import Path
import random
import tempfile
import time
from typing import Any, Mapping
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


USER_AGENT = (
    "anime-song-quiz-master-list/1.0 "
    "(+https://github.com/Dospacite/anime_song_quiz)"
)


class RequestError(RuntimeError):
    """Raised after an upstream request exhausts its retries."""


class RateLimiter:
    """Enforce a minimum interval between requests to one upstream."""

    def __init__(self, minimum_interval: float) -> None:
        self.minimum_interval = minimum_interval
        self._last_request = 0.0

    def wait(self) -> None:
        remaining = self.minimum_interval - (time.monotonic() - self._last_request)
        if remaining > 0:
            time.sleep(remaining)
        self._last_request = time.monotonic()


class HttpClient:
    def __init__(self, timeout: float = 90.0, attempts: int = 5) -> None:
        self.timeout = timeout
        self.attempts = attempts

    def get_json(self, url: str, *, rate_limiter: RateLimiter | None = None) -> Any:
        return json.loads(self.get_bytes(url, rate_limiter=rate_limiter))

    def post_json(
        self,
        url: str,
        payload: Mapping[str, Any],
        *,
        rate_limiter: RateLimiter | None = None,
    ) -> Any:
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        response = self._request(
            url,
            method="POST",
            body=body,
            headers={"Content-Type": "application/json"},
            rate_limiter=rate_limiter,
        )
        return json.loads(response)

    def get_bytes(
        self, url: str, *, rate_limiter: RateLimiter | None = None
    ) -> bytes:
        return self._request(url, method="GET", rate_limiter=rate_limiter)

    def _request(
        self,
        url: str,
        *,
        method: str,
        body: bytes | None = None,
        headers: Mapping[str, str] | None = None,
        rate_limiter: RateLimiter | None = None,
    ) -> bytes:
        request_headers = {"Accept": "application/json, application/xml, text/xml"}
        request_headers["User-Agent"] = USER_AGENT
        request_headers.update(headers or {})
        last_error: Exception | None = None

        for attempt in range(self.attempts):
            if rate_limiter is not None:
                rate_limiter.wait()
            request = Request(
                url,
                data=body,
                headers=request_headers,
                method=method,
            )
            try:
                with urlopen(request, timeout=self.timeout) as response:
                    return response.read()
            except HTTPError as error:
                last_error = error
                if error.code not in {408, 429, 500, 502, 503, 504}:
                    detail = error.read(500).decode("utf-8", errors="replace")
                    raise RequestError(
                        f"{method} {url} returned HTTP {error.code}: {detail}"
                    ) from error
                retry_after = error.headers.get("Retry-After")
                try:
                    delay = float(retry_after) if retry_after else 2**attempt
                except ValueError:
                    # Retry-After may be an HTTP date instead of a delay in seconds.
                    delay = 2**attempt
            except (TimeoutError, URLError) as error:
                last_error = error
                delay = 2**attempt

            if attempt + 1 < self.attempts:
                time.sleep(delay + random.uniform(0.0, 0.25))

        raise RequestError(
            f"{method} {url} failed after {self.attempts} attempts: {last_error}"
        ) from last_error


def read_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def write_json(path: Path, value: Any, *, pretty: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=path.parent, delete=False
        ) as handle:
            temporary = Path(handle.name)
            json.dump(
                value,
                handle,
                ensure_ascii=False,
                indent=2 if pretty else None,
                separators=None if pretty else (",", ":"),
            )
            handle.write("\n")
        mode = (path.stat().st_mode & 0o777) if path.exists() else 0o644
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    except Exception:
        if temporary is not None and temporary.exists():
            temporary.unlink()
        raise
