"""
url_validator.py — HTTP HEAD verification for user-supplied URLs.

Verifies planning_links provided by the user in the manifest.
AI-generated content URLs are NOT handled here — see url_discovery.py.
"""
from __future__ import annotations
import logging
import time
from typing import Any
from urllib.parse import urlparse
import requests
from requests.exceptions import RequestException

logger = logging.getLogger(__name__)
DEFAULT_TIMEOUT = 10
DEFAULT_UA = "RoadTripItineraryGenerator/1.0"
MAX_RETRIES = 2


class URLValidator:
    def __init__(self, timeout: int = DEFAULT_TIMEOUT, user_agent: str = DEFAULT_UA) -> None:
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": user_agent})
        self.results: list[dict[str, Any]] = []

    def verify_planning_links(self, trip: dict[str, Any]) -> None:
        for dest in trip.get("destinations", []):
            for link in dest.get("planning_links", []):
                ok, status = self._check(link.get("url", ""))
                link["verified"] = ok
                link["http_status"] = status
                logger.log(
                    logging.INFO if ok else logging.WARNING,
                    "[%s] %s → %s", "OK" if ok else "FAIL", link.get("url", ""), status
                )
                self.results.append({"url": link.get("url"), "verified": ok, "status": status})

    def verify_url(self, url: str) -> tuple[bool, int | str]:
        return self._check(url)

    def _check(self, url: str) -> tuple[bool, int | str]:
        if not url or not urlparse(url).scheme:
            return False, "invalid_url"
        for attempt in range(MAX_RETRIES + 1):
            try:
                resp = self.session.head(url, timeout=self.timeout, allow_redirects=True)
                if resp.status_code == 405:
                    resp = self.session.get(url, timeout=self.timeout, allow_redirects=True, stream=True)
                    resp.close()
                return resp.status_code < 400, resp.status_code
            except RequestException as exc:
                if attempt == MAX_RETRIES:
                    return False, str(exc)
                time.sleep(1)
        return False, "timeout"
