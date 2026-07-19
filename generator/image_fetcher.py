"""
image_fetcher.py — Fetch destination images from NPS API and Wikimedia Commons.

Strategy:
  1. NPS API first for national parks (requires nps_park_code)
  2. Wikimedia MediaSearch for all destinations
  3. Automatic fallback query sequence (up to 4 attempts) on failure
  4. Hard fail if < min_per_destination verified images found

Images are embedded as data URIs in the HTML (base64) OR stored as
relative paths in output/images/ depending on config.
"""
from __future__ import annotations
import hashlib, logging, mimetypes, os, time
from io import BytesIO
from pathlib import Path
from typing import Any
from openai import images
import requests
import urllib.parse

logger = logging.getLogger(__name__)
NPS_API_BASE = "https://developer.nps.gov/api/v1"
WIKIMEDIA_SEARCH = "https://commons.wikimedia.org/w/api.php"
WIKIMEDIA_INFO = "https://commons.wikimedia.org/w/api.php"
THUMB_WIDTH = 960
MAX_FALLBACK_ATTEMPTS = 4
REQUEST_DELAY = 1.5


class ImageFetcher:
    def __init__(self, config_path: str | Path = "config.yaml") -> None:
        import yaml
        with Path(config_path).open() as f:
            cfg = yaml.safe_load(f)
        self._nps_key = os.environ.get("NPS_API_KEY", "DEMO_KEY")
        images_cfg = cfg.get("images", {})
        self._min_per_dest = images_cfg.get("min_per_destination", 2)
        self._max_per_dest = images_cfg.get("max_per_destination", 4)
        self._output_dir = Path("output/images")
        self._output_dir.mkdir(parents=True, exist_ok=True)
        self._session = requests.Session()
        self._session.headers.update({"User-Agent": "RoadTripItineraryGenerator/1.0"})

    # ── Public entry point ───────────────────────────────────────────────────

    def fetch_all(self, trip: dict[str, Any]) -> None:
        for dest in trip.get("destinations", []):
            logger.info("Fetching images for '%s'…", dest["name"])
            images = self._fetch_for_dest(dest)
            if len(images) < self._min_per_dest:
                raise RuntimeError(
                    f"Image fetch failed for '{dest['name']}': "
                    f"only {len(images)} image(s) verified (min: {self._min_per_dest})"
                )
            dest["images"] = images
            logger.info("  %d image(s) acquired for '%s'", len(images), dest["name"])

    # ── Per-destination fetch ────────────────────────────────────────────────

    def _fetch_for_dest(self, dest: dict[str, Any]) -> list[dict[str, Any]]:
        images: list[dict[str, Any]] = []

        # Source 1: NPS API
        if dest.get("nps_park_code"):
            images.extend(self._fetch_from_nps(dest["nps_park_code"]))

    # Source 2: Unsplash (preferred over Wikimedia)
        if len(images) < self._max_per_dest:
            remaining = self._max_per_dest - len(images)
            images.extend(self._fetch_from_unsplash(dest["name"], limit=remaining))

    # Source 3: Wikimedia (fallback)
        if len(images) < self._max_per_dest:
            remaining = self._max_per_dest - len(images)
            images.extend(self._fetch_from_wikimedia(dest["name"], limit=remaining + 2))

        # Verify and deduplicate
        verified: list[dict[str, Any]] = []
        seen_urls: set[str] = set()
        for img in images:
            if img.get("url") and img["url"] not in seen_urls:
                local_path = self._download_image(img["url"])
                if local_path:
                    img["local_path"] = str(local_path)
                    verified.append(img)
                    seen_urls.add(img["url"])
            if len(verified) >= self._max_per_dest:
                break

        # Fallback queries if still short
        attempt = 0
        fallback_queries = self._fallback_queries(dest["name"])
        while len(verified) < self._min_per_dest and attempt < MAX_FALLBACK_ATTEMPTS:
            query = fallback_queries[attempt % len(fallback_queries)]
            logger.warning("  Image fallback attempt %d for '%s': '%s'", attempt + 1, dest["name"], query)
            extra = self._fetch_from_wikimedia(query, limit=4)
            for img in extra:
                if img.get("url") and img["url"] not in seen_urls:
                    local_path = self._download_image(img["url"])
                    if local_path:
                        img["local_path"] = str(local_path)
                        verified.append(img)
                        seen_urls.add(img["url"])
                if len(verified) >= self._max_per_dest:
                    break
            attempt += 1

        return verified[:self._max_per_dest]

    # ── NPS images ───────────────────────────────────────────────────────────

    def _fetch_from_nps(self, park_code: str) -> list[dict[str, Any]]:
        try:
            resp = self._session.get(
                f"{NPS_API_BASE}/multimedia/galleries/assets",
                params={"parkCode": park_code, "limit": 6},
                headers={"X-Api-Key": self._nps_key},
                timeout=10,
            )
            resp.raise_for_status()
            results = []
            for item in resp.json().get("data", []):
                url = item.get("fileInfo", {}).get("url", "")
                if url:
                    results.append({
                        "url": url,
                        "title": item.get("title", ""),
                        "credit": item.get("credit", "National Park Service"),
                        "license": "Public Domain / NPS",
                        "source": "nps",
                    })
            return results
        except requests.RequestException as exc:
            logger.warning("NPS image API error for '%s': %s", park_code, exc)
            return []

    # ── Wikimedia images ─────────────────────────────────────────────────────

    def _fetch_from_wikimedia(self, query: str, limit: int = 4) -> list[dict[str, Any]]:
        try:
            resp = self._session.get(
                WIKIMEDIA_SEARCH,
                params={
                    "action": "query",
                    "generator": "search",
                    "gsrnamespace": 6,
                    "gsrsearch": f"filetype:bitmap {query}",
                    "gsrlimit": limit * 3,
                    "prop": "imageinfo",
                    "iiprop": "url|extmetadata|size|mime",
                    "iiurlwidth": THUMB_WIDTH,
                    "format": "json",
                },
                timeout=10,
            )
            resp.raise_for_status()
            pages = resp.json().get("query", {}).get("pages", {})
            results = []
            for page in pages.values():
                info = page.get("imageinfo", [{}])[0]
                url = info.get("thumburl") or info.get("url", "")
                if not url:
                    continue
                mime = info.get("mime", "image/jpeg")
                if not mime.startswith("image/"):
                    continue
                meta = info.get("extmetadata", {})
                results.append({
                    "url": url,
                    "title": page.get("title", "").replace("File:", ""),
                    "credit": meta.get("Artist", {}).get("value", "Wikimedia Commons"),
                    "license": meta.get("LicenseShortName", {}).get("value", "CC BY-SA"),
                    "source": "wikimedia",
                })
            return results[:limit]
        except requests.RequestException as exc:
            logger.warning("Wikimedia search error for '%s': %s", query, exc)
            return []
        
        # ── Unsplash images ───────────────────────────────────────────────────────

    def _fetch_from_unsplash(self, query: str, limit: int = 4) -> list[dict[str, Any]]:
        key = os.environ.get("UNSPLASH_ACCESS_KEY")
        if not key:
            return []

        try:
            url = "https://api.unsplash.com/search/photos"
            params = {
                "query": query,
                "per_page": limit,
                "orientation": "landscape",
            }
            headers = {
                "Authorization": f"Client-ID {key}",
                "User-Agent": "RoadTripItineraryGenerator/1.0"
            }

            resp = self._session.get(url, params=params, headers=headers, timeout=10)
            resp.raise_for_status()

            results = []
            for item in resp.json().get("results", []):
                img_url = item.get("urls", {}).get("regular")
                if not img_url:
                    continue

                results.append({
                    "url": img_url,
                    "title": item.get("alt_description") or item.get("description") or "",
                    "credit": item.get("user", {}).get("name", "Unsplash"),
                    "license": "Unsplash License",
                    "source": "unsplash",
                })

            return results[:limit]

        except requests.RequestException as exc:
            logger.warning("Unsplash search error for '%s': %s", query, exc)
            return []


    # ── Download to local file ───────────────────────────────────────────────

    def _download_image(self, url: str) -> Path | None:
        url_hash = hashlib.md5(url.encode()).hexdigest()
        ext = self._guess_extension(url)
        local_path = self._output_dir / f"{url_hash}{ext}"
        if local_path.exists():
            return local_path
        try:
            resp = self._session.get(url, timeout=20, stream=True)
            resp.raise_for_status()
            with local_path.open("wb") as f:
                for chunk in resp.iter_content(chunk_size=65536):
                    f.write(chunk)
            time.sleep(REQUEST_DELAY)
            return local_path
        except requests.RequestException as exc:
            logger.warning("Image download failed (%s): %s", url[:60], exc)
            return None

    @staticmethod
    def _guess_extension(url: str) -> str:
        path = url.split("?")[0].split("#")[0]
        ext = Path(path).suffix.lower()
        return ext if ext in {".jpg", ".jpeg", ".png", ".webp", ".gif"} else ".jpg"

    @staticmethod
    def _fallback_queries(destination: str) -> list[str]:
        base = destination.split(",")[0].strip()
        return [
            f"{base} landscape",
            f"{base} mountains",
            f"{base} aerial view",
            f"{base} scenic",
        ]
