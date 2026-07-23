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
import hashlib, json, logging, mimetypes, os, threading, time
from concurrent.futures import ThreadPoolExecutor, as_completed
from io import BytesIO
from pathlib import Path
import re
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
    def __init__(
        self,
        config_path: str | Path = "config.yaml",
        output_dir: str | Path | None = None,
        force_refresh: bool = False,
    ) -> None:
        import yaml
        with Path(config_path).open() as f:
            cfg = yaml.safe_load(f)
        self._nps_key = os.environ.get("NPS_API_KEY", "DEMO_KEY")
        images_cfg = cfg.get("images", {})
        self._min_per_dest = images_cfg.get("min_per_destination", 2)
        self._max_per_dest = images_cfg.get("max_per_destination", 4)
        self._cache_ttl_seconds = int(images_cfg.get("cache_ttl_hours", 168)) * 3600
        self._force_refresh = force_refresh
        if output_dir is None:
            self._output_dir = Path("output/images")
        else:
            self._output_dir = Path(output_dir)
        self._output_dir.mkdir(parents=True, exist_ok=True)
        self._cache_dir = Path(".cache/images")
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        self._cache_index_path = self._cache_dir / "cache_index.json"
        self._cache_lock = threading.Lock()
        self._cache_index = self._load_cache_index()
        self._session_local = threading.local()

    def _get_session(self) -> requests.Session:
        if not hasattr(self._session_local, "session"):
            s = requests.Session()
            s.headers.update({"User-Agent": "RoadTripItineraryGenerator/1.0"})
            self._session_local.session = s
        return self._session_local.session

    # ── Public entry point ───────────────────────────────────────────────────

    def fetch_all(self, trip: dict[str, Any]) -> None:
        destinations = trip.get("destinations", [])

        def _fetch_one(dest: dict) -> None:
            logger.info("Fetching images for '%s'…", dest["name"])
            imgs = self._fetch_for_dest(dest)
            if len(imgs) < self._min_per_dest:
                raise RuntimeError(
                    f"Image fetch failed for '{dest['name']}': "
                    f"only {len(imgs)} image(s) verified (min: {self._min_per_dest})"
                )
            dest["images"] = imgs
            logger.info("  %d image(s) acquired for '%s'", len(imgs), dest["name"])

        with ThreadPoolExecutor(max_workers=min(len(destinations), 4)) as pool:
            futures = {pool.submit(_fetch_one, d): d for d in destinations}
            for f in as_completed(futures):
                f.result()

    # ── Per-destination fetch ────────────────────────────────────────────────

    def _fetch_for_dest(self, dest: dict[str, Any]) -> list[dict[str, Any]]:
        images: list[dict[str, Any]] = []
        dest_name = str(dest.get("name", "") or "")
        cache_key = self._cache_key(dest)

        if not self._force_refresh:
            cached_images = self._get_cached_images(cache_key)
            if cached_images:
                logger.info("  Reusing cached image candidates for '%s'", dest_name)
                verified_cached = self._verify_and_materialize(cached_images, dest_name)
                if len(verified_cached) >= self._min_per_dest:
                    return verified_cached[:self._max_per_dest]
                images.extend(cached_images)

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

        images = self._rank_images_for_destination(images, dest_name)
        verified = self._verify_and_materialize(images, dest_name)

        # Fallback queries if still short
        attempt = 0
        fallback_queries = self._fallback_queries(dest["name"])
        while len(verified) < self._min_per_dest and attempt < MAX_FALLBACK_ATTEMPTS:
            query = fallback_queries[attempt % len(fallback_queries)]
            logger.warning("  Image fallback attempt %d for '%s': '%s'", attempt + 1, dest["name"], query)
            extra = self._fetch_from_wikimedia(query, limit=4)
            extra = self._rank_images_for_destination(extra, dest_name)
            verified = self._verify_and_materialize(verified + extra, dest_name)
            attempt += 1

        if verified:
            self._set_cached_images(cache_key, verified)

        return verified[:self._max_per_dest]

    def _verify_and_materialize(self, images: list[dict[str, Any]], dest_name: str) -> list[dict[str, Any]]:
        ranked = self._rank_images_for_destination(images, dest_name)
        verified: list[dict[str, Any]] = []
        seen_urls: set[str] = set()
        for img in ranked:
            url = str(img.get("url", "") or "").strip()
            if not url or url in seen_urls:
                continue
            local_path = self._download_image(url)
            if not local_path:
                continue
            item = dict(img)
            item["local_path"] = str(local_path)
            verified.append(item)
            seen_urls.add(url)
            if len(verified) >= self._max_per_dest:
                break
        return verified

    def _cache_key(self, dest: dict[str, Any]) -> str:
        name = str(dest.get("name", "") or "").strip().lower()
        name = re.sub(r"\s+", " ", name)
        nps = str(dest.get("nps_park_code", "") or "none").strip().lower()
        return f"v1::{name}::{nps}"

    def _load_cache_index(self) -> dict[str, Any]:
        if not self._cache_index_path.exists():
            return {"version": 1, "entries": {}}
        try:
            payload = json.loads(self._cache_index_path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                return {"version": 1, "entries": {}}
            payload.setdefault("version", 1)
            payload.setdefault("entries", {})
            if not isinstance(payload["entries"], dict):
                payload["entries"] = {}
            return payload
        except Exception:
            logger.warning("Image cache index unreadable; rebuilding: %s", self._cache_index_path)
            return {"version": 1, "entries": {}}

    def _save_cache_index(self) -> None:
        tmp = self._cache_index_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._cache_index, indent=2), encoding="utf-8")
        tmp.replace(self._cache_index_path)

    def _get_cached_images(self, cache_key: str) -> list[dict[str, Any]]:
        with self._cache_lock:
            entry = self._cache_index.get("entries", {}).get(cache_key)
        if not entry or not isinstance(entry, dict):
            return []
        updated_at = float(entry.get("updated_at", 0) or 0)
        if time.time() - updated_at > self._cache_ttl_seconds:
            return []
        images = entry.get("images", [])
        if not isinstance(images, list):
            return []
        out: list[dict[str, Any]] = []
        for item in images:
            if not isinstance(item, dict):
                continue
            if not item.get("url"):
                continue
            out.append(dict(item))
        return out

    def _set_cached_images(self, cache_key: str, images: list[dict[str, Any]]) -> None:
        slim: list[dict[str, Any]] = []
        for img in images:
            if not isinstance(img, dict):
                continue
            url = str(img.get("url", "") or "").strip()
            if not url:
                continue
            slim.append(
                {
                    "url": url,
                    "title": str(img.get("title", "") or ""),
                    "credit": str(img.get("credit", "") or ""),
                    "license": str(img.get("license", "") or ""),
                    "source": str(img.get("source", "") or ""),
                }
            )
        if not slim:
            return
        with self._cache_lock:
            self._cache_index.setdefault("entries", {})[cache_key] = {
                "updated_at": time.time(),
                "images": slim,
            }
            self._save_cache_index()

    def _rank_images_for_destination(self, images: list[dict[str, Any]], destination: str) -> list[dict[str, Any]]:
        tokens = self._location_tokens(destination)
        if not tokens:
            return images

        profile = self._destination_image_profile(destination)

        def score(img: dict[str, Any]) -> int:
            hay = " ".join(
                [
                    str(img.get("title", "") or ""),
                    str(img.get("credit", "") or ""),
                    str(img.get("url", "") or ""),
                ]
            ).lower()
            base = sum(1 for t in tokens if t in hay)

            # Strongly penalize context mismatch (e.g., coral/ocean photos for desert parks).
            neg = sum(1 for t in profile["negative"] if t in hay)
            pos = sum(1 for t in profile["positive"] if t in hay)

            return base + (2 * pos) - (3 * neg)

        def neg_hits(img: dict[str, Any]) -> int:
            hay = " ".join(
                [
                    str(img.get("title", "") or ""),
                    str(img.get("credit", "") or ""),
                    str(img.get("url", "") or ""),
                ]
            ).lower()
            return sum(1 for t in profile["negative"] if t in hay)

        scored = sorted(images, key=score, reverse=True)
        non_negative = [img for img in scored if neg_hits(img) == 0]
        if non_negative:
            scored = non_negative
        # Keep only relevant images when possible; if none score above zero, keep original order.
        positive = [img for img in scored if score(img) > 0]
        return positive if positive else scored

    @staticmethod
    def _destination_image_profile(destination: str) -> dict[str, set[str]]:
        d = (destination or "").lower()

        positive: set[str] = {
            "landscape",
            "mountain",
            "canyon",
            "plateau",
            "mesa",
            "desert",
            "trail",
            "hiking",
            "sandstone",
            "cliff",
            "rock",
            "national park",
        }
        negative: set[str] = set()

        # For inland and canyon/desert contexts, marine imagery is usually a mismatch.
        inland_cues = (
            "national park",
            "state park",
            "desert",
            "canyon",
            "mesa",
            "plateau",
            "utah",
            "arizona",
            "nevada",
            "new mexico",
            "colorado",
        )
        if any(cue in d for cue in inland_cues):
            negative.update(
                {
                    "coral",
                    "underwater",
                    "scuba",
                    "snorkel",
                    "snorkeling",
                    "ocean",
                    "sea",
                    "tropical",
                    "reef fish",
                    "marine",
                    "wildlife",
                    "bird",
                    "rodent",
                    "marmot",
                    "chipmunk",
                    "squirrel",
                    "weasel",
                    "animal portrait",
                }
            )

        # Specific guard for Capitol Reef ambiguity with ocean reef photos.
        if "capitol reef" in d or "capital reef" in d:
            negative.update({"coral", "underwater", "ocean", "sea", "scuba", "snorkel"})
            positive.update({"capitol reef", "waterpocket fold", "utah", "sandstone"})

        return {"positive": positive, "negative": negative}

    @staticmethod
    def _location_tokens(destination: str) -> list[str]:
        parts = re.findall(r"[a-z0-9]+", (destination or "").lower())
        stop = {"national", "park", "state", "the", "and", "city"}
        tokens = [p for p in parts if len(p) >= 4 and p not in stop]
        # Add canonical typo resilience for common park names.
        expanded = set(tokens)
        if "kolob" in expanded:
            expanded.add("kolb")
        return sorted(expanded)

    # ── NPS images ───────────────────────────────────────────────────────────

    def _fetch_from_nps(self, park_code: str) -> list[dict[str, Any]]:
        try:
            resp = self._get_session().get(
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
            resp = self._get_session().get(
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

            resp = self._get_session().get(url, params=params, headers=headers, timeout=10)
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
            resp = self._get_session().get(url, timeout=20, stream=True)
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
