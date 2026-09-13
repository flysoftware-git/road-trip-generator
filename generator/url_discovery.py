"""
url_discovery.py — Per-item URL discovery via Grok semantic search.

AI NEVER generates URLs. This module discovers URLs for every named
attraction, restaurant, scenic drive, and en-route stop after AI
content generation is complete.

Two-pass restaurant strategy:
  Pass 1: Google Maps domain filter (top-rated, accurate hours)
  Pass 2: TripAdvisor domain filter (local favorites, cuisine diversity)

Search API history:
  v1.0: Bing Search API v7 (retired August 11, 2025)
  v1.1: Google Custom Search (deprecated full-web search, unusable)
  v1.2: Brave Search API (retired in favour of Azure AI Services)
  v1.3: Bing Web Search API (deprecated, limited availability)
  v1.4: Google Programmable Search Engine (rate-limited, prohibitive costs)
  v1.5: xAI Grok semantic search via chat-completions + live_search
        (superseded 2026-08-14: live_search tool deprecated, returns 410)
  v1.6: xAI Grok via /v1/responses + web_search tool, SSE streaming
        (current default). The search provider is pluggable -- see
        generator/search_provider.py for Claude/OpenAI alternatives.
"""
from __future__ import annotations
import html as html_lib
import json
import logging
import os
import time
from html.parser import HTMLParser
from pathlib import Path
import re
from math import asin, ceil, cos, radians, sqrt
from urllib.parse import parse_qs, quote, unquote, urlparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import threading
from threading import Lock
from typing import Any
from generator.llm_client import MultiLLMClient
from generator.fanout_metrics import pool as instrumented_pool
from generator.transit_routing import NON_DRIVING_BOOKED_TYPES, leg_mode
from generator.road_estimate import (
    ROAD_DISTANCE_FACTOR,
    drive_minutes,
    format_drive_time,
)
from generator.place_resolver import PlaceResolutionRefused, PlaceResolver
from generator.multi_site_grouping import DEFAULT_BASE_OWNED_CATEGORIES, category_deferred_to_base
from generator.search_provider import build_search_client
from generator.url_validator import URLValidator

logger = logging.getLogger(__name__)
MAX_FALLBACK_ATTEMPTS = 4
ALLTRAILS_404_MARKERS = (
    "we've reached the end of the trail",
    "the page you're looking for either doesn't exist",
)
ALLTRAILS_CLOSURE_MARKERS = (
    "this trail is closed",
    "trail is closed",
    "temporarily closed",
    "closed due to",
    "trail closure",
)
ATTRACTION_CLOSURE_MARKERS = (
    "currently closed",
    "closed for safety reasons",
    "closed for safety",
    "temporarily closed",
    "closed due to",
    "permanently closed",
    "this location is closed",
    "this place is closed",
    "this business is closed",
)
# Real bug (Bryce Canyon eval run): "Bryce Canyon Visitor Center" linked to
# https://www.nps.gov/brca/planyourvisit/visitorcenters.htm, which passes
# every existing liveness/relevance/genericness check (200 status, on the
# right domain, mentions the destination and "visitor center") yet actually
# renders nothing but "Page In-Progress -- This page is currently being
# worked on. Please check back later." NPS restructures its site often
# enough that a stub/placeholder page like this can sit at a perfectly
# plausible-looking URL indefinitely. None of the existing checks catch this
# class of page because they all reason about topic/entity relevance or
# closure status, not "does this page actually contain any real content at
# all" -- a placeholder is topically on-topic (it IS meant to be the visitor
# center page, eventually) and not "closed", just empty.
UNDER_CONSTRUCTION_PAGE_MARKERS = (
    "page in-progress",
    "page in progress",
    "this page is currently being worked on",
    "this page is under construction",
    "page under construction",
    "site under construction",
    "we are currently updating this page",
    "this content is currently unavailable",
    # Deliberately excludes generic phrases like "coming soon" -- too likely
    # to appear in passing on an otherwise substantive page (e.g. "new
    # exhibit coming soon") to serve as a reliable whole-page placeholder
    # signal on their own.
)
# A closure marker phrase found on the same page as a mention of one of
# these sub-part nouns is a signal the closure is scoped to a part of the
# venue (a wing, an exhibit, a specific gallery) rather than the whole
# attraction -- e.g. a real, live "Museum of International Folk Art" page
# describing "The Girard Wing will be temporarily closed ... for roof
# repair" while the rest of the museum operates normally. See
# _has_attraction_closure_marker for how this is applied (same-sentence
# co-occurrence with a closure marker, not a whole-page check).
ATTRACTION_PARTIAL_CLOSURE_SCOPE_MARKERS = (
    "wing",
    "gallery",
    "galleries",
    "exhibit",
    "exhibition",
    "annex",
    "hall",
    "room",
    "section",
    "one of the",
    "a portion of",
    "part of the",
    "area of the",
)
_HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
_HTML_SCRIPT_STYLE_RE = re.compile(r"<(script|style)\b[^>]*>.*?</\1>", re.DOTALL | re.IGNORECASE)


def _strip_non_visible_html_noise(text: str) -> str:
    """Strip HTML comments and <script>/<style> tag contents before running
    text-based content checks (e.g. closure-marker detection) against raw,
    unstripped page HTML (see URLValidator.get_text, which returns
    resp.text as-is). These regions are never visible to a real site
    visitor and can carry stale or unrelated text -- e.g. a leftover
    developer comment mentioning an old closure -- that shouldn't be
    treated as live page content.
    """
    if not text:
        return text
    stripped = _HTML_SCRIPT_STYLE_RE.sub(" ", text)
    stripped = _HTML_COMMENT_RE.sub(" ", stripped)
    return stripped
# How much of a fetched page body the campground filter reads. A page that is
# ABOUT a campground says so in its title and opening; a park page mentions the
# campground it happens to contain much further down. See
# _is_campground_focused_result_for_noncamping_item.
CAMPGROUND_TEXT_SCAN_CHARS = 2000

GENERIC_BAD_URL_MARKERS = (
    "404errorpage",
    "/assetdetail/",
    "/assetdetail",
    "/404",
    "/things2do",
    "/things-to-do",
    "/plan-your-visit",
    "/planyourvisit",
    "/explore",
    "/about",
    "/index.htm",
    "/index.html",
    # A news article, press release, or incident report is a dated,
    # non-durable page about an EVENT -- never a stable entity-specific
    # reference page for an attraction/trail/landmark, even when it's live,
    # on the right domain, and happens to mention the item's name (real
    # example: an nps.gov "seeks public assistance in locating missing
    # hiker" news post got accepted as "Chimney Rock"'s link because it's a
    # genuine, live, on-domain page that mentions Capitol Reef -- the
    # existing relevance checks have no notion of page TYPE, only token
    # overlap).
    "/learn/news/",
    "/news/",
    "/press-release",
    "/pressrelease",
)
# Titles/names that identify a search-result or listing page rather than a
# specific named place -- e.g. "THE 10 BEST Restaurants in St. George -
# Tripadvisor" or "Things to Do in Moab 2024". Harvested rows carrying one of
# these as their only "name" must never be used to synthesize a new
# attraction/restaurant item: both the listing title and its accompanying
# listing-page URL get attached together, producing a card whose displayed
# name is a listicle headline instead of an actual place.
GENERIC_LISTING_TITLE_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"^\s*(the\s+)?\d+\s+best\b",
        r"\bbest\s+restaurants?\s+(in|near)\b",
        r"\bthings\s+to\s+do\s+in\b",
        r"\btop\s+\d+\s+(restaurants|things\s+to\s+do|attractions|places)\b",
        r"[-|]\s*(tripadvisor|trip\s*advisor|yelp)\s*$",
    )
)
SAFE_FALLBACK_URL_PREFIXES = (
    "https://www.google.com/maps/search/",
    "https://www.google.com/maps/dir/",
    # A place_id link (generator/place_resolver.py) belongs here for the same
    # reason as the two above and more strongly: it names one specific place
    # rather than describing it in words, so it always resolves and spending an
    # HTTP validation request on it is pure waste.
    "https://www.google.com/maps/place/",
    "https://www.google.com/search",
)
POSITIVE_DOMAIN_HINTS = (
    "visit",
    "explore",
    "travel",
    "tourism",
    "tourisme",
    "turismo",
    "turizam",
    "turist",
    "arts",
    "culture",
    "gallery",
    "museum",
)
NEGATIVE_DOMAIN_HINTS = (
    "preserve",
    "wildlife",
    "nature",
    "conservation",
    "hospital",
    "government",
    "council",
    "realestate",
)
POSITIVE_PATH_HINTS = (
    "/arts/",
    "/culture/",
    "/gallery/",
    "/district/",
    "/visit/",
    "/explore/",
)
COUNTRY_TLD_HINTS: dict[str, tuple[str, ...]] = {
    "croatia": ("hr",),
    "italy": ("it",),
    "france": ("fr",),
    "spain": ("es",),
    "portugal": ("pt",),
    "germany": ("de",),
    "austria": ("at",),
    "switzerland": ("ch",),
    "netherlands": ("nl",),
    "belgium": ("be",),
    "czech": ("cz",),
    "czechia": ("cz",),
    "hungary": ("hu",),
    "slovenia": ("si",),
    "slovakia": ("sk",),
    "poland": ("pl",),
    "greece": ("gr",),
    "turkey": ("tr",),
    "norway": ("no",),
    "sweden": ("se",),
    "denmark": ("dk",),
    "finland": ("fi",),
    "ireland": ("ie",),
    "united kingdom": ("uk", "co.uk"),
    "england": ("uk", "co.uk"),
    "scotland": ("uk", "co.uk"),
    "wales": ("uk", "co.uk"),
    "serbia": ("rs",),
    "bosnia": ("ba",),
    "montenegro": ("me",),
    "albania": ("al",),
    "romania": ("ro",),
    "bulgaria": ("bg",),
}
LOCATION_CUE_TERMS = (
    "national park",
    "state park",
    "downtown",
    "historic district",
    "visitor center",
    "junction",
    "utah",
    "colorado",
    "arizona",
    "new mexico",
    "nevada",
    "california",
)
TEXT_URL_RE = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)


class _DirectBatchHTMLListParser(HTMLParser):
    """Parse <li> blocks from HTML payloads and extract item names plus href links."""

    def __init__(self) -> None:
        super().__init__()
        self.records: list[dict[str, Any]] = []
        self._in_li = False
        self._li_text_parts: list[str] = []
        self._li_urls: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        t = str(tag or "").lower()
        if t == "li":
            self._in_li = True
            self._li_text_parts = []
            self._li_urls = []
            return
        if t == "a" and self._in_li:
            for key, value in attrs:
                if str(key or "").lower() != "href":
                    continue
                href = str(value or "").strip()
                if href:
                    self._li_urls.append(href)

    def handle_data(self, data: str) -> None:
        if not self._in_li:
            return
        text = str(data or "").strip()
        if text:
            self._li_text_parts.append(text)

    def handle_endtag(self, tag: str) -> None:
        if str(tag or "").lower() != "li" or not self._in_li:
            return
        raw_text = " ".join(self._li_text_parts).strip()
        name = re.sub(r"\s+", " ", raw_text)
        name = re.sub(r"\bsource\b.*$", "", name, flags=re.IGNORECASE).strip()
        name = re.sub(r"\balltrails\b.*$", "", name, flags=re.IGNORECASE).strip()
        name = re.sub(r"^(?:google\s+maps|maps?|map)\s*:\s*", "", name, flags=re.IGNORECASE).strip()
        name = re.sub(r"^(?:source|official\s+site)\s*:\s*", "", name, flags=re.IGNORECASE).strip()
        name = re.sub(r"(?:\s+(?:source|maps?|alltrails))+\s*$", "", name, flags=re.IGNORECASE).strip()
        if not name and raw_text:
            name = raw_text
        self.records.append({"name": name, "urls": list(self._li_urls), "raw_text": raw_text})
        self._in_li = False
        self._li_text_parts = []
        self._li_urls = []

# ── URL Search Cache ────────────────────────────────────────────────────
_url_cache: dict[tuple[str, str, str], str | None] = {}

DEFAULT_UNINTERESTED_ATTRACTION_KEYWORDS = (
    "golf course",
    "country club",
    "golf club",
    " golf ",
    "bike trail",
    "bicycle trail",
    "cycling trail",
)
DEFAULT_SKI_ATTRACTION_KEYWORDS = (
    "ski",
    "ski-",
    "snowboard",
    "snowboarding",
    "chairlift",
)
DEFAULT_SKI_IN_SEASON_MONTHS = (11, 12, 1, 2, 3, 4)
DEFAULT_MAX_TRAIL_MILES = 3.0
DEFAULT_ALLOW_BLOCKED_ALLTRAILS = True
DEFAULT_ALLTRAILS_MIN_CONFIDENCE_FOR_PUBLISH = "high"
DEFAULT_ALLTRAILS_REQUEST_DELAY_SECONDS = 0.8
DEFAULT_ALLTRAILS_BLOCK_COOLDOWN_SECONDS = 8.0
# Generic per-domain equivalent of the AllTrails-specific cooldown above,
# applied to any domain (TripAdvisor and beyond) that returns a 401/403 to a
# generic page-text fetch -- avoids re-probing a domain that just blocked us
# for every subsequent distinct URL on that same domain.
DEFAULT_DOMAIN_BLOCK_COOLDOWN_SECONDS = 8.0
# The three states a published link can be in, and why there are three rather
# than two.
#
# The publication gate is fail-closed: a URL that is *definitively* dead
# (404/410, or a host that does not resolve) is stripped before assembly. What
# survives the gate carries a single bit -- it shipped -- and that bit
# conflates two very different situations. A link can ship because it was
# fetched and answered, or it can ship because nothing ever managed to ask it.
# Bot-blocking makes the second case ordinary rather than exotic: on a guide
# measured while writing this, 34 of 115 published links (29.6%) sat on domains
# that answer an automated fetch with a block page, a connection reset, or
# nothing at all, so the gate passed them having learned nothing about them.
#
# The two cases are indistinguishable in the output, and the difference is
# exactly what a reader of a fail-closed guarantee wants to know. So liveness
# is recorded as a tri-state:
#
#   live      -- a fetch succeeded; the link answered
#   dead      -- a fetch returned a definitively-dead verdict that the
#                bot-block carve-out did not withdraw (the state the gate acts on)
#   unchecked -- everything else: never fetched, fetched and blocked, timed
#                out, skipped by a domain cooldown, refused at the TCP level
#
# `unchecked` is a statement about the instrument, not about the URL. It says
# the pipeline does not know -- which is honest, and is not the same as "fine".
LINK_LIVENESS_LIVE = "live"
LINK_LIVENESS_DEAD = "dead"
LINK_LIVENESS_UNCHECKED = "unchecked"
# Precedence when one URL is observed more than once in a run. Fail-closed
# ordering, and order-independent by construction: a dead observation is never
# laundered by a later live one, and a live observation always beats "we never
# found out".
LINK_LIVENESS_PRECEDENCE = {
    LINK_LIVENESS_UNCHECKED: 0,
    LINK_LIVENESS_LIVE: 1,
    LINK_LIVENESS_DEAD: 2,
}
# Where a URL redirected to, recorded in `_fetch_final_url_cache`, has the same
# shape of problem the liveness ledger above was built for, one level down.
# `_redirect_target_lacks_item_relevance` rejects a candidate on the strength of
# that record, so the record has to be able to say which of three things it
# means:
#
#   redirected     -- a fetch followed the URL somewhere else; the target is
#                     the value, and the gate may judge it
#   not_redirected -- a fetch resolved this URL to itself. A measured fact
#                     about the URL, and the only one of the three that the
#                     gate can safely read as "there is nothing to judge"
#   unchecked      -- nothing ever looked, or the fetch that looked never
#                     reported a final URL (a request that raised, a validator
#                     that does not expose one). A statement about the
#                     instrument, exactly as in the liveness ledger
#
# Encoding: absence means `unchecked`; a value equal to the key means
# `not_redirected`; any other value is the redirect target. The value of an
# entry is therefore always the final URL, which is what every existing reader
# already assumed it was -- `.get(url, url)` at the call sites below spells the
# same convention out. It also needs no new field on disk: entries written
# before this change carry `final_url: ""` for BOTH "no redirect" and "never
# looked", and an empty string is not a URL, so they load as `unchecked` and
# assert nothing nobody measured.
LINK_REDIRECT_FOLLOWED = "redirected"
LINK_REDIRECT_NONE = "not_redirected"
LINK_REDIRECT_UNCHECKED = "unchecked"
# Wayback Machine fallback for AllTrails geo extraction (see
# _fetch_wayback_alltrails_text): a direct AllTrails fetch is bot-blocked by
# DataDome essentially universally in production (see _fetch_alltrails_text's
# own comment), but archive.org's crawler is a different requester on a
# different domain that AllTrails' bot-detection has no reason to block, and
# it stores the ORIGINAL page HTML (including JSON-LD) at crawl time. A
# trailhead coordinate doesn't change even in a years-old snapshot, unlike
# ratings/hours/closures, so this fallback is safe even when the archived
# page is stale. Delay is deliberately gentle -- archive.org is a shared
# nonprofit resource, not a target to hammer, and this path only fires when
# the (already-throttled) direct AllTrails fetch has already failed.
DEFAULT_WAYBACK_REQUEST_DELAY_SECONDS = 1.0
# Route distance/time already has a solid Haversine-estimate fallback that
# costs zero network calls -- the live Google Maps directions HTML scrape is
# a pure accuracy enhancement on top of it, not a correctness gate, and (like
# any scrape) is a real future bot-block risk. Default keeps current
# fetch-then-fallback behavior; set False to skip the live fetch entirely.
DEFAULT_ROUTE_DISTANCE_LIVE_FETCH_ENABLED = True
# _search_cached previously cached an empty search result permanently for the
# run, with no distinction between "genuinely no results" and "the request
# failed" (GrokSearch.search() swallows exceptions and returns [] either
# way). A single transient failure would poison that query for the rest of
# the run with zero chance of recovery. This cooldown makes an empty result
# "sticky" for a bounded window (so a real outage doesn't cause repeated
# re-probing) but still allows a fresh attempt once it expires.
DEFAULT_SEARCH_FAILURE_COOLDOWN_SECONDS = 180.0
DEFAULT_ENABLE_FILTERED_ALLTRAILS_SELECTION = False
DEFAULT_ALLTRAILS_FILTER_MAX_MILES = 3.0
DEFAULT_ALLTRAILS_FILTER_MAX_GAIN_FEET = 300
DEFAULT_ALLTRAILS_FILTER_MIN_REVIEWS = 5
DEFAULT_ALLTRAILS_FILTER_ALLOWED_DIFFICULTIES = ("easy", "moderate", "moderately challenging")
DEFAULT_ALLTRAILS_RATING_MIN = 4.5
DEFAULT_ALLTRAILS_RATING_MIN_VOTES = 200
DEFAULT_ALLTRAILS_RATING_BOOST = 8
DEFAULT_RESTAURANT_RATING_MIN = 4.4
DEFAULT_RESTAURANT_RATING_MIN_VOTES = 100
DEFAULT_RESTAURANT_PREFER_OFFICIAL_SITE_OVER_TRIPADVISOR = True
DEFAULT_RESTAURANT_RATING_BOOST = 6
DEFAULT_PLACE_INTEREST_MIN_RATING = 4.0
DEFAULT_PLACE_INTEREST_MIN_VOTES = 10
DEFAULT_PLACE_INTEREST_REQUIRE_METADATA = False
DEFAULT_RESTAURANT_NAME_DENYLIST: tuple[str, ...] = ()
RESTAURANT_CLOSURE_MARKERS: tuple[str, ...] = (
    "permanently closed",
    "this business is permanently closed",
    "closed permanently",
    "no longer in business",
    "this location is closed",
    "this restaurant is closed",
    "this place is closed",
)
RESTAURANT_PRE_OPENING_MARKERS: tuple[str, ...] = (
    "opening soon",
    "coming soon",
    "not yet open",
    "grand opening coming",
)
DEFAULT_URL_POLICY_MODE = "monitor"
DEFAULT_ALLTRAILS_SLUG_DENYLIST: tuple[str, ...] = (
    "dixie-sugarloaf-trail",
)
DEFAULT_ALLTRAILS_SOURCE = "direct_link_batch"
DEFAULT_ATTRACTION_SOURCE = "direct_link_batch"
DEFAULT_RESTAURANT_SOURCE = "direct_link_batch"
DEFAULT_EN_ROUTE_SOURCE = "search"
# "search" = today's paid per-item fallback. "geocode_maps" replaces it
# with a free geocode-backed coordinate Maps link. See _search_first.
#: Word-boundary "trail"/"trails". Built with chr(92) because writing the
#: escape inline through a patch script silently produced a backspace
#: character instead, giving a pattern that matched nothing -- including
#: real trails -- while reading correctly in the source.
TRAIL_NAME_PATTERN = chr(92) + "btrails?" + chr(92) + "b"

#: Which check in _retain_discovered_url refused a URL. The function has 30
#: rejection exits and recorded none of them, so every conclusion about why
#: a link was dropped had to be inferred from outside -- three separate
#: fixes for Upheaval Dome each targeted a gate that turned out not to be
#: the one firing. Labels are the guarding condition, captured from source.
_RETENTION_EXIT_LABELS = {
    1: 'if not url',
    2: 'if self._is_url_domain_denied(url)',
    3: 'if self._is_obviously_generic_url(lower)',
    4: "if self._is_alltrails_trail_url(url) and bool(getattr(self, '_disable_trails', False))",
    5: 'if not allow_alltrails and self._is_alltrails_trail_url(url)',
    6: 'if self._has_unescaped_whitespace(url)',
    7: 'if self._is_incomplete_google_maps_place_url(url)',
    8: 'if self._looks_synthetic_google_maps_place_url(url)',
    9: 'if not ok or not page_html',
    10: 'if max(page_overlap, label_overlap) < required_overlap',
    11: 'if anchor_overlap < 1',
    12: "if leniency_policy_class in leniency_blocked_classes and leniency_policy_mode == 'enforce'",
    13: 'if not ok and self._is_definitively_dead_status(fetch_status) and (not self._is_bot_block_false_negative_dead_status(url',
    14: 'if redirect_target',
    15: 'if self._is_google_maps_candidate_url(url)',
    16: 'if self._is_generic_restaurant_landing_url(url, item_name, dest_name, item_tokens=_rest_tokens)',
    17: 'if not self._looks_like_item_specific_homepage(url, item_name)',
    18: "if slug in getattr(self, '_alltrails_slug_denylist', frozenset())",
    19: 'if item_tokens and (not any((t in wiki_slug for t in item_tokens)))',
    20: 'if distinctive_item_tokens and (not any((t in wiki_slug for t in distinctive_item_tokens)))',
    21: 'if self._is_generic_geographic_url_for_category(url, item_name)',
    22: 'if self._is_category_offer_listing_url(url)',
    23: "if kind == 'scenic drive' and (not self._is_route_specific_scenic_drive_url(url))",
    24: 'if not self._alltrails_url_meets_seed_relaxed_standard(url, item_name)',
    25: 'if not self._meets_alltrails_publish_confidence(url, item_name, dest_name)',
    26: 'if not self._passes_alltrails_post_search_filters(url, item_name, dest_name)',
    27: 'if not ok and self._is_definitively_dead_status(status) and (not self._is_bot_block_false_negative_dead_status(url, stat',
    28: 'if redirect_target',
    29: 'if not self._is_relevant_result(url, item_name, dest_name, candidate=candidate, deep_check=deep_check, item_description=',
    30: "if allow_google_maps_search and policy_class in {'google_maps_search', 'google_maps_dir'}",
    31: "if kind in {'generic', 'attraction'} and self._is_the_destinations_own_page(url, item_name, dest_name)",
}

DEFAULT_FALLBACK_MODE = "search"
DEFAULT_DIRECT_LINK_BATCH_COUNT = 20
DEFAULT_DIRECT_BATCH_AUTHORITATIVE = True
# An item the authoritative batch cannot place still gets one per-item search.
# See _item_fallback_when_batch_silent_enabled.
DEFAULT_ITEM_FALLBACK_WHEN_BATCH_SILENT = True
DEFAULT_RESTAURANT_DIRECT_BATCH_ITEM_COUNT = 4
DEFAULT_EN_ROUTE_DIRECT_BATCH_ITEM_COUNT = 4
DEFAULT_ATTRACTION_DIRECT_BATCH_ITEMS_PER_DAY = 3
DEFAULT_TRAIL_DIRECT_BATCH_ITEMS_PER_DAY = 2
# Direct-batch destination grouping (2026-08-15): how many destinations'
# worth of a given kind (attraction/restaurant/trail) get asked for in a
# single direct-batch harvest call, instead of one call per destination.
# Real-call evidence: 2 destinations in one call completed in ~90s (in line
# with a single destination's own 68-150s range) with correct, cleanly
# separated results; 3 destinations never converged across 2 full retry
# attempts (300s+, zero output). 1 disables grouping entirely (the original
# one-call-per-destination behavior) -- the safe rollback value. Deliberately
# NOT raised above 2 by default until more evidence justifies it; the knob
# exists so that can happen later without further code changes.
DEFAULT_DIRECT_BATCH_GROUP_SIZE = 2
# Generic descriptive suffixes AI-generated attraction names often append to a
# place name (e.g. harvest row "Bryce Point" vs AI-generated item name "Bryce
# Point Overlook") that carry no matching-relevant meaning of their own.
GENERIC_VIEWPOINT_SUFFIX_TOKENS = frozenset({"overlook", "view", "viewpoint", "vista"})
_MATCH_STOPWORDS = frozenset({"the", "a", "an", "of", "and", "st"})
# Landscape/geographic descriptor words that are too common within a single
# region to serve as the SOLE distinguishing word for the weak single-
# anchor-token match tier (strength 1) in _direct_batch_row_match_strength /
# _direct_batch_url_matches_item below -- e.g. "canyon" appears in countless
# unrelated named places across Utah canyon country ("Snow Canyon", "Zion
# Canyon", "Bryce Canyon", "Red Canyon", ...). Real dipstick67 bug: the seed
# attraction "Jenny's Canyon Trail" (St. George) was authoritatively linked
# to an unrelated direct-batch row for "Entrada at Snow Canyon Golf Course"
# purely because both share the single generic word "canyon" -- the golf
# course row had no other token overlap with the trail's name at all. This
# mirrors the earlier "scenic"/"byway" boilerplate-overlap fix, except
# "canyon" still legitimately contributes toward the *full* required-
# overlap match tiers (2/3) above -- e.g. "Zion Canyon Scenic Drive" vs
# "Zion Canyon Visitor Center" genuinely share both "zion" and "canyon" --
# so it is only excluded from acting alone as the weak-tier anchor, not
# stripped from token matching altogether (unlike "scenic"/"byway", which
# are excluded from _significant_tokens entirely because they never carry
# real identifying meaning even combined with other words).
_GENERIC_ANCHOR_EXCLUDED_TOKENS = frozenset({"canyon"})
DEFAULT_RESTAURANT_DIRECT_BATCH_MIN_RESULTS = 3
DEFAULT_EN_ROUTE_DIRECT_BATCH_MIN_RESULTS = 2
DEFAULT_EN_ROUTE_DETOUR_MAX_MINUTES = 20
# Was 0.0 -- see the long comment above _en_route_stop_within_threshold's
# definition for the real-world consequence this had (root-caused via a
# real published run, Telluride -> Pagosa leg): with this at 0.0, the
# `max_miles > 0 and miles > max_miles` guard in that method was always
# False, so a text-mined/harvested detour_distance_miles value -- however
# large -- could never reject a candidate on distance alone. In practice
# this mattered a lot: many harvested stop descriptions state a detour
# distance in miles without also stating a time in minutes (the existing
# 20-minute cap only fires when `minutes` was actually mined), so distance
# was frequently the *only* signal available, and it was silently inert.
# Real casualties from that leg's "Can't-Miss Enroute" list: Dolores River
# Overlook (49.4 mi), Durango & Silverton Narrow Gauge Railroad Depot
# (87.3 mi), Animas River Trail (93.2 mi), Mancos State Park (112.9 mi) --
# all published as "can't-miss" quick side-trips despite being 2+ hours
# each way off a highway leg.
# 20.0 miles is chosen to pair with the existing 20-minute cap rather than
# introduce an unrelated magnitude, and is defensible under either reading
# of this module's documented, unresolved one-way/round-trip/loop ambiguity
# for what the mined number even means (see the NOTE above
# _extract_en_route_detour_minutes_from_text): a 20 mi *round-trip* detour
# is roughly 20-25 min at a realistic rural-highway-adjacent speed --
# solidly "quick"; even read as *one-way* (40 mi round trip), it is still
# under an hour added, matching the project owner's own bar for a
# "can't-miss" side-trip. Either reading comfortably rejects all four real
# examples above (49.4-112.9 mi), which is what this fix exists to do.
# Seeded stops (manifest `en_route_seeds`) are exempt from this cap via
# `_en_route_stop_within_threshold`'s `seed_threshold_override` -- they
# still have to clear `_prune_en_route_stops_by_geometry`'s real geocoding/
# route-proximity checks, just not this distance heuristic.
DEFAULT_EN_ROUTE_DETOUR_MAX_MILES = 20.0
DEFAULT_EN_ROUTE_REQUIRE_DETOUR_METADATA = True
# Shared with _en_route_stop_geometry_grounded_detour_floor and
# _resolve_en_route_stop_detour_metrics_against_geometry: a generous
# highway-speed ceiling (rural Utah interstate limit is 80 mph; 70 leaves
# margin for the slower stretches most detours actually run on). Used both
# as a per-dimension floor (a round trip can never be quicker than this
# implies) and, in the latter, to catch a text-mined miles/minutes *pair*
# that independently clears both dimensions' floors but is still mutually
# implausible together -- e.g. "22 mi in 15 min" (88 mph).
MAX_PLAUSIBLE_EN_ROUTE_DETOUR_MPH = 70.0
DEFAULT_DIRECT_BATCH_HTML_CAPTURE_ENABLED = True
DEFAULT_DIRECT_BATCH_HTML_CAPTURE_SUBDIR = "dev/url_discovery_direct_batch_html"
DEFAULT_URL_POLICY_BLOCKED_CLASSES = (
    "google_search",
    "google_maps_search",
    "google_maps_dir",
)
DEFAULT_URL_DOMAIN_DENYLIST: tuple[str, ...] = ()
DEFAULT_URL_POLICY_ALLOWLIST_PATH = "docs/policy/url_policy_allowlist.txt"
DEFAULT_URL_POLICY_AUTO_ALLOW_FROM_OUTPUT = True
DEFAULT_URL_POLICY_OUTPUT_PATH = "output/index.html"
DEFAULT_PERSISTENT_CACHE_ENABLED = True
DEFAULT_PERSISTENT_CACHE_PATH = ".cache/url_discovery/persistent_cache.json"
# Cost-audit finding (see docs/design/url-discovery-and-audit.md "Search-Result
# Cache Audit"): raised from 72h to 168h (7 days). A cached search result only
# answers "what URL does this query currently resolve to" -- a real place's
# authoritative page essentially never changes week to week. Whether that page
# is still LIVE is a completely separate question, re-checked independently on
# much shorter TTLs regardless of this one: _verify_url_cached (12h, see
# DEFAULT_PERSISTENT_VERIFY_CACHE_TTL_HOURS) and page-text/AllTrails fetches
# (24h/12h below). So extending this TTL cannot let a closure or dead link go
# undetected for longer than those already do -- it only avoids re-asking the
# same "which URL is this" question inside the same week, which is exactly the
# repeat-run cost driver this audit was chasing (AllTrails corroboration
# searches alone were ~30% of one real run's Grok calls). This mirrors the
# geocode/Wayback caches below, which already use a long TTL (720h) for the
# same reason: querying a genuinely static fact more than once a week is pure
# waste, not freshness.
DEFAULT_PERSISTENT_SEARCH_CACHE_TTL_HOURS = 168
DEFAULT_PERSISTENT_PAGE_TEXT_CACHE_TTL_HOURS = 24
DEFAULT_PERSISTENT_VERIFY_CACHE_TTL_HOURS = 12
DEFAULT_PERSISTENT_PAGE_TEXT_MAX_CHARS = 120000
# A named place's coordinates don't change; a long TTL is safe and avoids
# re-geocoding (and re-throttling against) Nominatim across runs.
DEFAULT_PERSISTENT_GEOCODE_CACHE_TTL_HOURS = 720
# AllTrails page content/block-state is more volatile than coordinates -- keep
# this short so a transient DataDome block or stale trail status doesn't get
# frozen in across runs.
DEFAULT_PERSISTENT_ALLTRAILS_CACHE_TTL_HOURS = 12
# A Wayback Machine snapshot's HTML is immutable once crawled (fetching the
# same archived-snapshot URL tomorrow returns byte-identical content), and
# this cache exists purely to extract a trailhead coordinate that also
# doesn't change -- so a long TTL (same rationale/value as the Nominatim
# geocode cache above) is safe and avoids re-hitting archive.org for a trail
# already resolved in a prior run.
DEFAULT_PERSISTENT_WAYBACK_CACHE_TTL_HOURS = 720
# Direct-batch harvest rows (the per-destination-per-kind Grok HTML-list
# responses for attractions/restaurants/trails/en-route stops) are the most
# expensive part of URL discovery and the most repeated across same-day
# iterative validation runs of an unchanged manifest.
#
# Raised 24h -> 168h (7 days) on 2026-08-19, matching
# DEFAULT_PERSISTENT_SEARCH_CACHE_TTL_HOURS and for the same reason. A day's
# TTL only ever helped SAME-DAY rebuilds; a run the next morning re-paid xAI's
# $5/1000 web_search fee to re-harvest candidate lists that had not
# meaningfully changed. Measured on the 2026-08-19/20 pair: a cold run cost
# $2.86 across 477 web_search calls, the warm rebuild $0.33 across 50 -- and
# this bucket is the highest-volume contributor, so a 24h expiry was throwing
# most of that saving away every night.
#
# Safe for the same reason the search TTL is. A harvest row answers "which
# candidate places does this destination/kind query yield, and where do they
# live" -- names, URLs, ratings, descriptions. Real places and their
# authoritative pages do not turn over week to week. Whether a page is still
# LIVE, and whether a place has CLOSED, are separate questions re-checked on
# much shorter independent TTLs regardless of this one: _verify_url_cached at
# 12h (DEFAULT_PERSISTENT_VERIFY_CACHE_TTL_HOURS) and page-text/AllTrails
# fetches at 24h/12h, which is what feeds closure-marker detection
# (_has_attraction_closure_marker et al). Raising THOSE would genuinely let a
# closure or dead link go unnoticed for longer; raising this one cannot.
#
# What a week-old harvest can carry is stale ratings/vote counts feeding
# _meets_place_interest_threshold, and a place opened in the last week being
# absent. Both are quality-of-ranking drift, not correctness or liveness --
# and both were already tolerated for up to a day.
DEFAULT_PERSISTENT_HARVEST_CACHE_TTL_HOURS = 168

# The four per-section harvest caches, and the key each is stored under in the
# persistent cache file. Named once: the mapping was written out twice, in the
# load path and the save path, and a fifth section added to one of them would
# silently not be persisted by the other.
HARVEST_CACHE_SECTIONS: dict[str, str] = {
    "direct_batch_harvest_alltrails": "_alltrails_direct_batch_cache",
    "direct_batch_harvest_attractions": "_attraction_direct_batch_cache",
    "direct_batch_harvest_restaurants": "_restaurant_direct_batch_cache",
    "direct_batch_harvest_en_route": "_en_route_direct_batch_cache",
}
# A failed/empty direct-batch HTML harvest is deliberately never cached (an
# empty result isn't authoritative), but multiple items at the same
# destination each independently call the same per-destination-per-kind
# harvest -- under a sustained provider-side outage that means every one of
# them re-pays a full multi-attempt timeout cycle for a call that just failed
# seconds ago. This cooldown makes that failure "sticky" in memory for a
# short window so concurrent/sequential callers share one failure instead of
# each re-triggering the network call from scratch.
DEFAULT_DIRECT_BATCH_HTML_FAILURE_COOLDOWN_SECONDS = 180.0

MONTH_NAME_TO_NUMBER = {
    "january": 1,
    "february": 2,
    "march": 3,
    "april": 4,
    "may": 5,
    "june": 6,
    "july": 7,
    "august": 8,
    "september": 9,
    "october": 10,
    "november": 11,
    "december": 12,
}


def _build_query_variants(name: str, destination: str, category: str) -> list[str]:
    """Return progressively broader, concise query strings for a named item."""
    category_terms = re.findall(r"[a-z0-9]+", (category or "").lower())
    category_compact = " ".join(category_terms[:2]).strip()

    quoted_name = f'"{name}"'
    base = f"{name} {destination}".strip()
    quoted_base = f"{quoted_name} {destination}".strip()

    if not category_compact:
        return [quoted_base, base, quoted_name, name]

    return [
        f"{quoted_base} {category_compact}",
        quoted_base,
        f"{base} {category_compact}",
        base,
    ]


def _build_alltrails_query_variants(name: str, destination: str) -> list[str]:
    """Focused AllTrails-first query variants to reduce search token overhead."""
    quoted_name = f'"{name}"'
    quoted_dest = f'"{destination}"'
    return [
        f"{quoted_name} {quoted_dest} alltrails",
        f"{name} {destination} alltrails trail",
        f"{quoted_name} alltrails",
        f"{quoted_name} alltrails trail",
        f"{name} alltrails",
        f"{name} trail",
        name,
    ]


def _build_restaurant_query_variants(name: str, destination: str) -> list[str]:
    """Focused restaurant query variants tuned for maps/directory lookup."""
    quoted_name = f'"{name}"'
    quoted_dest = f'"{destination}"'
    return [
        f"{quoted_name} {quoted_dest} restaurant",
        f"{name} {destination} restaurant",
        f"{quoted_name} {destination}",
    ]


def _build_nps_activity_query_variants(name: str, destination: str) -> list[str]:
    """Build NPS-focused activity variants for category-style attractions."""
    lowered = f"{name} {destination}".lower()
    theme = "activity program"
    if "stargazing" in lowered or "dark sky" in lowered or "night sky" in lowered:
        theme = "night sky astronomy"
    elif "bird" in lowered:
        theme = "wildlife birding"
    elif "fishing" in lowered:
        theme = "fishing"

    quoted_name = f'"{name}"'
    quoted_dest = f'"{destination}"'
    return [
        f"{quoted_dest} {theme}",
        f"{destination} {theme} nps",
        f"{quoted_name} {quoted_dest} nps",
        f"{name} {destination} nps",
    ]


def _build_attraction_maps_query_variants(name: str, destination: str) -> list[str]:
    """Build attraction queries tuned for Google Maps Things to do results."""
    quoted_name = f'"{name}"'
    quoted_dest = f'"{destination}"'
    return [
        f"{quoted_name} {quoted_dest} things to do",
        f"{name} {destination} things to do",
        f"{quoted_name} {destination} attraction",
    ]


def _cached_ok(value: Any) -> bool:
    """Whether a cached tuple records a success. `(ok, ...)` throughout."""
    if isinstance(value, tuple) and value:
        return bool(value[0])
    return bool(value)


class URLDiscoverer:
    def __init__(
        self,
        config_path: str | Any = "config.yaml",
        llm_client: MultiLLMClient | None = None,
        *,
        disable_trails: bool = False,
        alltrails_source: str | None = None,
        attraction_source: str | None = None,
        restaurant_source: str | None = None,
        en_route_source: str | None = None,
        disable_en_route: bool = False,
        disable_restaurants: bool = False,
        output_dir: str | Path | None = None,
        search_provider_override: str | None = None,
    ) -> None:
        self._llm = llm_client or MultiLLMClient(config_path)
        # When the canonical content-generation provider matches the search
        # provider, search shares its exact model instead of independently
        # falling back to its own env var default -- otherwise the two could
        # silently diverge (config.yaml says one model, search quietly uses
        # another). This only applies when they match; url_discovery.
        # search_provider (config.yaml) selects grok or claude independently
        # of ai.provider, defaulting to grok -- unchanged behavior from
        # before this selection existed. See search_provider.py and
        # claude_search.py (docs/design/search-provider-capability-probe.md).
        grok_model = self._llm.model if self._llm.provider == "grok" else None
        claude_model = self._llm.model if self._llm.provider == "anthropic" else None
        # Split the discovery model from the content-generation model.
        #
        # Measured on the 2026-08-21 cold-start run: URL discovery is 91% of
        # all tokens (batches 49.3%, per-item fallbacks 41.8%), while the ten
        # destination content bundles -- the actual product -- are 0.4% each.
        # 87% of discovery's tokens are INPUT, because each batch call carries
        # a ~300-token prompt and ~24,000 tokens of injected search results.
        #
        # That work is extraction from retrieved pages, not the reasoning the
        # top tier is worth paying for, so it does not need the same model as
        # content generation. Keeping content generation expensive costs
        # almost nothing at 0.4%; making discovery cheaper moves the dominant
        # term directly.
        #
        # Unset leaves both on the content model -- exactly today's behaviour.
        self._link_type_site_filters: dict[str, str] = self._read_link_type_site_filters(config_path)
        # NOT gated on the CONTENT provider. That was the original bug: this
        # project generates content with openai and searches with grok, so
        # `self._llm.provider == "grok"` was false and the override silently
        # never applied -- the 2026-08-22 baseline run still billed the
        # expensive tier. `grok_model` is consumed only by GrokSearch, so
        # setting it is inert unless the search provider is actually grok.
        search_model_override = self._read_search_model_override(config_path)
        if search_model_override:
            logger.info(
                "URL discovery using search model '%s' (content generation stays on '%s')",
                search_model_override, self._llm.model,
            )
            grok_model = search_model_override
        self._search = build_search_client(
            config_path,
            config_section="url_discovery",
            provider_key="search_provider",
            provider_override=search_provider_override,
            grok_model=grok_model,
            claude_model=claude_model,
            usage_tracker=self._llm.usage_tracker,
            usage_operation_prefix="url_discovery",
        )
        # search_provider_override (2026-08-15, --search-provider CLI flag)
        # forces a single provider with no fallback at all -- for a clean
        # per-provider cost/behavior comparison, uncontaminated by the
        # cross-provider batch retry or the non-batch fallback both
        # independently attempting a second provider. Every call site that
        # reads self._search_fallback already treats None as "no fallback
        # available" (see _fetch_direct_batch_html_rows, _search_cached),
        # so this doesn't need special-casing beyond just not building one.
        if search_provider_override:
            self._search_fallback = None
        else:
            # Separate client for the per-item search fallback (_search_cached,
            # used by _search_first/_search_first_strict) -- a lower-volume,
            # different-shaped call than the direct-batch HTML harvest above, and
            # independently pinned via nonbatch_search_provider (config.yaml).
            # See that key's comment for the rationale.
            self._search_fallback = build_search_client(
                config_path,
                config_section="url_discovery",
                provider_key="nonbatch_search_provider",
                grok_model=grok_model,
                claude_model=claude_model,
                usage_tracker=self._llm.usage_tracker,
                usage_operation_prefix="url_discovery_fallback",
            )
        self._url_validator = URLValidator()

        self._uninterested_keywords: tuple[str, ...] = DEFAULT_UNINTERESTED_ATTRACTION_KEYWORDS
        self._seasonal_ski_keywords: tuple[str, ...] = DEFAULT_SKI_ATTRACTION_KEYWORDS
        self._ski_in_season_months: tuple[int, ...] = DEFAULT_SKI_IN_SEASON_MONTHS
        self._max_trail_miles: float = DEFAULT_MAX_TRAIL_MILES
        self._allow_blocked_alltrails: bool = DEFAULT_ALLOW_BLOCKED_ALLTRAILS
        self._alltrails_min_confidence_for_publish: str = DEFAULT_ALLTRAILS_MIN_CONFIDENCE_FOR_PUBLISH
        self._alltrails_request_delay_seconds: float = DEFAULT_ALLTRAILS_REQUEST_DELAY_SECONDS
        self._alltrails_block_cooldown_seconds: float = DEFAULT_ALLTRAILS_BLOCK_COOLDOWN_SECONDS
        self._enable_filtered_alltrails_selection: bool = DEFAULT_ENABLE_FILTERED_ALLTRAILS_SELECTION
        self._alltrails_filter_max_miles: float = DEFAULT_ALLTRAILS_FILTER_MAX_MILES
        self._alltrails_filter_max_gain_feet: int = DEFAULT_ALLTRAILS_FILTER_MAX_GAIN_FEET
        self._alltrails_filter_min_reviews: int = DEFAULT_ALLTRAILS_FILTER_MIN_REVIEWS
        self._alltrails_filter_allowed_difficulties: tuple[str, ...] = DEFAULT_ALLTRAILS_FILTER_ALLOWED_DIFFICULTIES
        self._alltrails_filtered_selection_cache: dict[tuple[str, str], str | None] = {}
        self._alltrails_fetch_cache: dict[str, tuple[bool, int | str, str]] = {}
        self._alltrails_last_request_ts: float = 0.0
        self._alltrails_blocked_until_ts: float = 0.0
        self._alltrails_fetch_lock: Lock = Lock()
        # Wayback Machine fallback fetch state (see _fetch_wayback_alltrails_text)
        # -- keyed by the original AllTrails trail URL (not the archive.org
        # snapshot URL), separate lock/pacing from _alltrails_fetch_lock above
        # since these requests go to a completely different domain/host.
        self._wayback_request_delay_seconds: float = DEFAULT_WAYBACK_REQUEST_DELAY_SECONDS
        self._wayback_fetch_cache: dict[str, tuple[bool, int | str, str]] = {}
        self._wayback_last_request_ts: float = 0.0
        self._wayback_fetch_lock: Lock = Lock()
        self._max_alltrails_query_attempts: int = 2
        self._max_restaurant_query_attempts: int = 1
        self._alltrails_rating_min: float = DEFAULT_ALLTRAILS_RATING_MIN
        self._alltrails_rating_min_votes: int = DEFAULT_ALLTRAILS_RATING_MIN_VOTES
        self._alltrails_rating_boost: int = DEFAULT_ALLTRAILS_RATING_BOOST
        self._restaurant_rating_min: float = DEFAULT_RESTAURANT_RATING_MIN
        self._restaurant_rating_min_votes: int = DEFAULT_RESTAURANT_RATING_MIN_VOTES
        self._restaurant_rating_boost: int = DEFAULT_RESTAURANT_RATING_BOOST
        self._restaurant_prefer_official_site_over_tripadvisor: bool = DEFAULT_RESTAURANT_PREFER_OFFICIAL_SITE_OVER_TRIPADVISOR
        self._place_interest_min_rating: float = DEFAULT_PLACE_INTEREST_MIN_RATING
        self._place_interest_min_votes: int = DEFAULT_PLACE_INTEREST_MIN_VOTES
        self._place_interest_require_metadata: bool = DEFAULT_PLACE_INTEREST_REQUIRE_METADATA
        self._restaurant_name_denylist: frozenset[str] = frozenset(DEFAULT_RESTAURANT_NAME_DENYLIST)
        self._url_policy_mode: str = DEFAULT_URL_POLICY_MODE
        self._url_policy_blocked_classes: set[str] = set(DEFAULT_URL_POLICY_BLOCKED_CLASSES)
        self._url_domain_denylist: frozenset[str] = frozenset(DEFAULT_URL_DOMAIN_DENYLIST)
        self._url_policy_allowlist_path: str = DEFAULT_URL_POLICY_ALLOWLIST_PATH
        self._url_policy_auto_allow_from_output: bool = DEFAULT_URL_POLICY_AUTO_ALLOW_FROM_OUTPUT
        self._url_policy_output_path: str = DEFAULT_URL_POLICY_OUTPUT_PATH
        self._url_policy_allowlisted_urls: set[str] = set()
        self._alltrails_slug_denylist: frozenset[str] = frozenset(DEFAULT_ALLTRAILS_SLUG_DENYLIST)
        self._disable_trails: bool = bool(disable_trails)
        self._alltrails_source: str = DEFAULT_ALLTRAILS_SOURCE
        self._attraction_source: str = DEFAULT_ATTRACTION_SOURCE
        self._restaurant_source: str = DEFAULT_RESTAURANT_SOURCE
        self._disable_en_route: bool = bool(disable_en_route)
        self._disable_restaurants: bool = bool(disable_restaurants)
        self._en_route_source: str = DEFAULT_EN_ROUTE_SOURCE
        self._fallback_mode: str = DEFAULT_FALLBACK_MODE
        # GH #68 multi-site grouping (config.yaml multi_site_grouping.base_owned_categories) --
        # see generator/multi_site_grouping.py for the resolution rule this feeds.
        self._multi_site_base_owned_categories: frozenset[str] = frozenset(DEFAULT_BASE_OWNED_CATEGORIES)
        self._direct_link_batch_count: int = DEFAULT_DIRECT_LINK_BATCH_COUNT
        self._direct_batch_authoritative: bool = DEFAULT_DIRECT_BATCH_AUTHORITATIVE
        self._restaurant_direct_batch_item_count: int = DEFAULT_RESTAURANT_DIRECT_BATCH_ITEM_COUNT
        self._en_route_direct_batch_item_count: int = DEFAULT_EN_ROUTE_DIRECT_BATCH_ITEM_COUNT
        self._attraction_direct_batch_items_per_day: int = DEFAULT_ATTRACTION_DIRECT_BATCH_ITEMS_PER_DAY
        self._trail_direct_batch_items_per_day: int = DEFAULT_TRAIL_DIRECT_BATCH_ITEMS_PER_DAY
        self._direct_batch_group_size: int = DEFAULT_DIRECT_BATCH_GROUP_SIZE
        self._restaurant_direct_batch_min_results: int = DEFAULT_RESTAURANT_DIRECT_BATCH_MIN_RESULTS
        self._en_route_direct_batch_min_results: int = DEFAULT_EN_ROUTE_DIRECT_BATCH_MIN_RESULTS
        self._en_route_detour_max_minutes: int = DEFAULT_EN_ROUTE_DETOUR_MAX_MINUTES
        self._en_route_detour_max_miles: float = DEFAULT_EN_ROUTE_DETOUR_MAX_MILES
        self._en_route_require_detour_metadata: bool = DEFAULT_EN_ROUTE_REQUIRE_DETOUR_METADATA
        # Maps a normalized "known-good" URL to the set of item-name token-keys it
        # was actually validated against. Keyed by (url, item), NOT just url --
        # see _is_remembered_direct_batch_authoritative_url for why a flat url-only
        # cache is unsafe (it let a URL validated for one item silently vouch for
        # an unrelated item that happened to reuse the same URL string).
        self._direct_batch_authoritative_urls: dict[str, set[frozenset[str]]] = {}
        self._alltrails_direct_batch_cache: dict[str, list[dict[str, Any]]] = {}
        self._attraction_direct_batch_cache: dict[str, list[dict[str, Any]]] = {}
        self._attraction_maps_area_cache: dict[str, list[dict[str, Any]]] = {}
        self._restaurant_direct_batch_cache: dict[str, list[dict[str, Any]]] = {}
        self._en_route_direct_batch_cache: dict[str, list[dict[str, Any]]] = {}
        self._direct_batch_html_key_locks: dict[str, Lock] = {}
        self._direct_batch_html_failure_ts: dict[str, float] = {}
        # Route freshness. `_harvest_preloaded_keys` is what came off disk at
        # startup, so a later lookup can tell "this route was already harvested
        # before today" from "this run harvested it a moment ago" -- two very
        # different facts that a plain cache-hit counter reports as one.
        self._harvest_preloaded_keys: dict[str, set[str]] = {}
        self._harvest_freshness: dict[str, int] = {"warm": 0, "repeat": 0, "cold": 0}
        self._direct_batch_html_failure_cooldown_seconds: float = float(
            DEFAULT_DIRECT_BATCH_HTML_FAILURE_COOLDOWN_SECONDS
        )
        self._maps_url_resolution_cache: dict[str, str] = {}
        self._fetch_final_url_cache: dict[str, str] = {}
        self._verify_url_cache: dict[str, tuple[bool, int | str]] = {}
        self._page_text_cache: dict[str, tuple[bool, int | str, str]] = {}
        self._domain_blocked_until_ts: dict[str, float] = {}
        self._domain_block_cooldown_seconds: float = float(DEFAULT_DOMAIN_BLOCK_COOLDOWN_SECONDS)
        self._route_distance_live_fetch_enabled: bool = DEFAULT_ROUTE_DISTANCE_LIVE_FETCH_ENABLED
        # Optional. Inert unless a Google Maps Platform key is configured, in
        # which case secondary map links become place_id links instead of
        # text searches. See generator/place_resolver.py.
        self._place_resolver = PlaceResolver()
        self._search_failure_ts: dict[str, float] = {}
        self._search_failure_cooldown_seconds: float = float(DEFAULT_SEARCH_FAILURE_COOLDOWN_SECONDS)
        self._search_results_cache: dict[str, list[dict[str, Any]]] = {}
        # Maps discovered URL → the search candidate dict that produced it (name/snippet)
        self._search_winner_snippets: dict[str, dict[str, Any]] = {}
        self._request_cache_lock: Lock = Lock()
        self._nominatim_rate_limit_lock: Lock = Lock()
        self._persistent_cache_enabled: bool = DEFAULT_PERSISTENT_CACHE_ENABLED
        self._persistent_cache_path: str = DEFAULT_PERSISTENT_CACHE_PATH
        self._persistent_search_cache_ttl_hours: float = float(DEFAULT_PERSISTENT_SEARCH_CACHE_TTL_HOURS)
        self._persistent_page_text_cache_ttl_hours: float = float(DEFAULT_PERSISTENT_PAGE_TEXT_CACHE_TTL_HOURS)
        self._persistent_verify_cache_ttl_hours: float = float(DEFAULT_PERSISTENT_VERIFY_CACHE_TTL_HOURS)
        self._persistent_geocode_cache_ttl_hours: float = float(DEFAULT_PERSISTENT_GEOCODE_CACHE_TTL_HOURS)
        self._persistent_alltrails_cache_ttl_hours: float = float(DEFAULT_PERSISTENT_ALLTRAILS_CACHE_TTL_HOURS)
        self._persistent_wayback_cache_ttl_hours: float = float(DEFAULT_PERSISTENT_WAYBACK_CACHE_TTL_HOURS)
        self._persistent_harvest_cache_ttl_hours: float = float(DEFAULT_PERSISTENT_HARVEST_CACHE_TTL_HOURS)
        # Birth timestamps for entries read from the persistent cache, keyed by
        # (payload section, entry key). The in-memory caches themselves hold
        # only values, with no room for an age, so without this side table
        # _save_persistent_caches has nothing to write but "now" -- which
        # silently renews every entry on every save and makes the TTLs
        # unenforceable (a 100h-old entry came back from a resave dated 0h).
        # Entries first seen this run are absent here and correctly get "now".
        self._persistent_entry_ts: dict[tuple[str, str], float] = {}
        self._persistent_cache_dirty: bool = False
        self._persistent_cache_write_every: int = 25
        self._persistent_cache_pending_writes: int = 0
        self._decision_stats_by_destination: dict[str, dict[str, int]] = {}
        self._decision_source_stats_by_destination: dict[str, dict[str, int]] = {}
        self._decision_threads_by_destination: dict[str, dict[str, list[dict[str, Any]]]] = {}
        self._decision_event_sequence: int = 0
        self._run_output_dir: Path | None = Path(output_dir) if output_dir else None
        self._direct_batch_html_capture_enabled: bool = DEFAULT_DIRECT_BATCH_HTML_CAPTURE_ENABLED
        self._direct_batch_html_capture_subdir: str = DEFAULT_DIRECT_BATCH_HTML_CAPTURE_SUBDIR
        self._load_interest_filters(config_path)
        source_override = str(alltrails_source or "").strip().lower().replace("-", "_")
        if source_override in {"search", "direct_link_batch"}:
            self._alltrails_source = source_override
        attraction_override = str(attraction_source or "").strip().lower().replace("-", "_")
        if attraction_override in {"search", "direct_link_batch"}:
            self._attraction_source = attraction_override
        restaurant_override = str(restaurant_source or "").strip().lower().replace("-", "_")
        if restaurant_override in {"search", "direct_link_batch"}:
            self._restaurant_source = restaurant_override
        en_route_override = str(en_route_source or "").strip().lower().replace("-", "_")
        if en_route_override in {"search", "direct_link_batch", "maps"}:
            self._en_route_source = en_route_override
        self._load_url_policy_allowlist()
        self._load_persistent_caches()

    def _load_interest_filters(self, config_path: str | Any) -> None:
        try:
            import yaml

            with Path(config_path).open(encoding="utf-8") as f:
                cfg = yaml.safe_load(f) or {}
            url_cfg = cfg.get("url_discovery", {}) or {}

            multi_site_cfg = cfg.get("multi_site_grouping", {}) or {}
            raw_base_owned = multi_site_cfg.get("base_owned_categories", DEFAULT_BASE_OWNED_CATEGORIES)
            if isinstance(raw_base_owned, list):
                self._multi_site_base_owned_categories = frozenset(
                    str(c or "").strip().lower() for c in raw_base_owned if str(c or "").strip()
                )

            raw_keywords = url_cfg.get("uninterested_attraction_keywords", []) or []
            normalized_keywords = tuple(
                kw.strip().lower() for kw in raw_keywords if str(kw or "").strip()
            )
            if normalized_keywords:
                self._uninterested_keywords = normalized_keywords

            ski_cfg = url_cfg.get("seasonal_uninterested", {}).get("ski", {}) or {}
            raw_ski_keywords = ski_cfg.get("keywords", []) or []
            normalized_ski_keywords = tuple(
                kw.strip().lower() for kw in raw_ski_keywords if str(kw or "").strip()
            )
            if normalized_ski_keywords:
                self._seasonal_ski_keywords = normalized_ski_keywords

            raw_months = ski_cfg.get("in_season_months", []) or []
            normalized_months: list[int] = []
            for m in raw_months:
                try:
                    m_i = int(m)
                except (TypeError, ValueError):
                    continue
                if 1 <= m_i <= 12:
                    normalized_months.append(m_i)
            if normalized_months:
                self._ski_in_season_months = tuple(normalized_months)

            max_trail_miles = url_cfg.get("max_trail_miles", DEFAULT_MAX_TRAIL_MILES)
            try:
                parsed_max = float(max_trail_miles)
                if parsed_max > 0:
                    self._max_trail_miles = parsed_max
            except (TypeError, ValueError):
                self._max_trail_miles = DEFAULT_MAX_TRAIL_MILES

            allow_blocked = url_cfg.get("allow_blocked_alltrails", DEFAULT_ALLOW_BLOCKED_ALLTRAILS)
            self._allow_blocked_alltrails = bool(allow_blocked)

            min_conf = str(
                url_cfg.get(
                    "alltrails_min_confidence_for_publish",
                    DEFAULT_ALLTRAILS_MIN_CONFIDENCE_FOR_PUBLISH,
                )
                or DEFAULT_ALLTRAILS_MIN_CONFIDENCE_FOR_PUBLISH
            ).strip().lower()
            if min_conf in {"low", "medium", "high"}:
                self._alltrails_min_confidence_for_publish = min_conf
            else:
                self._alltrails_min_confidence_for_publish = DEFAULT_ALLTRAILS_MIN_CONFIDENCE_FOR_PUBLISH

            request_delay_seconds = url_cfg.get(
                "alltrails_request_delay_seconds",
                DEFAULT_ALLTRAILS_REQUEST_DELAY_SECONDS,
            )
            try:
                parsed_delay = float(request_delay_seconds)
                if parsed_delay >= 0:
                    self._alltrails_request_delay_seconds = parsed_delay
            except (TypeError, ValueError):
                self._alltrails_request_delay_seconds = DEFAULT_ALLTRAILS_REQUEST_DELAY_SECONDS

            block_cooldown_seconds = url_cfg.get(
                "alltrails_block_cooldown_seconds",
                DEFAULT_ALLTRAILS_BLOCK_COOLDOWN_SECONDS,
            )
            try:
                parsed_cooldown = float(block_cooldown_seconds)
                if parsed_cooldown >= 0:
                    self._alltrails_block_cooldown_seconds = parsed_cooldown
            except (TypeError, ValueError):
                self._alltrails_block_cooldown_seconds = DEFAULT_ALLTRAILS_BLOCK_COOLDOWN_SECONDS

            wayback_delay_seconds = url_cfg.get(
                "wayback_request_delay_seconds",
                DEFAULT_WAYBACK_REQUEST_DELAY_SECONDS,
            )
            try:
                parsed_wayback_delay = float(wayback_delay_seconds)
                if parsed_wayback_delay >= 0:
                    self._wayback_request_delay_seconds = parsed_wayback_delay
            except (TypeError, ValueError):
                self._wayback_request_delay_seconds = DEFAULT_WAYBACK_REQUEST_DELAY_SECONDS

            enable_filtered_alltrails = url_cfg.get(
                "enable_filtered_alltrails_selection",
                DEFAULT_ENABLE_FILTERED_ALLTRAILS_SELECTION,
            )
            self._enable_filtered_alltrails_selection = bool(enable_filtered_alltrails)

            filter_max_miles = url_cfg.get("alltrails_filter_max_miles", DEFAULT_ALLTRAILS_FILTER_MAX_MILES)
            try:
                parsed_filter_miles = float(filter_max_miles)
                if parsed_filter_miles > 0:
                    self._alltrails_filter_max_miles = parsed_filter_miles
            except (TypeError, ValueError):
                self._alltrails_filter_max_miles = DEFAULT_ALLTRAILS_FILTER_MAX_MILES

            filter_max_gain = url_cfg.get("alltrails_filter_max_gain_feet", DEFAULT_ALLTRAILS_FILTER_MAX_GAIN_FEET)
            try:
                parsed_filter_gain = int(filter_max_gain)
                if parsed_filter_gain >= 0:
                    self._alltrails_filter_max_gain_feet = parsed_filter_gain
            except (TypeError, ValueError):
                self._alltrails_filter_max_gain_feet = DEFAULT_ALLTRAILS_FILTER_MAX_GAIN_FEET

            filter_min_reviews = url_cfg.get("alltrails_filter_min_reviews", DEFAULT_ALLTRAILS_FILTER_MIN_REVIEWS)
            try:
                parsed_filter_reviews = int(filter_min_reviews)
                if parsed_filter_reviews >= 0:
                    self._alltrails_filter_min_reviews = parsed_filter_reviews
            except (TypeError, ValueError):
                self._alltrails_filter_min_reviews = DEFAULT_ALLTRAILS_FILTER_MIN_REVIEWS

            raw_difficulties = url_cfg.get(
                "alltrails_filter_allowed_difficulties",
                list(DEFAULT_ALLTRAILS_FILTER_ALLOWED_DIFFICULTIES),
            )
            if isinstance(raw_difficulties, list):
                normalized_difficulties = tuple(
                    str(v or "").strip().lower() for v in raw_difficulties if str(v or "").strip()
                )
                if normalized_difficulties:
                    self._alltrails_filter_allowed_difficulties = normalized_difficulties

            max_alltrails_attempts = url_cfg.get("max_alltrails_query_attempts", 2)
            try:
                parsed_alltrails_attempts = int(max_alltrails_attempts)
                if parsed_alltrails_attempts > 0:
                    self._max_alltrails_query_attempts = parsed_alltrails_attempts
            except (TypeError, ValueError):
                self._max_alltrails_query_attempts = 2

            max_restaurant_attempts = url_cfg.get("max_restaurant_query_attempts", 1)
            try:
                parsed_restaurant_attempts = int(max_restaurant_attempts)
                if parsed_restaurant_attempts > 0:
                    self._max_restaurant_query_attempts = parsed_restaurant_attempts
            except (TypeError, ValueError):
                self._max_restaurant_query_attempts = 1

            alltrails_rating_min = url_cfg.get("alltrails_rating_min", DEFAULT_ALLTRAILS_RATING_MIN)
            try:
                parsed_alltrails_rating_min = float(alltrails_rating_min)
                if 0.0 <= parsed_alltrails_rating_min <= 5.0:
                    self._alltrails_rating_min = parsed_alltrails_rating_min
            except (TypeError, ValueError):
                self._alltrails_rating_min = DEFAULT_ALLTRAILS_RATING_MIN

            alltrails_rating_min_votes = url_cfg.get("alltrails_rating_min_votes", DEFAULT_ALLTRAILS_RATING_MIN_VOTES)
            try:
                parsed_alltrails_min_votes = int(alltrails_rating_min_votes)
                if parsed_alltrails_min_votes >= 0:
                    self._alltrails_rating_min_votes = parsed_alltrails_min_votes
            except (TypeError, ValueError):
                self._alltrails_rating_min_votes = DEFAULT_ALLTRAILS_RATING_MIN_VOTES

            alltrails_rating_boost = url_cfg.get("alltrails_rating_boost", DEFAULT_ALLTRAILS_RATING_BOOST)
            try:
                parsed_alltrails_boost = int(alltrails_rating_boost)
                if parsed_alltrails_boost >= 0:
                    self._alltrails_rating_boost = parsed_alltrails_boost
            except (TypeError, ValueError):
                self._alltrails_rating_boost = DEFAULT_ALLTRAILS_RATING_BOOST

            restaurant_rating_min = url_cfg.get("restaurant_rating_min", DEFAULT_RESTAURANT_RATING_MIN)
            try:
                parsed_restaurant_rating_min = float(restaurant_rating_min)
                if 0.0 <= parsed_restaurant_rating_min <= 5.0:
                    self._restaurant_rating_min = parsed_restaurant_rating_min
            except (TypeError, ValueError):
                self._restaurant_rating_min = DEFAULT_RESTAURANT_RATING_MIN

            restaurant_rating_min_votes = url_cfg.get("restaurant_rating_min_votes", DEFAULT_RESTAURANT_RATING_MIN_VOTES)
            try:
                parsed_restaurant_min_votes = int(restaurant_rating_min_votes)
                if parsed_restaurant_min_votes >= 0:
                    self._restaurant_rating_min_votes = parsed_restaurant_min_votes
            except (TypeError, ValueError):
                self._restaurant_rating_min_votes = DEFAULT_RESTAURANT_RATING_MIN_VOTES

            restaurant_rating_boost = url_cfg.get("restaurant_rating_boost", DEFAULT_RESTAURANT_RATING_BOOST)
            try:
                parsed_restaurant_boost = int(restaurant_rating_boost)
                if parsed_restaurant_boost >= 0:
                    self._restaurant_rating_boost = parsed_restaurant_boost
            except (TypeError, ValueError):
                self._restaurant_rating_boost = DEFAULT_RESTAURANT_RATING_BOOST

            prefer_official_over_tripadvisor = url_cfg.get(
                "restaurant_prefer_official_site_over_tripadvisor",
                DEFAULT_RESTAURANT_PREFER_OFFICIAL_SITE_OVER_TRIPADVISOR,
            )
            self._restaurant_prefer_official_site_over_tripadvisor = bool(prefer_official_over_tripadvisor)

            place_interest_min_rating = url_cfg.get("place_interest_min_rating", DEFAULT_PLACE_INTEREST_MIN_RATING)
            try:
                parsed_place_interest_rating = float(place_interest_min_rating)
                if 0.0 <= parsed_place_interest_rating <= 5.0:
                    self._place_interest_min_rating = parsed_place_interest_rating
            except (TypeError, ValueError):
                self._place_interest_min_rating = DEFAULT_PLACE_INTEREST_MIN_RATING

            place_interest_min_votes = url_cfg.get("place_interest_min_votes", DEFAULT_PLACE_INTEREST_MIN_VOTES)
            try:
                parsed_place_interest_votes = int(place_interest_min_votes)
                if parsed_place_interest_votes >= 0:
                    self._place_interest_min_votes = parsed_place_interest_votes
            except (TypeError, ValueError):
                self._place_interest_min_votes = DEFAULT_PLACE_INTEREST_MIN_VOTES

            self._place_interest_require_metadata = bool(
                url_cfg.get("place_interest_require_metadata", DEFAULT_PLACE_INTEREST_REQUIRE_METADATA)
            )

            policy_mode = str(
                url_cfg.get("url_policy_mode", DEFAULT_URL_POLICY_MODE)
                or DEFAULT_URL_POLICY_MODE
            ).strip().lower()
            if policy_mode in {"off", "monitor", "enforce"}:
                self._url_policy_mode = policy_mode
            else:
                self._url_policy_mode = DEFAULT_URL_POLICY_MODE

            raw_blocked_classes = url_cfg.get(
                "url_policy_blocked_classes",
                list(DEFAULT_URL_POLICY_BLOCKED_CLASSES),
            )
            if isinstance(raw_blocked_classes, list):
                normalized_blocked = {
                    str(v or "").strip().lower()
                    for v in raw_blocked_classes
                    if str(v or "").strip()
                }
                if normalized_blocked:
                    self._url_policy_blocked_classes = normalized_blocked

            allowlist_path = str(
                url_cfg.get("url_policy_allowlist_path", DEFAULT_URL_POLICY_ALLOWLIST_PATH)
                or DEFAULT_URL_POLICY_ALLOWLIST_PATH
            ).strip()
            if allowlist_path:
                self._url_policy_allowlist_path = allowlist_path

            auto_allow_from_output = url_cfg.get(
                "url_policy_auto_allow_from_output",
                DEFAULT_URL_POLICY_AUTO_ALLOW_FROM_OUTPUT,
            )
            self._url_policy_auto_allow_from_output = bool(auto_allow_from_output)

            output_path = str(
                url_cfg.get("url_policy_output_path", DEFAULT_URL_POLICY_OUTPUT_PATH)
                or DEFAULT_URL_POLICY_OUTPUT_PATH
            ).strip()
            if output_path:
                self._url_policy_output_path = output_path

            persistent_cache_enabled = url_cfg.get(
                "persistent_cache_enabled",
                DEFAULT_PERSISTENT_CACHE_ENABLED,
            )
            self._persistent_cache_enabled = bool(persistent_cache_enabled)

            persistent_cache_path = str(
                url_cfg.get("persistent_cache_path", DEFAULT_PERSISTENT_CACHE_PATH)
                or DEFAULT_PERSISTENT_CACHE_PATH
            ).strip()
            if persistent_cache_path:
                self._persistent_cache_path = persistent_cache_path

            persistent_search_ttl = url_cfg.get(
                "persistent_search_cache_ttl_hours",
                DEFAULT_PERSISTENT_SEARCH_CACHE_TTL_HOURS,
            )
            try:
                parsed_search_ttl = float(persistent_search_ttl)
                if parsed_search_ttl > 0:
                    self._persistent_search_cache_ttl_hours = parsed_search_ttl
            except (TypeError, ValueError):
                self._persistent_search_cache_ttl_hours = float(DEFAULT_PERSISTENT_SEARCH_CACHE_TTL_HOURS)

            persistent_page_ttl = url_cfg.get(
                "persistent_page_text_cache_ttl_hours",
                DEFAULT_PERSISTENT_PAGE_TEXT_CACHE_TTL_HOURS,
            )
            try:
                parsed_page_ttl = float(persistent_page_ttl)
                if parsed_page_ttl > 0:
                    self._persistent_page_text_cache_ttl_hours = parsed_page_ttl
            except (TypeError, ValueError):
                self._persistent_page_text_cache_ttl_hours = float(DEFAULT_PERSISTENT_PAGE_TEXT_CACHE_TTL_HOURS)

            persistent_verify_ttl = url_cfg.get(
                "persistent_verify_cache_ttl_hours",
                DEFAULT_PERSISTENT_VERIFY_CACHE_TTL_HOURS,
            )
            try:
                parsed_verify_ttl = float(persistent_verify_ttl)
                if parsed_verify_ttl > 0:
                    self._persistent_verify_cache_ttl_hours = parsed_verify_ttl
            except (TypeError, ValueError):
                self._persistent_verify_cache_ttl_hours = float(DEFAULT_PERSISTENT_VERIFY_CACHE_TTL_HOURS)

            persistent_geocode_ttl = url_cfg.get(
                "persistent_geocode_cache_ttl_hours",
                DEFAULT_PERSISTENT_GEOCODE_CACHE_TTL_HOURS,
            )
            try:
                parsed_geocode_ttl = float(persistent_geocode_ttl)
                if parsed_geocode_ttl > 0:
                    self._persistent_geocode_cache_ttl_hours = parsed_geocode_ttl
            except (TypeError, ValueError):
                self._persistent_geocode_cache_ttl_hours = float(DEFAULT_PERSISTENT_GEOCODE_CACHE_TTL_HOURS)

            persistent_alltrails_ttl = url_cfg.get(
                "persistent_alltrails_cache_ttl_hours",
                DEFAULT_PERSISTENT_ALLTRAILS_CACHE_TTL_HOURS,
            )
            try:
                parsed_alltrails_ttl = float(persistent_alltrails_ttl)
                if parsed_alltrails_ttl > 0:
                    self._persistent_alltrails_cache_ttl_hours = parsed_alltrails_ttl
            except (TypeError, ValueError):
                self._persistent_alltrails_cache_ttl_hours = float(DEFAULT_PERSISTENT_ALLTRAILS_CACHE_TTL_HOURS)

            persistent_wayback_ttl = url_cfg.get(
                "persistent_wayback_cache_ttl_hours",
                DEFAULT_PERSISTENT_WAYBACK_CACHE_TTL_HOURS,
            )
            try:
                parsed_wayback_ttl = float(persistent_wayback_ttl)
                if parsed_wayback_ttl > 0:
                    self._persistent_wayback_cache_ttl_hours = parsed_wayback_ttl
            except (TypeError, ValueError):
                self._persistent_wayback_cache_ttl_hours = float(DEFAULT_PERSISTENT_WAYBACK_CACHE_TTL_HOURS)

            persistent_harvest_ttl = url_cfg.get(
                "persistent_harvest_cache_ttl_hours",
                DEFAULT_PERSISTENT_HARVEST_CACHE_TTL_HOURS,
            )
            try:
                parsed_harvest_ttl = float(persistent_harvest_ttl)
                if parsed_harvest_ttl > 0:
                    self._persistent_harvest_cache_ttl_hours = parsed_harvest_ttl
            except (TypeError, ValueError):
                self._persistent_harvest_cache_ttl_hours = float(DEFAULT_PERSISTENT_HARVEST_CACHE_TTL_HOURS)

            harvest_failure_cooldown = url_cfg.get(
                "direct_batch_html_failure_cooldown_seconds",
                DEFAULT_DIRECT_BATCH_HTML_FAILURE_COOLDOWN_SECONDS,
            )
            try:
                parsed_failure_cooldown = float(harvest_failure_cooldown)
                if parsed_failure_cooldown >= 0:
                    self._direct_batch_html_failure_cooldown_seconds = parsed_failure_cooldown
            except (TypeError, ValueError):
                self._direct_batch_html_failure_cooldown_seconds = float(
                    DEFAULT_DIRECT_BATCH_HTML_FAILURE_COOLDOWN_SECONDS
                )

            domain_block_cooldown = url_cfg.get(
                "domain_block_cooldown_seconds",
                DEFAULT_DOMAIN_BLOCK_COOLDOWN_SECONDS,
            )
            try:
                parsed_domain_block_cooldown = float(domain_block_cooldown)
                if parsed_domain_block_cooldown >= 0:
                    self._domain_block_cooldown_seconds = parsed_domain_block_cooldown
            except (TypeError, ValueError):
                self._domain_block_cooldown_seconds = float(DEFAULT_DOMAIN_BLOCK_COOLDOWN_SECONDS)

            self._route_distance_live_fetch_enabled = bool(
                url_cfg.get("route_distance_live_fetch_enabled", DEFAULT_ROUTE_DISTANCE_LIVE_FETCH_ENABLED)
            )

            search_failure_cooldown = url_cfg.get(
                "search_failure_cooldown_seconds",
                DEFAULT_SEARCH_FAILURE_COOLDOWN_SECONDS,
            )
            try:
                parsed_search_failure_cooldown = float(search_failure_cooldown)
                if parsed_search_failure_cooldown >= 0:
                    self._search_failure_cooldown_seconds = parsed_search_failure_cooldown
            except (TypeError, ValueError):
                self._search_failure_cooldown_seconds = float(DEFAULT_SEARCH_FAILURE_COOLDOWN_SECONDS)

            raw_slug_denylist = url_cfg.get("alltrails_slug_denylist", [])
            if isinstance(raw_slug_denylist, list):
                self._alltrails_slug_denylist = frozenset(
                    str(v or "").strip().lower()
                    for v in raw_slug_denylist
                    if str(v or "").strip()
                )

            alltrails_source = str(
                url_cfg.get("alltrails_source", DEFAULT_ALLTRAILS_SOURCE)
                or DEFAULT_ALLTRAILS_SOURCE
            ).strip().lower().replace("-", "_")
            if alltrails_source in {"search", "direct_link_batch"}:
                self._alltrails_source = alltrails_source

            attraction_source = str(
                url_cfg.get("attraction_source", DEFAULT_ATTRACTION_SOURCE)
                or DEFAULT_ATTRACTION_SOURCE
            ).strip().lower().replace("-", "_")
            if attraction_source in {"search", "direct_link_batch"}:
                self._attraction_source = attraction_source

            restaurant_source = str(
                url_cfg.get("restaurant_source", DEFAULT_RESTAURANT_SOURCE)
                or DEFAULT_RESTAURANT_SOURCE
            ).strip().lower().replace("-", "_")
            if restaurant_source in {"search", "direct_link_batch"}:
                self._restaurant_source = restaurant_source

            en_route_source = str(
                url_cfg.get("en_route_source", DEFAULT_EN_ROUTE_SOURCE)
                or DEFAULT_EN_ROUTE_SOURCE
            ).strip().lower().replace("-", "_")
            if en_route_source in {"search", "direct_link_batch", "maps"}:
                self._en_route_source = en_route_source
            fallback_mode = str(url_cfg.get("fallback_mode", DEFAULT_FALLBACK_MODE) or "").strip().lower()
            if fallback_mode in {"search", "geocode_maps"}:
                self._fallback_mode = fallback_mode

            direct_link_batch_count = url_cfg.get("direct_link_batch_count", DEFAULT_DIRECT_LINK_BATCH_COUNT)
            try:
                parsed_batch_count = int(direct_link_batch_count)
                if parsed_batch_count > 0:
                    self._direct_link_batch_count = parsed_batch_count
            except (TypeError, ValueError):
                self._direct_link_batch_count = DEFAULT_DIRECT_LINK_BATCH_COUNT

            attraction_direct_batch_items_per_day = url_cfg.get(
                "attraction_direct_batch_items_per_day",
                DEFAULT_ATTRACTION_DIRECT_BATCH_ITEMS_PER_DAY,
            )
            try:
                parsed_attraction_items_per_day = int(attraction_direct_batch_items_per_day)
                if parsed_attraction_items_per_day > 0:
                    self._attraction_direct_batch_items_per_day = parsed_attraction_items_per_day
            except (TypeError, ValueError):
                self._attraction_direct_batch_items_per_day = DEFAULT_ATTRACTION_DIRECT_BATCH_ITEMS_PER_DAY

            trail_direct_batch_items_per_day = url_cfg.get(
                "trail_direct_batch_items_per_day",
                DEFAULT_TRAIL_DIRECT_BATCH_ITEMS_PER_DAY,
            )
            try:
                parsed_trail_items_per_day = int(trail_direct_batch_items_per_day)
                if parsed_trail_items_per_day > 0:
                    self._trail_direct_batch_items_per_day = parsed_trail_items_per_day
            except (TypeError, ValueError):
                self._trail_direct_batch_items_per_day = DEFAULT_TRAIL_DIRECT_BATCH_ITEMS_PER_DAY

            # DIRECT_BATCH_GROUP_SIZE env var takes priority over config.yaml so
            # this can be ramped up/down for a single experimental run without
            # editing tracked config -- see DEFAULT_DIRECT_BATCH_GROUP_SIZE.
            direct_batch_group_size = os.environ.get(
                "DIRECT_BATCH_GROUP_SIZE",
                url_cfg.get("direct_batch_group_size", DEFAULT_DIRECT_BATCH_GROUP_SIZE),
            )
            try:
                parsed_group_size = int(direct_batch_group_size)
                if parsed_group_size > 0:
                    self._direct_batch_group_size = parsed_group_size
            except (TypeError, ValueError):
                self._direct_batch_group_size = DEFAULT_DIRECT_BATCH_GROUP_SIZE

            restaurant_direct_batch_item_count = url_cfg.get(
                "restaurant_direct_batch_item_count",
                DEFAULT_RESTAURANT_DIRECT_BATCH_ITEM_COUNT,
            )
            try:
                parsed_restaurant_item_count = int(restaurant_direct_batch_item_count)
                if parsed_restaurant_item_count > 0:
                    self._restaurant_direct_batch_item_count = parsed_restaurant_item_count
            except (TypeError, ValueError):
                self._restaurant_direct_batch_item_count = DEFAULT_RESTAURANT_DIRECT_BATCH_ITEM_COUNT

            en_route_direct_batch_item_count = url_cfg.get(
                "en_route_direct_batch_item_count",
                DEFAULT_EN_ROUTE_DIRECT_BATCH_ITEM_COUNT,
            )
            try:
                parsed_en_route_item_count = int(en_route_direct_batch_item_count)
                if parsed_en_route_item_count > 0:
                    self._en_route_direct_batch_item_count = parsed_en_route_item_count
            except (TypeError, ValueError):
                self._en_route_direct_batch_item_count = DEFAULT_EN_ROUTE_DIRECT_BATCH_ITEM_COUNT

            restaurant_direct_batch_min_results = url_cfg.get(
                "restaurant_direct_batch_min_results",
                DEFAULT_RESTAURANT_DIRECT_BATCH_MIN_RESULTS,
            )
            try:
                parsed_restaurant_min_results = int(restaurant_direct_batch_min_results)
                if parsed_restaurant_min_results > 0:
                    self._restaurant_direct_batch_min_results = parsed_restaurant_min_results
            except (TypeError, ValueError):
                self._restaurant_direct_batch_min_results = DEFAULT_RESTAURANT_DIRECT_BATCH_MIN_RESULTS

            en_route_direct_batch_min_results = url_cfg.get(
                "en_route_direct_batch_min_results",
                DEFAULT_EN_ROUTE_DIRECT_BATCH_MIN_RESULTS,
            )
            try:
                parsed_en_route_min_results = int(en_route_direct_batch_min_results)
                if parsed_en_route_min_results > 0:
                    self._en_route_direct_batch_min_results = parsed_en_route_min_results
            except (TypeError, ValueError):
                self._en_route_direct_batch_min_results = DEFAULT_EN_ROUTE_DIRECT_BATCH_MIN_RESULTS

            en_route_detour_max_minutes = url_cfg.get(
                "en_route_detour_max_minutes",
                DEFAULT_EN_ROUTE_DETOUR_MAX_MINUTES,
            )
            try:
                parsed_en_route_detour_max_minutes = int(en_route_detour_max_minutes)
                if parsed_en_route_detour_max_minutes >= 0:
                    self._en_route_detour_max_minutes = parsed_en_route_detour_max_minutes
            except (TypeError, ValueError):
                self._en_route_detour_max_minutes = DEFAULT_EN_ROUTE_DETOUR_MAX_MINUTES

            en_route_detour_max_miles = url_cfg.get(
                "en_route_detour_max_miles",
                DEFAULT_EN_ROUTE_DETOUR_MAX_MILES,
            )
            try:
                parsed_en_route_detour_max_miles = float(en_route_detour_max_miles)
                if parsed_en_route_detour_max_miles >= 0:
                    self._en_route_detour_max_miles = parsed_en_route_detour_max_miles
            except (TypeError, ValueError):
                self._en_route_detour_max_miles = DEFAULT_EN_ROUTE_DETOUR_MAX_MILES

            self._en_route_require_detour_metadata = bool(
                url_cfg.get(
                    "en_route_require_detour_metadata",
                    DEFAULT_EN_ROUTE_REQUIRE_DETOUR_METADATA,
                )
            )

            direct_batch_authoritative = url_cfg.get(
                "direct_batch_authoritative",
                DEFAULT_DIRECT_BATCH_AUTHORITATIVE,
            )
            self._direct_batch_authoritative = bool(direct_batch_authoritative)

            direct_batch_html_capture_enabled = url_cfg.get(
                "direct_batch_html_capture_enabled",
                DEFAULT_DIRECT_BATCH_HTML_CAPTURE_ENABLED,
            )
            self._direct_batch_html_capture_enabled = bool(direct_batch_html_capture_enabled)

            direct_batch_html_capture_subdir = str(
                url_cfg.get(
                    "direct_batch_html_capture_subdir",
                    DEFAULT_DIRECT_BATCH_HTML_CAPTURE_SUBDIR,
                )
                or DEFAULT_DIRECT_BATCH_HTML_CAPTURE_SUBDIR
            ).strip()
            if direct_batch_html_capture_subdir:
                self._direct_batch_html_capture_subdir = direct_batch_html_capture_subdir

            raw_restaurant_denylist = url_cfg.get("restaurant_name_denylist", [])
            if isinstance(raw_restaurant_denylist, list):
                self._restaurant_name_denylist = frozenset(
                    str(v or "").strip().lower()
                    for v in raw_restaurant_denylist
                    if str(v or "").strip()
                )

            raw_url_domain_denylist = url_cfg.get("url_domain_denylist", [])
            if isinstance(raw_url_domain_denylist, list):
                self._url_domain_denylist = frozenset(
                    str(v or "").strip().lower().lstrip(".")
                    for v in raw_url_domain_denylist
                    if str(v or "").strip()
                )
        except Exception:
            # Keep defaults when config loading is unavailable in tests or runtime.
            return

    def _load_url_policy_allowlist(self) -> None:
        self._url_policy_allowlisted_urls = set()
        path = Path(self._url_policy_allowlist_path)
        if path.exists():
            try:
                for raw in path.read_text(encoding="utf-8").splitlines():
                    line = raw.strip()
                    if not line or line.startswith("#"):
                        continue
                    self._url_policy_allowlisted_urls.add(line)
            except Exception as exc:
                logger.warning("Failed to load URL policy allowlist '%s': %s", path, exc)

        auto_allow_from_output = bool(
            getattr(self, "_url_policy_auto_allow_from_output", DEFAULT_URL_POLICY_AUTO_ALLOW_FROM_OUTPUT)
        )
        if not auto_allow_from_output:
            return

        output_path = Path(getattr(self, "_url_policy_output_path", DEFAULT_URL_POLICY_OUTPUT_PATH))
        if not output_path.exists():
            return
        try:
            for url in self._extract_urls_from_html(output_path):
                self._url_policy_allowlisted_urls.add(url)
        except Exception as exc:
            logger.warning("Failed to load URL policy URLs from output '%s': %s", output_path, exc)

    @staticmethod
    def _extract_urls_from_html(path: Path) -> set[str]:
        urls: set[str] = set()
        text = path.read_text(encoding="utf-8", errors="ignore")
        for match in re.finditer(r'href\s*=\s*["\']([^"\']+)["\']', text, flags=re.IGNORECASE):
            candidate = match.group(1).strip()
            if candidate.lower().startswith(("http://", "https://")):
                urls.add(candidate)
        return urls

    def _persistent_cache_file(self) -> Path:
        return Path(getattr(self, "_persistent_cache_path", DEFAULT_PERSISTENT_CACHE_PATH))

    @staticmethod
    def _status_from_json(value: Any) -> int | str:
        if isinstance(value, int):
            return value
        return str(value or "")

    def _load_persistent_caches(self) -> None:
        if not bool(getattr(self, "_persistent_cache_enabled", DEFAULT_PERSISTENT_CACHE_ENABLED)):
            return

        cache_path = self._persistent_cache_file()
        if not cache_path.exists():
            return

        try:
            payload = json.loads(cache_path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                return

            # Capture every entry's birth timestamp in one generic pass before
            # the per-section loads below, so _save_persistent_caches can write
            # back the original age instead of renewing it to "now". Done here
            # rather than per-section so a newly added cache section cannot
            # forget to participate and silently become immortal. Entries the
            # TTL checks below then reject stay out of the in-memory caches, so
            # their presence in this map is inert.
            # Tests (and any other caller) may construct URLDiscoverer via
            # __new__, bypassing __init__; mirror the same defensive
            # ensure-the-member-exists pattern _save_persistent_caches uses.
            # Without it the AttributeError raised here would be swallowed by
            # this method's own except clause and silently abort the entire
            # load, not just the timestamp capture.
            if not hasattr(self, "_persistent_entry_ts"):
                self._persistent_entry_ts = {}
            for section_name, section_entries in payload.items():
                if not isinstance(section_entries, dict):
                    continue
                for entry_key, entry_value in section_entries.items():
                    if not isinstance(entry_key, str) or not isinstance(entry_value, dict):
                        continue
                    try:
                        self._persistent_entry_ts[(section_name, entry_key)] = float(
                            entry_value.get("ts", 0) or 0
                        )
                    except (TypeError, ValueError):
                        continue

            now = time.time()
            search_cutoff = now - (float(getattr(self, "_persistent_search_cache_ttl_hours", DEFAULT_PERSISTENT_SEARCH_CACHE_TTL_HOURS)) * 3600.0)
            page_cutoff = now - (float(getattr(self, "_persistent_page_text_cache_ttl_hours", DEFAULT_PERSISTENT_PAGE_TEXT_CACHE_TTL_HOURS)) * 3600.0)
            verify_cutoff = now - (float(getattr(self, "_persistent_verify_cache_ttl_hours", DEFAULT_PERSISTENT_VERIFY_CACHE_TTL_HOURS)) * 3600.0)

            for key, entry in (payload.get("search_results", {}) or {}).items():
                if not isinstance(key, str) or not isinstance(entry, dict):
                    continue
                ts = float(entry.get("ts", 0) or 0)
                if ts < search_cutoff:
                    continue
                rows = entry.get("results", [])
                if not isinstance(rows, list):
                    continue
                normalized = [dict(item) for item in rows if isinstance(item, dict)]
                if normalized:
                    self._search_results_cache[key] = normalized

            for key, entry in (payload.get("verify_results", {}) or {}).items():
                if not isinstance(key, str) or not isinstance(entry, dict):
                    continue
                ts = float(entry.get("ts", 0) or 0)
                if ts < verify_cutoff:
                    continue
                self._verify_url_cache[key] = (
                    bool(entry.get("ok", False)),
                    self._status_from_json(entry.get("status", "")),
                )

            for key, entry in (payload.get("page_text_results", {}) or {}).items():
                if not isinstance(key, str) or not isinstance(entry, dict):
                    continue
                ts = float(entry.get("ts", 0) or 0)
                if ts < page_cutoff:
                    continue
                status = self._status_from_json(entry.get("status", ""))
                ok = bool(entry.get("ok", False))
                # Dropped on the way in as well as refused on the way out. The
                # writer above stops adding these; every entry a previous
                # version already wrote is still on disk, still inherited, and
                # still deciding. Skipping it costs one real fetch and replaces
                # an inherited placeholder with an observation.
                if self.classify_link_liveness(key, ok, status) == LINK_LIVENESS_UNCHECKED:
                    continue
                self._page_text_cache[key] = (
                    ok,
                    status,
                    str(entry.get("text", "") or ""),
                )
                # Absent or empty `final_url` loads as `unchecked`, which is
                # what distinguishes an entry written before the redirect
                # ledger existed from one that says "not redirected". The old
                # writer stored "" for both "no redirect" and "never observed",
                # so an old entry cannot support the first claim; the new
                # writer stores the URL itself for a measured non-redirect, and
                # an empty string is never a URL.
                self._record_link_redirect(key, str(entry.get("final_url", "") or ""))

            geocode_cutoff = now - (float(getattr(self, "_persistent_geocode_cache_ttl_hours", DEFAULT_PERSISTENT_GEOCODE_CACHE_TTL_HOURS)) * 3600.0)
            if not hasattr(self, "_en_route_stop_geocode_cache"):
                self._en_route_stop_geocode_cache = {}
            for key, entry in (payload.get("en_route_geocode", {}) or {}).items():
                if not isinstance(key, str) or not isinstance(entry, dict):
                    continue
                ts = float(entry.get("ts", 0) or 0)
                if ts < geocode_cutoff:
                    continue
                lat = entry.get("lat")
                lng = entry.get("lng")
                if lat is None or lng is None:
                    continue
                try:
                    self._en_route_stop_geocode_cache[key] = (float(lat), float(lng))
                except (TypeError, ValueError):
                    continue

            alltrails_cutoff = now - (float(getattr(self, "_persistent_alltrails_cache_ttl_hours", DEFAULT_PERSISTENT_ALLTRAILS_CACHE_TTL_HOURS)) * 3600.0)
            if not hasattr(self, "_alltrails_fetch_cache"):
                self._alltrails_fetch_cache = {}
            for key, entry in (payload.get("alltrails_fetch_results", {}) or {}).items():
                if not isinstance(key, str) or not isinstance(entry, dict):
                    continue
                ts = float(entry.get("ts", 0) or 0)
                if ts < alltrails_cutoff:
                    continue
                # Only successful fetches are ever persisted (see save side), but
                # guard defensively in case of a hand-edited cache file.
                if not bool(entry.get("ok", False)):
                    continue
                self._alltrails_fetch_cache[key] = (
                    True,
                    self._status_from_json(entry.get("status", "")),
                    str(entry.get("text", "") or ""),
                )

            wayback_cutoff = now - (float(getattr(self, "_persistent_wayback_cache_ttl_hours", DEFAULT_PERSISTENT_WAYBACK_CACHE_TTL_HOURS)) * 3600.0)
            if not hasattr(self, "_wayback_fetch_cache"):
                self._wayback_fetch_cache = {}
            for key, entry in (payload.get("wayback_geo_fetch_results", {}) or {}).items():
                if not isinstance(key, str) or not isinstance(entry, dict):
                    continue
                ts = float(entry.get("ts", 0) or 0)
                if ts < wayback_cutoff:
                    continue
                # Only successful fetches are ever persisted (see save side), but
                # guard defensively in case of a hand-edited cache file.
                if not bool(entry.get("ok", False)):
                    continue
                self._wayback_fetch_cache[key] = (
                    True,
                    self._status_from_json(entry.get("status", "")),
                    str(entry.get("text", "") or ""),
                )

            harvest_cutoff = now - (float(getattr(self, "_persistent_harvest_cache_ttl_hours", DEFAULT_PERSISTENT_HARVEST_CACHE_TTL_HOURS)) * 3600.0)
            harvest_cache_by_section = HARVEST_CACHE_SECTIONS
            for section_name, attr_name in harvest_cache_by_section.items():
                if not hasattr(self, attr_name):
                    setattr(self, attr_name, {})
                target_cache = getattr(self, attr_name)
                for key, entry in (payload.get(section_name, {}) or {}).items():
                    if not isinstance(key, str) or not isinstance(entry, dict):
                        continue
                    ts = float(entry.get("ts", 0) or 0)
                    if ts < harvest_cutoff:
                        continue
                    rows = entry.get("rows", [])
                    if not isinstance(rows, list):
                        continue
                    normalized = [dict(item) for item in rows if isinstance(item, dict)]
                    if normalized:
                        target_cache[key] = normalized
                        # Remembered before any lookup happens, because after the
                        # first miss writes to the same dict there is no way to
                        # tell a restored entry from one this run just bought.
                        if not hasattr(self, "_harvest_preloaded_keys"):
                            self._harvest_preloaded_keys = {}
                        self._harvest_preloaded_keys.setdefault(section_name, set()).add(key)
        except Exception as exc:
            logger.info("Persistent cache load skipped due to read/parse error: %s", exc)

    def _save_persistent_caches(self) -> None:
        if not bool(getattr(self, "_persistent_cache_enabled", DEFAULT_PERSISTENT_CACHE_ENABLED)):
            return
        if not bool(getattr(self, "_persistent_cache_dirty", False)):
            return

        # Tests may construct URLDiscoverer via __new__; ensure cache members
        # exist before persistence walks them.
        if not hasattr(self, "_search_results_cache"):
            self._search_results_cache = {}
        if not hasattr(self, "_verify_url_cache"):
            self._verify_url_cache = {}
        if not hasattr(self, "_page_text_cache"):
            self._page_text_cache = {}
        if not hasattr(self, "_en_route_stop_geocode_cache"):
            self._en_route_stop_geocode_cache = {}
        if not hasattr(self, "_alltrails_fetch_cache"):
            self._alltrails_fetch_cache = {}
        if not hasattr(self, "_wayback_fetch_cache"):
            self._wayback_fetch_cache = {}
        harvest_cache_by_section = HARVEST_CACHE_SECTIONS
        for attr_name in harvest_cache_by_section.values():
            if not hasattr(self, attr_name):
                setattr(self, attr_name, {})

        cache_path = self._persistent_cache_file()
        cache_path.parent.mkdir(parents=True, exist_ok=True)

        # TTL cutoffs are computed on the LOAD side only. Save now writes each
        # entry's preserved birth timestamp (see _entry_ts below) and lets the
        # next load do the expiring, so the cutoffs that used to be recomputed
        # here were both unused and misleading.
        now = time.time()

        payload: dict[str, Any] = {
            "version": 1,
            "updated_at": now,
            "search_results": {},
            "verify_results": {},
            "page_text_results": {},
            "en_route_geocode": {},
            "alltrails_fetch_results": {},
            "wayback_geo_fetch_results": {},
            "direct_batch_harvest_alltrails": {},
            "direct_batch_harvest_attractions": {},
            "direct_batch_harvest_restaurants": {},
            "direct_batch_harvest_en_route": {},
        }

        def _entry_ts(section_name: str, entry_key: str) -> float:
            """Original birth timestamp for an entry, or now if first seen this
            run. Without this every save renewed every entry's age, so no TTL
            here could ever expire anything as long as builds kept happening --
            a dead URL stayed cached indefinitely. Note the previous
            `now if now >= cutoff else cutoff` guards were dead code: cutoff is
            `now - ttl`, so the condition was always true and always yielded
            `now`."""
            return float(
                getattr(self, "_persistent_entry_ts", {}).get((section_name, entry_key), now)
            )

        for key, results in list(self._search_results_cache.items()):
            payload["search_results"][key] = {
                "ts": _entry_ts("search_results", key),
                "results": [dict(item) for item in results if isinstance(item, dict)],
            }

        for key, result in list(self._verify_url_cache.items()):
            if not isinstance(result, tuple) or len(result) != 2:
                continue
            payload["verify_results"][key] = {
                "ts": _entry_ts("verify_results", key),
                "ok": bool(result[0]),
                "status": result[1],
            }

        for key, result in list(self._page_text_cache.items()):
            if not isinstance(result, tuple) or len(result) != 3:
                continue
            # Only persist entries that are an observation. A fetch the liveness
            # ledger classifies as `unchecked` -- a 401/403 block page, a
            # timeout, an SSL error, a cooldown synthetic -- says nothing about
            # the URL; it says something about this run's reach. Writing it to
            # disk turns a transient block into a durable answer that every
            # later run inherits, and the gates downstream cannot tell an
            # inherited placeholder from a measurement: they deliberately fail
            # open on a failed fetch, and `_redirect_target_lacks_item_relevance`
            # reads the absent redirect record such an entry carries as "this
            # URL did not redirect" rather than "nothing ever looked". So a URL
            # rejected by a run that reached it is accepted by a later run that
            # inherited a block, with no request made and nothing in the record
            # to explain the difference.
            #
            # The two caches below already refuse this for their own transient
            # failures -- the geocoder's "no result", AllTrails' DataDome block.
            # The generic page-text cache is the one that feeds the relevance
            # and redirect gates, and it was the one still freezing them in.
            if self.classify_link_liveness(key, bool(result[0]), result[1]) == LINK_LIVENESS_UNCHECKED:
                continue
            final_url = str(getattr(self, "_fetch_final_url_cache", {}).get(key, "") or "")
            payload["page_text_results"][key] = {
                "ts": _entry_ts("page_text_results", key),
                "ok": bool(result[0]),
                "status": result[1],
                "text": str(result[2] or "")[: int(DEFAULT_PERSISTENT_PAGE_TEXT_MAX_CHARS)],
                "final_url": final_url,
            }

        for key, result in list(self._en_route_stop_geocode_cache.items()):
            # Only persist confirmed coordinates -- a "no result" (None) is often
            # a transient Nominatim rate-limit/timeout outcome, not a durable
            # "this place doesn't exist" answer, so it must not be frozen in.
            if not isinstance(result, tuple) or len(result) != 2:
                continue
            payload["en_route_geocode"][key] = {
                "ts": _entry_ts("en_route_geocode", key),
                "lat": result[0],
                "lng": result[1],
            }

        for key, result in list(self._alltrails_fetch_cache.items()):
            # Only persist successful fetches -- caching a transient DataDome
            # block (401/403) would freeze that block state in across runs.
            if not isinstance(result, tuple) or len(result) != 3 or not result[0]:
                continue
            payload["alltrails_fetch_results"][key] = {
                "ts": _entry_ts("alltrails_fetch_results", key),
                "ok": True,
                "status": result[1],
                "text": str(result[2] or "")[: int(DEFAULT_PERSISTENT_PAGE_TEXT_MAX_CHARS)],
            }

        for key, result in list(self._wayback_fetch_cache.items()):
            # Only persist successful fetches -- a "no snapshot"/fetch-failure
            # result could be a transient archive.org hiccup rather than a
            # durable "this trail was never archived" answer, and freezing
            # that in would permanently block a fallback that might succeed
            # on a later run.
            if not isinstance(result, tuple) or len(result) != 3 or not result[0]:
                continue
            payload["wayback_geo_fetch_results"][key] = {
                "ts": _entry_ts("wayback_geo_fetch_results", key),
                "ok": True,
                "status": result[1],
                "text": str(result[2] or "")[: int(DEFAULT_PERSISTENT_PAGE_TEXT_MAX_CHARS)],
            }

        for section_name, attr_name in list(harvest_cache_by_section.items()):
            for key, rows in list(getattr(self, attr_name).items()):
                if not isinstance(rows, list) or not rows:
                    # Never freeze in an empty harvest as a durable negative
                    # result -- an empty batch is often a transient upstream
                    # hiccup, not "this destination has no attractions".
                    continue
                payload[section_name][key] = {
                    "ts": _entry_ts(section_name, key),
                    "rows": [dict(item) for item in rows if isinstance(item, dict)],
                }

        # Cost-audit finding (see docs/design/url-discovery-and-audit.md
        # "Search-Result Cache Audit"): real dipstick runs from tonight
        # (dipstick69/70/72's run-console.log) logged "Persistent cache save
        # skipped due to write error" -- WinError 32 (sharing violation),
        # WinError 2, and Errno 13 (permission denied), all on this exact
        # '.cache\\url_discovery\\persistent_cache.tmp' path. _discover_attractions
        # et al. run destinations concurrently via a ThreadPoolExecutor
        # (see the caller of _save_persistent_caches below), and
        # _mark_persistent_cache_dirty triggers a mid-run checkpoint save
        # from WHICHEVER worker thread happens to cross the write_every
        # threshold -- so two threads in the same process could race to
        # write_text()/replace() the exact same fixed tmp path at once,
        # which is precisely what a Windows sharing-violation/permission
        # error on that path looks like. A failed save doesn't just lose
        # that run's new results for next time -- while it's failing
        # silently, this run also never gets the benefit of a warm cache, no
        # matter how long DEFAULT_PERSISTENT_SEARCH_CACHE_TTL_HOURS is set to.
        #
        # Fixed two ways: (1) a dedicated lock (deliberately NOT the existing
        # _request_cache_lock -- at least one caller of
        # _mark_persistent_cache_dirty(), the grouped direct-batch harvest
        # path around line 5928, already holds _request_cache_lock while
        # calling it, and _mark_persistent_cache_dirty can itself trigger
        # this save; reusing that lock here would self-deadlock the very
        # first time a checkpoint save fires from inside that call site)
        # serializes the write+replace, so two threads in this process can
        # never collide on the same tmp path; (2) the tmp filename is unique
        # per save attempt (pid + thread id) as defense-in-depth against an
        # external process (antivirus, OneDrive, a second manual invocation)
        # transiently holding the shared path, with a short bounded retry
        # since that class of external lock is normally gone within
        # milliseconds.
        if not hasattr(self, "_persistent_cache_save_lock"):
            self._persistent_cache_save_lock = Lock()

        with self._persistent_cache_save_lock:
            tmp = cache_path.with_name(
                f"{cache_path.stem}.{os.getpid()}.{threading.get_ident()}.tmp"
            )
            last_exc: Exception | None = None
            for attempt in range(3):
                try:
                    tmp.write_text(json.dumps(payload, ensure_ascii=True), encoding="utf-8")
                    tmp.replace(cache_path)
                    self._persistent_cache_dirty = False
                    last_exc = None
                    break
                except Exception as exc:
                    last_exc = exc
                    if attempt < 2:
                        time.sleep(0.05 * (attempt + 1))
            if last_exc is not None:
                logger.info("Persistent cache save skipped due to write error: %s", last_exc)
                # Best-effort cleanup so a failed attempt doesn't litter the
                # cache directory with an orphaned per-attempt tmp file.
                try:
                    tmp.unlink(missing_ok=True)
                except Exception:
                    pass

    def _mark_persistent_cache_dirty(self) -> None:
        self._persistent_cache_dirty = True
        self._persistent_cache_pending_writes = int(getattr(self, "_persistent_cache_pending_writes", 0) or 0) + 1
        write_every = max(1, int(getattr(self, "_persistent_cache_write_every", 25) or 25))
        if self._persistent_cache_pending_writes >= write_every:
            self._save_persistent_caches()
            self._persistent_cache_pending_writes = 0

    @staticmethod
    def _normalize_reason_code(reason: str) -> str:
        code = re.sub(r"[^a-z0-9_]+", "_", str(reason or "").strip().lower())
        code = re.sub(r"_+", "_", code).strip("_")
        return code or "unknown"

    def _trace_id(self, *, kind: str, dest_name: str, item_name: str) -> str:
        seed = f"{kind}|{dest_name}|{item_name}".strip().lower()
        normalized = re.sub(r"[^a-z0-9]+", "-", seed).strip("-")
        return normalized[:120] if normalized else "entity-unknown"

    @staticmethod
    def _infer_discovery_source(reason_code: str) -> str:
        code = str(reason_code or "").strip().lower()
        if code.startswith("direct_batch_item_fanout"):
            return "direct_batch_item_fanout"
        if code.startswith("direct_batch"):
            return "direct_batch"
        if code.startswith("alltrails"):
            return "alltrails"
        if code.startswith("search"):
            return "search"
        if code.startswith("maps"):
            return "maps"
        if code.startswith("ai_candidate"):
            return "ai_candidate"
        if code.startswith("interest_filter"):
            return "interest_filter"
        return "other"

    # Canonical outcome states shared across all entity kinds.
    # "accepted"  — a usable URL (or deliberate no-URL entry) was produced.
    # "rejected"  — discovery found nothing defensible; item carries no link.
    # "filtered"  — item was removed from the list by a hard rule (interest,
    #               freshness, trail-miles threshold, etc.).
    # "demoted"   — trail reclassified as attraction; may still appear in output.
    # "skipped"   — deliberately bypassed (disabled trails, seed-override, etc.).
    _ACCEPTED_REASON_SUFFIXES: frozenset[str] = frozenset({
        "accepted", "preserved", "recovered",
        "seed_ai_candidate_recovered",
        "direct_batch_source_locked_maps_fallback_assigned",
        "nps_deterministic_accepted",
        "discovery_completed",
    })
    _REJECTED_REASON_SUFFIXES: frozenset[str] = frozenset({
        "no_match", "source_locked_no_match", "maps_fallback_only",
        "direct_batch_no_accepted_candidates", "direct_batch_empty",
        "direct_batch_candidate_rejected", "direct_batch_candidate_rejected_generic",
        "url_rejected",
    })
    _FILTERED_REASON_CODES: frozenset[str] = frozenset({
        "interest_filter_skipped", "interest_filter_removed",
        "entity_removed",
        "trail_links_disabled",
    })
    _DEMOTED_REASON_CODES: frozenset[str] = frozenset({
        "threshold_demoted_to_attraction",
    })
    _SKIPPED_REASON_CODES: frozenset[str] = frozenset({
        "seed_threshold_override",
    })

    @classmethod
    def _classify_disposition_outcome(cls, reason_code: str, url: str) -> str:
        """Return a canonical outcome state for a single decision event.

        States: accepted | rejected | filtered | demoted | skipped
        """
        code = str(reason_code or "").strip().lower()
        if code in cls._FILTERED_REASON_CODES:
            return "filtered"
        if code in cls._DEMOTED_REASON_CODES:
            return "demoted"
        if code in cls._SKIPPED_REASON_CODES:
            return "skipped"
        for suffix in cls._ACCEPTED_REASON_SUFFIXES:
            if code == suffix or code.endswith(f"_{suffix}"):
                return "accepted"
        for suffix in cls._REJECTED_REASON_SUFFIXES:
            if code == suffix or code.endswith(f"_{suffix}"):
                return "rejected"
        # Fall back to URL presence: if a URL was emitted the item was accepted.
        return "accepted" if url else "rejected"

    def _record_discovery_stat(self, dest_name: str, reason_code: str, source_code: str) -> None:
        if not hasattr(self, "_decision_stats_by_destination"):
            self._decision_stats_by_destination = {}
        if not hasattr(self, "_decision_source_stats_by_destination"):
            self._decision_source_stats_by_destination = {}
        if not hasattr(self, "_request_cache_lock"):
            self._request_cache_lock = Lock()
        normalized_dest = str(dest_name or "").strip()
        if not normalized_dest:
            normalized_dest = "unknown"
        code = self._normalize_reason_code(reason_code)
        source = self._normalize_reason_code(source_code)
        with self._request_cache_lock:
            dest_bucket = self._decision_stats_by_destination.setdefault(normalized_dest, {})
            dest_bucket[code] = int(dest_bucket.get(code, 0) or 0) + 1
            source_bucket = self._decision_source_stats_by_destination.setdefault(normalized_dest, {})
            source_bucket[source] = int(source_bucket.get(source, 0) or 0) + 1

    def _record_disposition_thread_event(
        self,
        *,
        trace_id: str,
        kind: str,
        dest_name: str,
        item_name: str,
        reason_code: str,
        source_code: str,
        message: str,
        rendered_url: str,
    ) -> None:
        if not hasattr(self, "_decision_threads_by_destination"):
            self._decision_threads_by_destination = {}
        if not hasattr(self, "_request_cache_lock"):
            self._request_cache_lock = Lock()

        normalized_dest = str(dest_name or "").strip() or "unknown"
        with self._request_cache_lock:
            self._decision_event_sequence = int(getattr(self, "_decision_event_sequence", 0) or 0) + 1
            seq = self._decision_event_sequence
            by_trace = self._decision_threads_by_destination.setdefault(normalized_dest, {})
            thread = by_trace.setdefault(trace_id, [])
            thread.append(
                {
                    "seq": seq,
                    "kind": str(kind or ""),
                    "destination": normalized_dest,
                    "item": str(item_name or ""),
                    "reason": reason_code,
                    "source": source_code,
                    "message": str(message or ""),
                    "url": str(rendered_url or ""),
                }
            )

    def _log_decision(
        self,
        *,
        kind: str,
        dest_name: str,
        item_name: str,
        reason: str,
        message: str,
        url: str = "",
        level: int = logging.INFO,
    ) -> None:
        trace_id = self._trace_id(kind=kind, dest_name=dest_name, item_name=item_name)
        reason_code = self._normalize_reason_code(reason)
        source_code = self._infer_discovery_source(reason_code)
        rendered_url = url or "(none)"
        logger.log(
            level,
            "  [%s|reason=%s] %s: %s -> %s",
            trace_id,
            reason_code,
            message,
            item_name,
            rendered_url,
        )
        self._record_discovery_stat(dest_name, reason_code, source_code)
        self._record_disposition_thread_event(
            trace_id=trace_id,
            kind=kind,
            dest_name=dest_name,
            item_name=item_name,
            reason_code=reason_code,
            source_code=source_code,
            message=message,
            rendered_url=("" if rendered_url == "(none)" else rendered_url),
        )

    def _summarize_entity_dispositions(
        self,
        *,
        kind: str,
        disposition_threads: dict[str, list[dict[str, Any]]],
    ) -> dict[str, Any]:
        """Build a per-item disposition summary for one entity kind.

        Outcomes use canonical states: accepted | rejected | filtered | demoted | skipped.
        Each item's final_outcome is the highest-priority outcome seen across all events
        for that item (accepted > skipped > demoted > filtered > rejected).
        """
        _OUTCOME_PRIORITY = {"accepted": 5, "skipped": 4, "demoted": 3, "filtered": 2, "rejected": 1}
        item_map: dict[str, dict[str, Any]] = {}
        disposition_counts: dict[str, int] = {
            "accepted": 0, "rejected": 0, "filtered": 0, "demoted": 0, "skipped": 0,
        }
        source_counts: dict[str, int] = {}

        target_kind = str(kind or "").lower()
        # "trail" events are tagged with kind="attraction" in the current event schema;
        # support both so trail summaries work transparently.
        trail_aliases = {"trail", "attraction"}

        for events in disposition_threads.values():
            if not isinstance(events, list):
                continue
            for event in events:
                if not isinstance(event, dict):
                    continue
                event_kind = str(event.get("kind", "") or "").lower()
                if target_kind == "trail":
                    if event_kind not in trail_aliases:
                        continue
                elif event_kind != target_kind:
                    continue

                name = str(event.get("item", "") or "").strip()
                source = str(event.get("source", "") or "").strip() or "other"
                reason = str(event.get("reason", "") or "").strip()
                url = str(event.get("url", "") or "").strip()
                if not name:
                    continue

                outcome = self._classify_disposition_outcome(reason, url)
                source_counts[source] = int(source_counts.get(source, 0) or 0) + 1

                if name not in item_map:
                    item_map[name] = {
                        "name": name,
                        "final_outcome": outcome,
                        "source": source,
                        "reasons": [reason] if reason else [],
                        "urls": [url] if url else [],
                    }
                else:
                    entry = item_map[name]
                    if _OUTCOME_PRIORITY.get(outcome, 0) > _OUTCOME_PRIORITY.get(entry["final_outcome"], 0):
                        entry["final_outcome"] = outcome
                        entry["source"] = source
                    if reason and reason not in entry["reasons"]:
                        entry["reasons"].append(reason)
                    if url and url not in entry["urls"]:
                        entry["urls"].append(url)

        items = list(item_map.values())
        for entry in items:
            outcome = entry["final_outcome"]
            disposition_counts[outcome] = int(disposition_counts.get(outcome, 0) or 0) + 1

        return {
            "kind": kind,
            "total": len(items),
            "disposition_counts": disposition_counts,
            "source_counts": source_counts,
            "items": items,
        }

    # ── Public entry point ───────────────────────────────────────────────────

    def is_search_circuit_open(self) -> bool:
        """Non-raising peek at the underlying GrokSearch circuit-breaker state,
        for callers (e.g. main.py's selective-retry gate) that want to skip
        firing a whole extra pass into a known-ongoing outage rather than
        discovering that per-destination as each retry fails fast."""
        return bool(
            hasattr(self, "_search")
            and self._search is not None
            and hasattr(self._search, "is_circuit_open")
            and self._search.is_circuit_open()
        )

    def discover_all(self, trip: dict[str, Any]) -> None:
        # The batch harvest builds its own restaurant list, so ai_content's
        # budget filter never sees those items. A "low-cost" Europe itinerary
        # published 15 restaurants at $$$$ -- Rutz, Tim Raue, Horvath, Ciel
        # Bleu -- because the filter ran upstream of the source that supplied
        # them. Fifth time a rule has been applied to one of two sources.
        self._trip_budget = ((trip or {}).get("trip") or {}).get("budget")

        # Places as a filter over our own candidates. Never a source, and no
        # field it returns is published -- see places_filter's module docstring.
        try:
            from generator.places_filter import PlacesBudgetFilter

            self._places_filter = PlacesBudgetFilter()
            if self._places_filter.available:
                logger.info("Places budget filter active")
        except Exception as exc:
            logger.warning("Places budget filter unavailable: %s", exc)
            self._places_filter = None

        destinations = trip.get("destinations", [])

        def _discover_one(dest: dict) -> None:
            name = dest["name"]
            ai = dest.get("ai_content", {})
            nps_code = dest.get("nps_park_code")
            origin_name = str(dest.get("_en_route_origin", "") or "")
            logger.info("URL discovery for '%s'…", name)
            # Parallelise the four independent URL categories within each destination
            with instrumented_pool("url_discovery_categories", 4) as inner:
                futs = [
                    inner.submit(self._discover_attractions, ai, name, nps_code, dest.get("dates"), dest.get("seeds", []), dest=dest),
                    inner.submit(self._discover_restaurants, ai, name, dest.get("dates"), dest),
                    inner.submit(
                        self._discover_en_route_stops,
                        ai,
                        name,
                        dest.get("dates"),
                        origin_name,
                        dest.get("_en_route_origin_lat"),
                        dest.get("_en_route_origin_lng"),
                        dest.get("lat"),
                        dest.get("lng"),
                        dest,
                    ),
                    inner.submit(self._discover_scenic_drives, dest, name, nps_code),
                    # Its own job rather than a tail of en-route discovery.
                    # It was the latter for one release and never ran once:
                    # en-route stops are a priced enrichment that is off by
                    # default, and that early return sits well above where
                    # the trail lookup had been appended. A leg's trail is
                    # not an en-route stop and must not share its switch.
                    inner.submit(
                        self._discover_leg_trail_link,
                        ai,
                        dest,
                        origin_name,
                        name,
                    ),
                ]
                for f in as_completed(futs):
                    f.result()
            if not hasattr(self, "_request_cache_lock"):
                self._request_cache_lock = Lock()
            with self._request_cache_lock:
                decision_counts = dict(getattr(self, "_decision_stats_by_destination", {}).get(name, {}))
                source_counts = dict(getattr(self, "_decision_source_stats_by_destination", {}).get(name, {}))
                raw_threads = dict(getattr(self, "_decision_threads_by_destination", {}).get(name, {}))

            disposition_threads: dict[str, list[dict[str, Any]]] = {
                trace: [dict(event) for event in events if isinstance(event, dict)]
                for trace, events in raw_threads.items()
                if isinstance(events, list)
            }
            event_count = sum(len(events) for events in disposition_threads.values())
            dest["_url_discovery"] = {
                "reason_counts": decision_counts,
                "source_counts": source_counts,
                "thread_count": len(disposition_threads),
                "event_count": event_count,
                "disposition_threads": disposition_threads,
                "restaurant_dispositions": self._summarize_entity_dispositions(
                    kind="restaurant",
                    disposition_threads=disposition_threads,
                ),
                "attraction_dispositions": self._summarize_entity_dispositions(
                    kind="attraction",
                    disposition_threads=disposition_threads,
                ),
                "trail_dispositions": self._summarize_entity_dispositions(
                    kind="trail",
                    disposition_threads=disposition_threads,
                ),
                "en_route_stop_dispositions": self._summarize_entity_dispositions(
                    kind="en_route_stop",
                    disposition_threads=disposition_threads,
                ),
                "scenic_drive_dispositions": self._summarize_entity_dispositions(
                    kind="scenic_drive",
                    disposition_threads=disposition_threads,
                ),
            }

            top_counts = sorted(decision_counts.items(), key=lambda row: row[1], reverse=True)
            summary_bits = ", ".join(f"{k}={v}" for k, v in top_counts[:6]) if top_counts else "none"
            logger.info("URL discovery summary for '%s': %s", name, summary_bits)

        # GH #68 multi-site grouping §4: origin resolution for the
        # per-destination "getting here" leg. Built once so a group_with
        # reference resolves regardless of list order (a grouped entry can
        # legally appear before its base in the manifest).
        dest_by_id: dict[str, Any] = {
            d.get("id"): d for d in destinations if isinstance(d, dict) and d.get("id")
        }
        # Tracks the most recent *ungrouped* destination -- the traveler's
        # actual physical base. A run of group_with entries never advances
        # this, so the first ungrouped destination after a group still
        # measures its own leg from the shared base rather than from
        # whichever grouped sibling happened to render last.
        last_physical_base: dict[str, Any] | None = None
        for idx, dest in enumerate(destinations):
            if not isinstance(dest, dict):
                continue
            origin_name = ""
            origin_lat = None
            origin_lng = None
            base_id = str(dest.get("group_with", "") or "").strip()
            base_dest = dest_by_id.get(base_id) if base_id else None
            if base_dest is not None:
                # Grouped entry: base -> entry is a day-trip/detour, never
                # previous-in-list -> entry (which could itself be another
                # grouped sibling and would silently chain distances
                # through it instead of measuring from the real base).
                origin_name = str(base_dest.get("name", "") or "").strip()
                origin_lat = base_dest.get("lat")
                origin_lng = base_dest.get("lng")
            elif last_physical_base is not None:
                origin_name = str(last_physical_base.get("name", "") or "").strip()
                origin_lat = last_physical_base.get("lat")
                origin_lng = last_physical_base.get("lng")
            elif idx > 0 and isinstance(destinations[idx - 1], dict):
                # No base tracking applies yet (e.g. the very first
                # destination) -- original adjacent-stop behavior, unchanged.
                origin_name = str(destinations[idx - 1].get("name", "") or "").strip()
                origin_lat = destinations[idx - 1].get("lat")
                origin_lng = destinations[idx - 1].get("lng")
            dest["_en_route_origin"] = origin_name
            dest["_en_route_origin_lat"] = origin_lat
            dest["_en_route_origin_lng"] = origin_lng
            if base_dest is None:
                # This (ungrouped) entry is now the physical base for
                # whatever follows, including the next ungrouped
                # destination after any grouped entries in between.
                last_physical_base = dest

        # Pre-populate per-destination direct-batch caches from grouped
        # multi-destination calls before the per-destination pass below runs,
        # so _discover_attractions (attractions + AllTrails trails) and
        # _discover_restaurants (via their _get_*_direct_batch_rows_for_destination
        # getters) see cache hits instead of firing individual calls -- see
        # _prefetch_grouped_direct_batch for the no-op-when-disabled and
        # fail-open-per-destination behavior.
        self._prefetch_grouped_direct_batch(destinations)

        with instrumented_pool("url_discovery_destinations", min(len(destinations), 3)) as pool:
            futures = [pool.submit(_discover_one, d) for d in destinations]
            for f in as_completed(futures):
                f.result()

        for dest in destinations:
            if isinstance(dest, dict):
                dest.pop("_en_route_origin", None)
                dest.pop("_en_route_origin_lat", None)
                dest.pop("_en_route_origin_lng", None)

        # Emitted at WARNING so it survives the --log-level warning that real
        # runs use. Without this, every paid fallback call lands in the
        # artifacts as `url_discovery_fallback:search` with no way to tell the
        # four call paths apart -- which is precisely how 66% of a run got
        # attributed to "the per-item website hunt" when only one path is
        # that. See cost-accounting-and-reduction.md section 8.3.
        sites = getattr(self, "_fallback_call_sites", None)
        if sites:
            breakdown = ", ".join(f"{k}={v}" for k, v in sorted(sites.items(), key=lambda i: -i[1]))
            logger.warning(
                "Paid fallback calls by call site (total %d): %s", sum(sites.values()), breakdown
            )

        self._save_persistent_caches()

    def _attach_secondary_maps_link(
        self, item: dict[str, Any], item_name: str, dest_name: str, kind: str
    ) -> None:
        """Attach a distinct, additive Google-Maps-search `maps_url` to an
        attraction/restaurant that already has a real, distinct primary
        source URL, so `_maps_corner_link_html` (html_assembler.py) has a
        genuinely distinct link to surface as the card's map-icon badge.

        Real production data (dipstick72, a full validation run) showed 0 of
        50 attraction cards and 0 of 61 restaurant cards ever rendered that
        badge, even though the render-side machinery is fully wired up and
        does work for en-route stops on the very same page (39 badge-map
        occurrences there). Root cause traced through this file: en-route
        stops always get an unconditional maps_url assigned from
        route-waypoint geocoding (or a query-text fallback when no geocode)
        *before* their `url` field is even decided -- see the
        `has_precise_geocode` block a few hundred lines below, in the
        en-route-stop resolution loop. Attractions and restaurants have no
        equivalent step: every `attr["maps_url"] = ...` / `rest["maps_url"]
        = ...` assignment in `_discover_attractions`/`_discover_restaurants`
        and in this method's own per-item loops only fires in the "no real
        source URL was found, a maps-search URL became the PRIMARY url
        itself" paths (where maps_url is deliberately set equal to url --
        `_maps_corner_link_html` correctly treats that as redundant and
        suppresses the badge). Restaurant discovery goes further and
        actively does `rest.pop("maps_url", None)` in every branch that
        finds a real, distinct URL (Google Maps place, TripAdvisor, official
        site, direct-batch row). Net effect: whenever a genuinely useful
        distinct primary link was found, no code path ever attached a
        separate maps_url alongside it, so the badge had nothing to show.

        Called once per item, after `audit_discovered_urls` has already
        settled on that item's final `url` for this pass (mirrors where the
        AllTrails coordinate-geo maps_url hook is inserted, just below, for
        the same "url already final" reason). Purely additive:
          - Skipped entirely when there's no primary url (an item with no
            verified source keeps whatever its own fail-closed logic upstream
            already decided -- e.g. category-style-activity/ambiguous-
            geography/policy-enforce omissions -- untouched).
          - Skipped for AllTrails trail urls: those get their own
            coordinate-based hook (`_alltrails_geo_maps_url`) with its own
            fail-closed/logging contract; duplicating a text-query fallback
            on top of (or instead of) that would blur which mechanism is
            responsible for a trail card's map link.
          - Skipped when the url's own policy class is already a Google Maps
            search/directions link -- that IS the "no real source, maps
            became primary" case this fix must not touch.
          - Skipped whenever `item["maps_url"]` is already non-empty, so an
            item that picked up a maps_url from any other mechanism (existing
            direct-batch row data, the AllTrails geo hook, a pre-existing
            fallback) is never double-processed or overwritten.

        Attractions/restaurants carry no geocode data at this point in the
        pipeline (only en-route stops get route-waypoint geocoding), so
        unlike the AllTrails geo hook's coordinate link, the only honest
        option here is the same name+destination Google-Maps-search-query
        convention `_maps_fallback_query_text` already builds everywhere
        else in this file.
        """
        url = str(item.get("url", "") or "").strip()
        if not url:
            return
        if self._is_alltrails_trail_url(url):
            return
        if self._classify_url_policy_class(url) in {"google_maps_search", "google_maps_dir"}:
            return
        if str(item.get("maps_url", "") or "").strip():
            return
        query_text = self._maps_fallback_query_text(item_name, dest_name)
        if not query_text:
            return
        fallback_url = f"https://www.google.com/maps/search/?api=1&query={quote(query_text)}"

        # A place_id link names one specific place; the search URL above only
        # describes it in words, and a common name can resolve elsewhere. Use
        # the precise form when a key is configured, and keep the search URL
        # when it is not -- which is the unconfigured default, so output is
        # unchanged for anyone without a key.
        #
        # A refusal disables the resolver loudly for the rest of the run rather
        # than aborting it: this is a *secondary* link on an item that already
        # has a good primary one, so a broken key must be impossible to miss
        # but must not cost the customer their guide.
        resolver = getattr(self, "_place_resolver", None)
        link_kind = "maps_search"
        if resolver is not None and resolver.enabled:
            try:
                precise_url = resolver.maps_url_for(query_text)
            except PlaceResolutionRefused as exc:
                resolver.disable(str(exc))
            else:
                if precise_url:
                    fallback_url = precise_url
                    link_kind = "maps_place_id"

        item["maps_url"] = fallback_url
        self._log_decision(
            kind=kind,
            dest_name=dest_name,
            item_name=item_name,
            reason=f"secondary_maps_link_attached:{link_kind}",
            message=(
                "attached name+destination Google Maps search link alongside "
                "distinct primary source URL, for the map-icon badge"
            ),
            url=fallback_url,
        )

    def _attach_place_id(self, item: dict[str, Any], item_name: str, dest_name: str) -> None:
        """Record a `place_id` on an item, without touching its maps_url.

        En-route stops take an unconditional coordinate maps_url from route
        geocoding, which is the precise form and stays. But a coordinate is
        labelled by Google's reverse geocoder, so a route panel reads
        "Millcreek 2nd post market, 5FGM+75" where the card says "Red Cliffs
        National Conservation Area Overlook" -- right pins, unreadable list.

        A place_id is precise AND carries the real name, so the route URL can
        pass `waypoint_place_ids` alongside readable waypoint names. Stored as
        its own field rather than folded into maps_url so the map badge keeps
        the coordinate it already had.

        Resolution is cached per query within a run and bounded by the
        resolver's own per-run call cap, so a repeated stop costs nothing and
        a runaway is impossible. A refusal disables the resolver for the rest
        of the run, exactly as _attach_secondary_maps_link does: this is an
        enhancement to a link the item already has, so a broken key must be
        loud but must not cost the customer their guide.
        """
        if not item_name or item.get("place_id"):
            return
        resolver = getattr(self, "_place_resolver", None)
        if resolver is None or not resolver.enabled:
            return
        # A geocode is better evidence of where a stop is than the name of the
        # destination its leg happens to end at. "Cumberland Plateau Asheville,
        # North Carolina" resolved to nothing -- the plateau is in Tennessee,
        # 250 miles away -- and "Blount Mansion Asheville, North Carolina"
        # resolved only because the name survives a wrong qualifier. Both are
        # the failure the waypoint code already documents as "Mossy Cave
        # Capitol Reef National Park".
        #
        # With a coordinate, ask for the bare name and let the bias say where.
        # Without one, fall back to the qualified name, which is still better
        # than an unqualified one.
        lat, lng = item.get("geocode_lat"), item.get("geocode_lng")
        has_geocode = isinstance(lat, (int, float)) and isinstance(lng, (int, float))
        query = item_name if has_geocode else self._maps_fallback_query_text(item_name, dest_name)
        if not query:
            return
        try:
            place_id = resolver.resolve(
                query,
                bias_lat=float(lat) if has_geocode else None,
                bias_lng=float(lng) if has_geocode else None,
            )
        except PlaceResolutionRefused as exc:
            resolver.disable(str(exc))
            return
        if place_id:
            item["place_id"] = place_id

    def audit_discovered_urls(self, trip: dict[str, Any]) -> None:
        """Strip low-confidence discovered URLs before HTML assembly.

        This is a final safety pass. Discovery should already be strict, but
        we remove anything that still looks weak so bad links do not reach the
        generated itinerary.
        """
        # Single-pass URL evidence prefetch: validate and fetch page text once
        # per unique non-AllTrails URL so downstream checks reuse cached results.
        self._prewarm_url_validation_cache(trip)

        for dest in trip.get("destinations", []):
            dest_name = dest.get("name", "")
            dest_dates = str(dest.get("dates", "") or "")
            seed_key_set = {
                re.sub(r"[^a-z0-9]+", " ", str(seed or "").lower()).strip()
                for seed in (dest.get("seeds", []) or [])
                if str(seed or "").strip()
            }
            en_route_seed_key_set = {
                re.sub(r"[^a-z0-9]+", " ", str(seed or "").lower()).strip()
                for seed in (dest.get("en_route_seeds", []) or [])
                if str(seed or "").strip()
            }
            ai = dest.get("ai_content", {}) if isinstance(dest.get("ai_content", {}), dict) else {}

            max_trail_miles = float(getattr(self, "_max_trail_miles", DEFAULT_MAX_TRAIL_MILES) or 0)
            eligible_attractions: list[dict[str, Any]] = []
            # Captured BEFORE any removal, so the trail-batch backfill below
            # can refill emptied slots without ever exceeding the count
            # ai_content already sized to attractions_per_day * day_count.
            original_attraction_count = len(ai.get("top_attractions", []) or [])
            for attr in ai.get("top_attractions", []) or []:
                attr_name = str(attr.get("name", "") or "")
                attr_key = re.sub(r"[^a-z0-9]+", " ", attr_name.lower()).strip()
                is_seed = bool(attr_key and attr_key in seed_key_set)
                attr["is_seed"] = is_seed
                attr_type = str(attr.get("type", "attraction") or "attraction").lower()
                attr_desc = str(attr.get("description", "") or "")
                attr_context = self._attraction_trail_context(attr)
                url = str(attr.get("url", "") or "").strip()
                trail_like = self._is_trail_like_attraction(attr_name, attr_type, attr_context) or self._is_alltrails_trail_url(url)
                # Prefer a trail-specific AllTrails URL the trail batch already
                # bought for this same item over whatever this category found.
                # Deliberately BEATS the incumbent rather than only filling a
                # gap: the links it displaces are generic park pages that pass
                # validation while promising a specific place (see
                # _cached_alltrails_batch_url_for_item for the measurement).
                if trail_like and not self._is_alltrails_trail_url(url):
                    batch_trail_url = self._cached_alltrails_batch_url_for_item(dest_name, dest_dates, attr_name)
                    if batch_trail_url:
                        self._log_decision(
                            kind="attraction",
                            dest_name=dest_name,
                            item_name=attr_name,
                            reason="alltrails_batch_url_preferred",
                            message=f"replacing {url or '(none)'} with already-harvested {batch_trail_url}",
                        )
                        url = batch_trail_url
                        # This URL is introduced BY the audit, so discovery
                        # never recorded retention evidence for it -- and the
                        # retention check a few lines below then judges it with
                        # no context and can discard it. The audit was
                        # replacing a link with a better one it had already
                        # bought, then rejecting its own replacement.
                        #
                        # Upheaval Dome, sw 2026-08-30: the trail batch had
                        # alltrails.com/trail/us/utah/upheaval-dome-via-crater-
                        # view-trail, the audit preferred it over an unrelated
                        # nps.gov photography-permit page, then discarded it and
                        # removed the item for having no verified URL.
                        #
                        # The trail batch already vetted this link when it
                        # harvested it, so the audit should not re-derive deep
                        # relevance blind. Recorded as shallow-relevance
                        # evidence rather than trusted outright: the AllTrails
                        # slug/confidence gates still apply.
                        self._remember_retention_evidence(
                            attr_name, batch_trail_url,
                            candidate=None, allow_shallow_relevance=True,
                        )
                direct_batch_authoritative_url = self._is_remembered_direct_batch_authoritative_url(url, attr_name)
                maps_url = str(attr.get("maps_url", "") or "").strip()
                if not maps_url and self._classify_url_policy_class(url) in {"google_maps_search", "google_maps_dir"}:
                    maps_url = url

                if self._is_uninterested_attraction(attr_name, attr_type, attr_desc, dest_dates):
                    self._record_registry_entity_removal(
                        dest,
                        section_target="top_attractions",
                        entity_class="trail" if trail_like else "attraction",
                        display_name=attr_name,
                        description=attr_desc,
                        rejection_reason="interest_filter_removed",
                    )
                    continue

                # PR-028: enforce max_trail_miles from AI description when page fetch is unavailable
                if trail_like and max_trail_miles > 0:
                    desc_text = (
                        str(attr.get("description", "") or "") + " " +
                        str(attr.get("practical_note", "") or "")
                    )
                    threshold_miles = self._extract_trail_miles(desc_text)
                    if threshold_miles is None and self._is_alltrails_trail_url(url):
                        ok, _status, page_text = self._fetch_page_text(url, timeout=8)
                        if ok and page_text:
                            threshold_miles = self._extract_trail_miles(page_text)
                    if threshold_miles is not None and threshold_miles > max_trail_miles and not is_seed:
                        logger.info(
                            "  Trail miles threshold exceeded for '%s' in '%s': %.1f mi > %.1f mi",
                            attr_name, dest_name, threshold_miles, max_trail_miles,
                        )
                        attr.pop("url", None)
                        attr["type"] = "attraction"
                        # A demoted trail should present as a plain attraction, not
                        # a hike whose link happened to be stripped -- clear the
                        # hike-specific fields (difficulty/elevation) that would
                        # otherwise still render hike badges (e.g. badge-hike-*)
                        # in html_assembler despite the type change above.
                        attr.pop("difficulty", None)
                        attr.pop("elevation_gain_feet", None)
                        threshold_note = self._build_trail_threshold_note(
                            miles=threshold_miles,
                            max_miles=max_trail_miles,
                        )
                        if threshold_note:
                            existing_note = str(attr.get("practical_note", "") or "").strip()
                            if existing_note:
                                if threshold_note.lower() not in existing_note.lower():
                                    attr["practical_note"] = f"{existing_note} {threshold_note}".strip()
                            else:
                                attr["practical_note"] = threshold_note
                        # Do not keep synthetic/non-single-target fallback links
                        # when demoting over-threshold trails.
                        attr.pop("url", None)
                        attr.pop("maps_url", None)
                        self._log_decision(
                            kind="attraction",
                            dest_name=dest_name,
                            item_name=attr_name,
                            reason="threshold_demoted_to_attraction",
                            message="trail-like attraction exceeded threshold and was demoted to non-hike attraction",
                            url="",
                        )
                        self._annotate_registry_url_decision(attr, rendered_url="", rejection_reason="threshold_demoted_to_attraction")
                        if self._keep_item_if_verified_or_seed(
                            dest, attr, attr_name,
                            is_seed=is_seed,
                            section_target="top_attractions",
                            entity_class="attraction",
                            kind="attraction",
                            dest_name=dest_name,
                        ):
                            eligible_attractions.append(attr)
                        continue
                    if threshold_miles is not None and threshold_miles > max_trail_miles and is_seed:
                        # A seed must never end up with zero link, but confidently
                        # pointing at the single most strenuous AllTrails variant
                        # (e.g. "The Narrows" resolving to the ~19-mile top-down
                        # wilderness-permit hike when most visitors mean the short
                        # bottom-up day-hike) is misleading. Prefer an official
                        # nps.gov page for the item first, when the destination has
                        # an NPS park code -- it typically covers the full picture
                        # (route variants, permits, safety/seasonal info) rather
                        # than one specific variant. Reuse the same NPS-preference
                        # search approach used for attraction discovery above
                        # (site_filter="nps.gov" / site:nps.gov/<code> site hint).
                        original_url = str(attr.get("url", "") or "")
                        nps_code = dest.get("nps_park_code")
                        nps_replacement_url: str | None = None
                        if nps_code:
                            nps_replacement_url = self._search_first(
                                _build_query_variants(attr_name, dest_name, "trail hike"),
                                site_filter="nps.gov",
                                site_hint=f"site:nps.gov/{nps_code}",
                                item_name=attr_name,
                                dest_name=dest_name,
                                allow_alltrails=False,
                            )
                        if nps_replacement_url:
                            attr["url"] = nps_replacement_url
                            if maps_url:
                                attr["maps_url"] = maps_url
                            self._log_decision(
                                kind="attraction",
                                dest_name=dest_name,
                                item_name=attr_name,
                                reason="seed_threshold_nps_fallback_preferred",
                                message=(
                                    "seed attraction exceeds trail threshold; preferred nps.gov page "
                                    f"over over-threshold AllTrails link (old url: {original_url})"
                                ),
                                url=nps_replacement_url,
                            )
                            # The nps.gov URL just assigned is a terminal decision for
                            # this attraction -- it must bypass the trail-specific
                            # AllTrails/maps URL gate directly below (which expects
                            # trail-like items to carry an AllTrails or maps URL and
                            # would otherwise strip this legitimate nps.gov link).
                            self._annotate_registry_url_decision(attr, rendered_url=nps_replacement_url)
                            eligible_attractions.append(attr)
                            continue
                        else:
                            self._log_decision(
                                kind="attraction",
                                dest_name=dest_name,
                                item_name=attr_name,
                                reason="seed_threshold_override",
                                message="seed attraction exceeds trail threshold but link retention allowed",
                                url=original_url,
                            )

                if trail_like and url and not self._is_alltrails_trail_url(url):
                    policy_class = self._classify_url_policy_class(url)
                    if policy_class in {"google_maps_search", "google_maps_dir"}:
                        if not is_seed:
                            attr.pop("url", None)
                        if maps_url:
                            attr["maps_url"] = maps_url
                        elif not is_seed:
                            attr.pop("maps_url", None)
                        self._annotate_registry_url_decision(
                            attr,
                            rendered_url=url if is_seed else "",
                            rejection_reason="" if is_seed else "url_rejected",
                        )
                    elif not self._title_claims_a_trail(attr_name):
                        # Trail-like by description but not by name: keep the
                        # official (e.g. nps.gov) page. AllTrails-first applies
                        # to things that call themselves trails; Balanced Rock
                        # and Upheaval Dome are a rock formation and a crater,
                        # classed trail_like and stripped of correct nps.gov
                        # pages purely for not being alltrails.com.
                        self._log_decision(
                            kind="attraction",
                            dest_name=dest_name,
                            item_name=attr_name,
                            reason="official_url_kept_for_non_trail_named_item",
                            message=(
                                "trail-like item whose name does not claim a trail; "
                                "keeping the official non-AllTrails link"
                            ),
                            url=url,
                        )
                        if maps_url:
                            attr["maps_url"] = maps_url
                        self._annotate_registry_url_decision(attr, rendered_url=url)
                    else:
                        self._log_rejected_url("attraction", dest_name, attr_name, url)
                        attr.pop("url", None)
                        if maps_url:
                            attr["maps_url"] = maps_url
                            self._annotate_registry_url_decision(attr, rendered_url="", rejection_reason="url_rejected")
                        else:
                            # No pre-existing maps_url to fall back on either --
                            # e.g. a misclassified trail-like item (real
                            # dipstick64 "Bryce Point": a viewpoint, trail_like
                            # only because its description mentions "a short
                            # walk") whose real, correctly-matched nps.gov page
                            # was recovered via the attraction direct-batch
                            # fallback (reason=trail_like_misclassified_
                            # attraction_batch_recovered) gets rejected here for
                            # not being an alltrails.com URL, leaving it with no
                            # link and no maps fallback at all. Assign the same
                            # safe Google-Maps-search fallback every other
                            # "no URL found" attraction gets (and the same
                            # fail-closed exceptions), instead of leaving it
                            # completely unverified.
                            attr.pop("maps_url", None)
                            fallback_query_url = (
                                "https://www.google.com/maps/search/?api=1&query="
                                f"{quote(self._maps_fallback_query_text(attr_name, dest_name))}"
                            )
                            self._assign_attraction_maps_fallback_or_fail_closed(
                                attr,
                                attr_name=attr_name,
                                dest_name=dest_name,
                                maps_fallback_url=fallback_query_url,
                            )
                            self._annotate_registry_url_decision(
                                attr,
                                rendered_url=str(attr.get("url", "") or ""),
                                rejection_reason="" if attr.get("url") else "url_rejected",
                            )
                    if self._keep_item_if_verified_or_seed(
                        dest, attr, attr_name,
                        is_seed=is_seed,
                        section_target="top_attractions",
                        entity_class="trail" if trail_like else "attraction",
                        kind="attraction",
                        dest_name=dest_name,
                    ):
                        eligible_attractions.append(attr)
                    continue
                evidence = self._recall_retention_evidence(attr_name, url)
                cleaned = self._retain_discovered_url(
                    url,
                    attr_name,
                    dest_name,
                    allow_alltrails=trail_like,
                    kind="attraction",
                    is_seed=is_seed,
                    item_description=str(attr.get("description", "") or ""),
                    candidate=evidence.get("candidate"),
                    allow_shallow_relevance=bool(evidence.get("allow_shallow_relevance")),
                )
                if cleaned != url:
                    self._log_rejected_url("attraction", dest_name, attr_name, url)
                    if cleaned:
                        attr["url"] = cleaned
                        if maps_url:
                            attr["maps_url"] = maps_url
                        self._annotate_registry_url_decision(attr, rendered_url=cleaned)
                    else:
                        attr.pop("url", None)
                        if maps_url:
                            attr["maps_url"] = maps_url
                        else:
                            attr.pop("maps_url", None)
                        self._annotate_registry_url_decision(attr, rendered_url="", rejection_reason="url_rejected")
                elif cleaned and self._is_alltrails_trail_url(cleaned):
                    # Accepted-as-is AllTrails trail link (cleared
                    # _retain_discovered_url unchanged, i.e. this is the point
                    # where an AllTrails trail URL has fully cleared this
                    # codebase's acceptance gates). Attach a real
                    # coordinate-based Google Maps link from the trail's own
                    # page JSON-LD `geo` field so the card's map icon takes
                    # the visitor to the actual trailhead rather than back to
                    # the same AllTrails page. Strictly additive: on any
                    # extraction failure _alltrails_geo_maps_url returns None
                    # and maps_url is left exactly as whatever the
                    # pre-existing fallback logic already produced.
                    geo_maps_url = self._alltrails_geo_maps_url(cleaned)
                    if geo_maps_url:
                        attr["maps_url"] = geo_maps_url
                        self._log_decision(
                            kind="attraction",
                            dest_name=dest_name,
                            item_name=attr_name,
                            reason="alltrails_geo_maps_url_attached",
                            message="attached coordinate-based Google Maps link from AllTrails trail page JSON-LD geo field",
                            url=geo_maps_url,
                        )
                    else:
                        # Visibility fix (2026-08-18): previously this branch
                        # logged nothing at all on failure, which is exactly
                        # why dipstick72's real 0/20-fires-in-production
                        # regression (root cause: archive.org's wayback
                        # availability lookup 429ing under ordinary request
                        # volume -- see _fetch_wayback_alltrails_text's
                        # docstring) was invisible in run-console.log and
                        # needed a live reproduction script to diagnose
                        # instead of a log grep. Strictly informational --
                        # does not touch maps_url/url, matches the
                        # fail-closed contract documented on
                        # _alltrails_geo_maps_url.
                        self._log_decision(
                            kind="attraction",
                            dest_name=dest_name,
                            item_name=attr_name,
                            reason="alltrails_geo_maps_url_unavailable",
                            message="no coordinate-based Google Maps link attached: direct fetch and Wayback Machine fallback both failed to yield a usable JSON-LD geo field",
                            url="",
                        )
                # Last resort before deletion: answer "where is it" with a free
                # geocode rather than buying a search that already failed.
                # Without this the item is removed by verified-link-or-seed --
                # 29 attractions went that way on the 2026-08-22 run.
                if (
                    str(getattr(self, "_fallback_mode", DEFAULT_FALLBACK_MODE) or "") == "geocode_maps"
                    and not is_seed
                    and not self._item_has_verified_url(attr)
                ):
                    dest_lat, dest_lng = (dest or {}).get("lat"), (dest or {}).get("lng")
                    viewbox = (
                        (float(dest_lat), float(dest_lng))
                        if isinstance(dest_lat, (int, float)) and isinstance(dest_lng, (int, float))
                        else None
                    )
                    geo_url = self._geocode_maps_url_for_item(attr_name, dest_name, viewbox)
                    if geo_url:
                        attr["url"] = geo_url
                        self._log_decision(
                            kind="attraction",
                            dest_name=dest_name,
                            item_name=attr_name,
                            reason="geocode_maps_url_attached",
                            message="no website found; resolved to a coordinate Maps link via free geocode",
                            url=geo_url,
                        )
                for field in ("url", "maps_url"):
                    existing = str(attr.get(field, "") or "")
                    if existing:
                        attr[field] = self.requalify_maps_query_url(existing, attr_name, dest_name)
                if self._keep_item_if_verified_or_seed(
                    dest, attr, attr_name,
                    is_seed=is_seed,
                    section_target="top_attractions",
                    entity_class="trail" if trail_like else "attraction",
                    kind="attraction",
                    dest_name=dest_name,
                ):
                    eligible_attractions.append(attr)

            self._backfill_attractions_from_trail_batch(
                dest=dest,
                ai=ai,
                dest_name=dest_name,
                dest_dates=dest_dates,
                eligible=eligible_attractions,
                original_count=original_attraction_count,
            )

            if len(eligible_attractions) != len(ai.get("top_attractions", []) or []):
                ai["top_attractions"] = eligible_attractions

            # See _attach_secondary_maps_link's docstring: every attraction's
            # primary url is now final for this pass, so attach a distinct
            # map-icon-badge maps_url wherever one is missing and useful.
            for attr in eligible_attractions:
                self._attach_secondary_maps_link(
                    attr, str(attr.get("name", "") or ""), dest_name, kind="attraction"
                )

            # Collect attraction URLs for PR-004: drive URL dedup
            attraction_urls: set[str] = {
                str(a.get("url", "") or "").strip()
                for a in (ai.get("top_attractions", []) or [])
                if str(a.get("url", "") or "").strip()
            }

            eligible_stops: list[dict[str, Any]] = []
            for stop in ai.get("getting_here", {}).get("en_route_stops", []) or []:
                stop_name = str(stop.get("name", "") or "")
                stop_key = re.sub(r"[^a-z0-9]+", " ", stop_name.lower()).strip()
                stop_is_seed = bool(
                    (stop_key and stop_key in en_route_seed_key_set) or stop.get("is_seed")
                )
                stop["is_seed"] = stop_is_seed
                url = str(stop.get("url", "") or "").strip()
                # What the stop arrived carrying. The write-back below compares
                # against THIS rather than against `url`, which several blocks
                # between here and there are entitled to reassign.
                original_url = url
                # An en-route stop can be a trail -- Canyon Overlook Trail is
                # one. Until now this path hard-coded allow_alltrails=False, so
                # a trail that happened to be classified as an en-route stop
                # could never carry an AllTrails link even when the trail batch
                # had already bought one for it, and fell back to a generic
                # park page. Mirror the attraction path's allow_alltrails=
                # trail_like instead of forbidding outright; a non-trail stop
                # still refuses AllTrails exactly as before.
                stop_trail_like = self._is_trail_like_attraction(
                    stop_name,
                    str(stop.get("type", "") or ""),
                    self._attraction_trail_context(stop),
                ) or self._is_alltrails_trail_url(url)
                if stop_trail_like and not self._is_alltrails_trail_url(url):
                    batch_trail_url = self._cached_alltrails_batch_url_for_item(dest_name, dest_dates, stop_name)
                    if batch_trail_url:
                        self._log_decision(
                            kind="en-route stop",
                            dest_name=dest_name,
                            item_name=stop_name,
                            reason="alltrails_batch_url_preferred",
                            message=f"replacing {url or '(none)'} with already-harvested {batch_trail_url}",
                        )
                        url = batch_trail_url
                direct_batch_authoritative_url = self._is_remembered_direct_batch_authoritative_url(url, stop_name)
                # "maps" mode: resolve the stop to a Google Maps link instead
                # of hunting a website. What the mode buys is not spending
                # searches on pages that mostly do not exist for a roadside
                # pullout -- it is about the hunt, not about refusing a page
                # already in hand. So a real source URL the batch has ALREADY
                # harvested wins, for the reason the AllTrails exemption here
                # gave first and which was never specific to AllTrails: it is
                # free, it is specific to the stop, and it is strictly more
                # useful than a pin. thetrustees.org/place/appleton-farms/
                # tells a rider the opening hours; 42.6479,-70.8543 does not.
                #
                # Only a Maps URL is displaced, which is the case the mode
                # exists for: a bare text query naming the stop is a guess,
                # and a coordinate at least resolves to one point.
                en_route_maps_mode = (
                    str(getattr(self, "_en_route_source", DEFAULT_EN_ROUTE_SOURCE) or "") == "maps"
                )
                url_is_a_real_page = bool(url) and not self._is_google_maps_candidate_url(url)
                if en_route_maps_mode and not self._is_alltrails_trail_url(url) and not url_is_a_real_page:
                    stop_lat, stop_lng = (dest or {}).get("lat"), (dest or {}).get("lng")
                    stop_viewbox = (
                        (float(stop_lat), float(stop_lng))
                        if isinstance(stop_lat, (int, float)) and isinstance(stop_lng, (int, float))
                        else None
                    )
                    maps_primary = self._en_route_maps_url(
                        stop, stop_name, dest_name, stop_viewbox
                    )
                    if maps_primary:
                        self._log_decision(
                            kind="en-route stop",
                            dest_name=dest_name,
                            item_name=stop_name,
                            reason="en_route_resolved_to_maps",
                            message=f"resolved to a Maps link instead of a website (was {url or '(none)'})",
                            url=maps_primary,
                        )
                        url = maps_primary
                maps_url = str(stop.get("maps_url", "") or "").strip()
                if not maps_url and self._classify_url_policy_class(url) in {"google_maps_search", "google_maps_dir"}:
                    maps_url = url
                cleaned = self._retain_discovered_url(
                    url,
                    stop_name,
                    dest_name,
                    allow_alltrails=stop_trail_like,
                    kind="en-route stop",
                    is_seed=stop_is_seed,
                    # A Maps link IS the intended answer in this mode, so it
                    # must not be rejected as a vague search result.
                    allow_google_maps_search=en_route_maps_mode,
                )
                # Two questions, one of which used to be asked for both.
                # "Did retention reject what it was handed" is `cleaned != url`
                # and governs the rejection log. "Does the stop still hold the
                # right value" is `cleaned != original_url` and governs the
                # write-back -- `url` is a local that the AllTrails-batch block
                # and the maps-mode block above are both entitled to reassign,
                # so a value they introduced and retention then accepted
                # unchanged satisfied the old single test of `cleaned != url`
                # and was never written. It was logged first, which is what
                # made it look applied: en_route_resolved_to_maps announced a
                # coordinate for Wickford Village and Stony Creek while the
                # page went on rendering the text query the direct batch had
                # left in `stop["url"]`.
                if cleaned != url:
                    self._log_rejected_url("en-route stop", dest_name, stop_name, url)
                if cleaned != original_url:
                    if cleaned:
                        stop["url"] = cleaned
                        stop["_url_assigned_by"] = "audit_retention_cleaned"
                        if maps_url:
                            stop["maps_url"] = maps_url
                        self._annotate_registry_url_decision(stop, rendered_url=cleaned)
                    else:
                        stop.pop("url", None)
                        if maps_url:
                            stop["maps_url"] = maps_url
                        else:
                            stop.pop("maps_url", None)
                        self._annotate_registry_url_decision(stop, rendered_url="", rejection_reason="url_rejected")
                # A place_id lets the route URL name this stop instead of
                # letting Google reverse-geocode its coordinate into whatever
                # business is nearest. Does not replace maps_url.
                self._attach_place_id(stop, stop_name, dest_name)
                # Final values for this stop: make any bare Maps text query
                # name its destination before it reaches the page.
                for field in ("url", "maps_url"):
                    existing = str(stop.get(field, "") or "")
                    if existing:
                        stop[field] = self.requalify_maps_query_url(existing, stop_name, dest_name)
                if self._keep_item_if_verified_or_seed(
                    dest, stop, stop_name,
                    is_seed=stop_is_seed,
                    section_target="en_route_stops",
                    entity_class="en_route_stop",
                    kind="en_route_stop",
                    dest_name=dest_name,
                    extra_verified=self._item_has_verified_route_geocode(stop),
                    extra_verified_reason="en_route_geocode_verified_kept",
                ):
                    eligible_stops.append(stop)
            gh_block = ai.get("getting_here", {})
            if isinstance(gh_block, dict) and len(eligible_stops) != len(gh_block.get("en_route_stops", []) or []):
                gh_block["en_route_stops"] = eligible_stops
                ai["getting_here"] = gh_block

            route_options_list = ai.get("getting_there", {}).get("route_options", []) or []
            for route_opt in route_options_list:
                opt_name = str(route_opt.get("title", "") or route_opt.get("name", "") or "")
                url = str(route_opt.get("url", "") or "").strip()
                cleaned = self._retain_discovered_url(
                    url,
                    opt_name,
                    dest_name,
                    allow_alltrails=False,
                    kind="getting_there route option",
                )
                if cleaned != url:
                    self._log_rejected_url("getting_there route option", dest_name, opt_name, url)
                    if cleaned:
                        route_opt["url"] = cleaned
                        self._annotate_registry_url_decision(route_opt, rendered_url=cleaned)
                    else:
                        route_opt.pop("url", None)
                        self._annotate_registry_url_decision(route_opt, rendered_url="", rejection_reason="url_rejected")

            # Real bug (published eval run): the Turquoise Trail National
            # Scenic Byway departure route option had a real, distinct
            # primary source URL (nsbfoundation.com) but no map-icon badge
            # at all -- unlike en-route stops, attractions, and restaurants,
            # nothing anywhere in this pipeline ever attached a maps_url to
            # a route option. See _attach_secondary_maps_link's docstring:
            # every route option's primary url is now final for this pass,
            # so attach a distinct map-icon-badge maps_url wherever one is
            # missing and useful, exactly like attractions/restaurants below.
            for route_opt in route_options_list:
                opt_name = str(route_opt.get("title", "") or route_opt.get("name", "") or "")
                self._attach_secondary_maps_link(route_opt, opt_name, dest_name, kind="route_option")

            eligible_restaurants: list[dict[str, Any]] = []
            for rest in ai.get("dinner_recommendations", []) or []:
                rest_name = str(rest.get("name", "") or "")
                url = str(rest.get("url", "") or "").strip()
                direct_batch_authoritative_url = self._is_remembered_direct_batch_authoritative_url(url, rest_name)
                maps_url = str(rest.get("maps_url", "") or "").strip()
                if not maps_url and self._classify_url_policy_class(url) in {"google_maps_search", "google_maps_dir"}:
                    maps_url = url
                cleaned = self._retain_discovered_url(
                    url,
                    rest_name,
                    dest_name,
                    allow_alltrails=False,
                    kind="restaurant",
                )
                if not cleaned and direct_batch_authoritative_url and not self._is_google_maps_candidate_url(url):
                    cleaned = url
                if cleaned != url:
                    self._log_rejected_url("restaurant", dest_name, rest_name, url)
                    if not cleaned:
                        self._log_decision(
                            kind="restaurant",
                            dest_name=dest_name,
                            item_name=rest_name,
                            reason="audit_url_rejected",
                            message="restaurant URL stripped by audit retention gate",
                            url=url,
                        )
                    if cleaned:
                        rest["url"] = cleaned
                        if maps_url:
                            rest["maps_url"] = maps_url
                        self._annotate_registry_url_decision(rest, rendered_url=cleaned)
                    else:
                        rest.pop("url", None)
                        if maps_url:
                            rest["maps_url"] = maps_url
                        else:
                            rest.pop("maps_url", None)
                        self._annotate_registry_url_decision(rest, rendered_url="", rejection_reason="url_rejected")
                if (not direct_batch_authoritative_url) and self._is_restaurant_ineligible(rest, dest_name):
                    logger.info(
                        "  Restaurant freshness gate removed '%s' in '%s'",
                        rest_name, dest_name,
                    )
                    self._record_registry_entity_removal(
                        dest,
                        section_target="dinner_recommendations",
                        entity_class="restaurant",
                        display_name=rest_name,
                        description=str(rest.get("description", "") or ""),
                        rejection_reason="entity_removed",
                    )
                    continue
                # Restaurants carry no seed concept anywhere in this codebase
                # (no manifest field, no is_seed tracking) -- every restaurant
                # is a non-seed item for policy purposes, so is_seed=False
                # unconditionally here.
                if self._keep_item_if_verified_or_seed(
                    dest, rest, rest_name,
                    is_seed=False,
                    section_target="dinner_recommendations",
                    entity_class="restaurant",
                    kind="restaurant",
                    dest_name=dest_name,
                ):
                    eligible_restaurants.append(rest)
            if len(eligible_restaurants) != len(ai.get("dinner_recommendations", []) or []):
                ai["dinner_recommendations"] = eligible_restaurants

            # See _attach_secondary_maps_link's docstring: every restaurant's
            # primary url is now final for this pass, so attach a distinct
            # map-icon-badge maps_url wherever one is missing and useful.
            for rest in eligible_restaurants:
                self._attach_secondary_maps_link(
                    rest, str(rest.get("name", "") or ""), dest_name, kind="restaurant"
                )

            for drive in dest.get("scenic_drives", []) or []:
                drive_name = str(drive.get("title", "") or "")
                url = str(drive.get("url", "") or "").strip()
                cleaned = self._retain_discovered_url(
                    url,
                    drive_name,
                    dest_name,
                    allow_alltrails=False,
                    kind="scenic drive",
                    item_description=str(drive.get("description", "") or ""),
                )
                # PR-004: reject drive URL when it duplicates an attraction URL
                if cleaned and cleaned in attraction_urls:
                    logger.info(
                        "  Scenic drive URL dedup (matches attraction): '%s' in '%s': %s",
                        drive_name, dest_name, cleaned,
                    )
                    cleaned = ""
                if cleaned != url:
                    self._log_rejected_url("scenic drive", dest_name, drive_name, url)
                    if not cleaned:
                        self._log_decision(
                            kind="scenic_drive",
                            dest_name=dest_name,
                            item_name=drive_name,
                            reason="audit_url_rejected",
                            message="scenic drive URL stripped by audit retention gate",
                            url=url,
                        )
                    if cleaned:
                        drive["url"] = cleaned
                        self._annotate_registry_url_decision(drive, rendered_url=cleaned)
                    else:
                        drive.pop("url", None)
                        self._annotate_registry_url_decision(drive, rendered_url="", rejection_reason="url_rejected")

            events = dest.get("cultural_events", {})
            if isinstance(events, dict):
                for event in events.get("events", []) or []:
                    event_name = str(event.get("name", "") or "")
                    url = str(event.get("url", "") or "").strip()
                    # dipstick73/74: cultural_events.py's _verify_event_urls
                    # deliberately assigns a Google-Maps-search fallback into
                    # this same "url" field for any event whose real URL was
                    # never found or was stripped as generic (see that
                    # method's docstring: "unlike every other content type
                    # ... which always falls back to a Google Maps search
                    # link"). But this audit pass re-validates every event
                    # url through the same strict retention gate every other
                    # category uses, and DEFAULT_URL_POLICY_BLOCKED_CLASSES
                    # blocks the "google_maps_search" class in "enforce" mode
                    # (config.yaml url_policy_mode) unless the caller opts in
                    # via allow_google_maps_search -- which this call site
                    # never did. That silently stripped the very fallback
                    # _verify_event_urls had just attached, leaving real,
                    # dated events (confirmed real St. George example:
                    # "I-15 Country Rock Music Festival") with no link at
                    # all. Restaurants/attractions/en-route stops avoid this
                    # exact trap by extracting a pre-existing maps-search URL
                    # into a separate maps_url field before this same retain
                    # call, so it survives even when the retain gate rejects
                    # it as the primary url -- mirror that here so the
                    # fallback is preserved for html_assembler._build_events
                    # to render (falls back to maps_url when url is empty).
                    maps_url = str(event.get("maps_url", "") or "").strip()
                    if not maps_url and self._classify_url_policy_class(url) in {"google_maps_search", "google_maps_dir"}:
                        maps_url = url
                    cleaned = self._retain_discovered_url(
                        url,
                        event_name,
                        dest_name,
                        allow_alltrails=False,
                        kind="event",
                    )
                    if cleaned != url:
                        self._log_rejected_url("event", dest_name, event_name, url)
                        if cleaned:
                            event["url"] = cleaned
                            if maps_url:
                                event["maps_url"] = maps_url
                            self._annotate_registry_url_decision(event, rendered_url=cleaned)
                        else:
                            event.pop("url", None)
                            if maps_url:
                                event["maps_url"] = maps_url
                            else:
                                event.pop("maps_url", None)
                            self._annotate_registry_url_decision(event, rendered_url="", rejection_reason="url_rejected")

            self._deduplicate_within_destination(dest)

        self._deduplicate_cross_destination_drives(trip)
        self._deduplicate_attractions_against_en_route_stops_tripwide(trip)

        # Last thing the audit does, because it must describe the links that
        # actually survived it rather than the ones it started with.
        liveness = self.link_liveness_report(trip)
        trip["_link_liveness"] = liveness
        counts = liveness["counts"]
        logger.info(
            "Link liveness: %d published, %d live, %d dead, %d unchecked (%.1f%%)%s",
            liveness["published_count"],
            counts[LINK_LIVENESS_LIVE],
            counts[LINK_LIVENESS_DEAD],
            counts[LINK_LIVENESS_UNCHECKED],
            liveness["unchecked_share"] * 100.0,
            (
                " -- unchecked concentrated on: "
                + ", ".join(f"{d}={n}" for d, n in list(liveness["unchecked_by_domain"].items())[:5])
            )
            if liveness["unchecked_by_domain"]
            else "",
        )

    def _retain_discovered_url(
        self,
        url: str,
        item_name: str,
        dest_name: str,
        *,
        allow_alltrails: bool,
        kind: str = "generic",
        candidate: dict[str, Any] | None = None,
        allow_shallow_relevance: bool = False,
        allow_google_maps_search: bool = False,
        is_seed: bool = False,
        item_description: str = "",
    ) -> str:
        # Remember the context this call had, so the audit pass can judge the
        # same URL on the same evidence. audit_discovered_urls takes `trip` as
        # its only input -- candidate rows are transient to discovery and were
        # never persisted -- so it could not pass `candidate` or
        # `allow_shallow_relevance`, and `deep_check = not
        # allow_shallow_relevance` silently read that absence as "be stricter".
        # Discovery and audit then reached opposite verdicts on one link:
        # Upheaval Dome's alltrails.com/trail/us/utah/upheaval-dome-via-crater-
        # view-trail was accepted during discovery and discarded during audit,
        # leaving the item with no URL at all.
        self._remember_retention_evidence(
            item_name, url, candidate=candidate,
            allow_shallow_relevance=allow_shallow_relevance,
        )
        # Cleared per call. _last_retention_rejection is instance state, so a
        # value left by a previous call would be attached to a later discard
        # that had nothing to do with it -- the first run reported exit 1
        # ("if not url") for items that plainly had URLs.
        self._last_retention_rejection = None
        if not url:
            return self._reject_retention(1)
        if self._is_url_domain_denied(url):
            logger.info("URL domain denylist hit for %s '%s': %s", kind, item_name, url)
            return self._reject_retention(2)
        lower = url.lower()
        allowlisted_urls = getattr(self, "_url_policy_allowlisted_urls", set())
        if url in allowlisted_urls:
            return url
        is_safe_fallback = any(lower.startswith(prefix) for prefix in SAFE_FALLBACK_URL_PREFIXES)
        if self._is_obviously_generic_url(lower):
            return self._reject_retention(3)
        # The trails switch, enforced at the single chokepoint every candidate
        # URL passes through. Guarding call sites one at a time failed four
        # times: three search entry points, then the direct batch, and the
        # 2026-08-23 Core run still resolved 29 AllTrails URLs -- 56% of
        # everything the paid fallback found -- because the general per-item
        # hunt passes allow_alltrails=True and nothing downstream reconciled
        # that with the category being off.
        if self._is_alltrails_trail_url(url) and bool(getattr(self, "_disable_trails", False)):
            return self._reject_retention(4)
        if not allow_alltrails and self._is_alltrails_trail_url(url):
            return self._reject_retention(5)
        if self._has_unescaped_whitespace(url):
            logger.info("URL rejected due to unescaped whitespace for %s '%s': %s", kind, item_name, url)
            return self._reject_retention(6)
        if self._is_incomplete_google_maps_place_url(url):
            logger.info("URL rejected incomplete google maps place link for %s '%s': %s", kind, item_name, url)
            return self._reject_retention(7)
        if self._is_deterministic_google_maps_place_url(url) and kind in {"attraction", "en-route stop", "en_route_stop"}:
            if self._looks_synthetic_google_maps_place_url(url):
                logger.info(
                    "Maps place URL rejected as synthetic for %s '%s': %s",
                    kind,
                    item_name,
                    url,
                )
                return self._reject_retention(8)
            # Require substantial entity-token overlap for deterministic place pages.
            item_tokens = self._significant_tokens(item_name)
            if item_tokens:
                ok, _status, page_html = self._fetch_page_text(url, timeout=8)
                if not ok or not page_html:
                    logger.info(
                        "Maps place URL rejected: unable to verify page content for %s '%s': %s",
                        kind,
                        item_name,
                        url,
                    )
                    return self._reject_retention(9)
                lower_html = page_html.lower()
                parsed = urlparse(url)
                place_label = ""
                path_l = (parsed.path or "").lower()
                if path_l.startswith("/maps/place/"):
                    place_label = unquote((parsed.path or "").split("/maps/place/", 1)[-1].split("/", 1)[0]).replace("+", " ")
                elif path_l.startswith("/place/"):
                    place_label = unquote((parsed.path or "").split("/place/", 1)[-1].split("/", 1)[0]).replace("+", " ")
                label_token_set = set(self._significant_tokens(place_label))
                required_overlap = self._required_general_token_matches(len(item_tokens))
                page_overlap = sum(1 for t in item_tokens if t in lower_html)
                label_overlap = sum(1 for t in item_tokens if t in label_token_set)
                if max(page_overlap, label_overlap) < required_overlap:
                    logger.info(
                        "Maps place URL rejected: weak entity token overlap for %s '%s': %s",
                        kind, item_name, url,
                    )
                    return self._reject_retention(10)
                generic_entity_tokens = {
                    "historic",
                    "district",
                    "museum",
                    "park",
                    "state",
                    "national",
                    "visitor",
                    "center",
                    "trail",
                    "hike",
                    "canyon",
                    "springs",
                }
                anchor_tokens = [t for t in item_tokens if t not in generic_entity_tokens]
                if anchor_tokens:
                    anchor_overlap = sum(
                        1
                        for t in anchor_tokens
                        if t in lower_html or t in label_token_set
                    )
                    if anchor_overlap < 1:
                        logger.info(
                            "Maps place URL rejected: no distinctive entity-token overlap for %s '%s': %s",
                            kind,
                            item_name,
                            url,
                        )
                        return self._reject_retention(11)
        if self._direct_batch_is_authoritative() and self._is_remembered_direct_batch_authoritative_url(url, item_name):
            logger.info(
                "Remembered authoritative direct-batch URL preserved for %s '%s' (%s): %s",
                kind,
                item_name or "unknown",
                dest_name or "unknown destination",
                url,
            )
            return url

        if (
            self._direct_batch_is_authoritative()
            and kind == "restaurant"
            and isinstance(candidate, dict)
            and self._direct_batch_row_matches_item(candidate, item_name, dest_name)
            and not self._is_google_maps_candidate_url(url)
        ):
            # This leniency is restaurant-specific per the documented direct-link-batch
            # authoritative contract (non-map snippet/source URLs may be accepted with
            # weak path tokens once the row matches). Other kinds (attraction, en-route
            # stop, ...) must still clear the dedicated generic-section-landing-page and
            # relevance gates below — those checks (not this restaurant-shaped pattern
            # list) are what catch a generic "things to do" listing page for that kind.
            parsed = urlparse(url)
            host_l = (parsed.netloc or "").lower()
            path_l = (parsed.path or "").lower()
            lower_url = (url or "").lower()
            obvious_area_listing = (
                "tripadvisor." in host_l and ("/restaurants-" in path_l or "restaurants-g" in lower_url)
                or "google.com/maps" in host_l
                or "maps.google.com" in host_l
                or "/restaurants/" in path_l
                or "restaurants-near" in lower_url
                or "best-restaurants-near" in lower_url
            )
            if not obvious_area_listing:
                # This leniency still must not bypass the hard URL-class policy gate
                # (e.g. social_media blocked in enforce mode) — item-row matching is
                # about entity relevance, not URL-class safety.
                leniency_policy_class = self._classify_url_policy_class(url)
                leniency_blocked_classes = getattr(self, "_url_policy_blocked_classes", set(DEFAULT_URL_POLICY_BLOCKED_CLASSES))
                leniency_policy_mode = getattr(self, "_url_policy_mode", DEFAULT_URL_POLICY_MODE)
                if leniency_policy_class in leniency_blocked_classes and leniency_policy_mode == "enforce":
                    logger.info(
                        "URL policy rejected [%s] for %s '%s' (%s): %s",
                        leniency_policy_class,
                        kind,
                        item_name or "unknown",
                        dest_name or "unknown destination",
                        url,
                    )
                    return self._reject_retention(12)
                # This leniency still must not publish a URL that is definitively
                # dead (404/410, or a DNS/connection failure meaning the host
                # doesn't exist at all). Relevance leniency is not a liveness
                # exemption -- a matched row pointing at a domain that fails to
                # resolve is not "close enough", it is a broken link.
                ok, fetch_status, fetch_text = self._fetch_page_text(url, timeout=8)
                if (
                    not ok
                    and self._is_definitively_dead_status(fetch_status)
                    and not self._is_bot_block_false_negative_dead_status(url, fetch_status)
                ):
                    logger.info(
                        "Rejected dead item-matched authoritative direct-batch URL for %s '%s' (%s): %s (%s)",
                        kind,
                        item_name or "unknown",
                        dest_name or "unknown destination",
                        url,
                        fetch_status,
                    )
                    return self._reject_retention(13)
                redirect_target = self._redirect_target_lacks_item_relevance(
                    url, item_name, dest_name, self._significant_tokens(item_name), kind, fetch_text
                )
                if redirect_target:
                    logger.info(
                        "Rejected item-matched authoritative direct-batch URL for %s '%s' (%s): redirects to generic page %s -> %s",
                        kind,
                        item_name or "unknown",
                        dest_name or "unknown destination",
                        url,
                        redirect_target,
                    )
                    return self._reject_retention(14)
                logger.info(
                    "Item-matched authoritative direct-batch URL preserved for %s '%s' (%s): %s",
                    kind,
                    item_name or "unknown",
                    dest_name or "unknown destination",
                    url,
                )
                return url

        if kind == "restaurant":
            if self._is_google_maps_candidate_url(url):
                logger.info(
                    "URL rejected google maps candidate for %s '%s': %s",
                    kind,
                    item_name,
                    url,
                )
                return self._reject_retention(15)
            _rest_tokens = self._restaurant_significant_tokens(item_name)
            if self._looks_like_item_specific_homepage(url, item_name, item_tokens=_rest_tokens):
                return url
            if self._is_generic_restaurant_landing_url(url, item_name, dest_name, item_tokens=_rest_tokens):
                logger.info(
                    "URL rejected generic restaurant landing page for %s '%s': %s",
                    kind,
                    item_name,
                    url,
                )
                return self._reject_retention(16)
        if kind in {"generic", "attraction", "en-route stop", "en_route_stop", "getting_there route option"}:
            if self._is_generic_section_landing_page(url):
                if not self._looks_like_item_specific_homepage(url, item_name):
                    logger.info(
                        "URL rejected generic section landing page for %s '%s': %s",
                        kind,
                        item_name,
                        url,
                    )
                    return self._reject_retention(17)
                # Falls through to the relevance gate so page text can confirm the entity.
        # AllTrails slug denylist: fast-reject known-invalid slugs before any fetch.
        if self._is_alltrails_trail_url(url):
            slug = urlparse(url).path.rsplit("/", 1)[-1].lower()
            if slug in getattr(self, "_alltrails_slug_denylist", frozenset()):
                logger.info("AllTrails slug denylist hit for %s '%s': %s", kind, item_name, url)
                return self._reject_retention(18)
        # Wikipedia entity-path check: wiki page name is deterministic in the URL.
        # Reject when no item token appears in the wiki slug (catches wrong-entity links).
        if not is_safe_fallback and "wikipedia.org/wiki/" in lower:
            wiki_slug = lower.split("/wiki/")[-1].split("?")[0].replace("_", " ").replace("-", " ")
            item_tokens = self._significant_tokens(item_name)
            if item_tokens and not any(t in wiki_slug for t in item_tokens):
                logger.info(
                    "Wikipedia entity-path mismatch for %s '%s': %s",
                    kind, item_name, url,
                )
                return self._reject_retention(19)
            generic_wiki_tokens = {
                "historic",
                "district",
                "national",
                "register",
                "listing",
                "listings",
                "county",
                "place",
                "places",
                "state",
                "city",
                "town",
                "utah",
            }
            distinctive_item_tokens = [
                t for t in item_tokens if t not in generic_wiki_tokens and len(t) >= 4
            ]
            if distinctive_item_tokens and not any(t in wiki_slug for t in distinctive_item_tokens):
                logger.info(
                    "Wikipedia entity-path generic-only overlap rejected for %s '%s': %s",
                    kind, item_name, url,
                )
                return self._reject_retention(20)
        # GH #59. Scoped to the kinds that name a thing inside a destination:
        # restaurants have their own generic-landing rule
        # (`_is_generic_restaurant_landing_url`), and an en-route stop is not
        # inside the destination at all -- it sits on the leg between two.
        #
        # Seeds are NOT exempt. A seed is a human saying "I want to see this",
        # which makes the link's correctness matter more rather than less; the
        # seed still survives verified-link-or-seed and renders without a link
        # rather than with one pointing at the wrong thing. Same trade the
        # trail-link fix made: a wrong page is worse than no page.
        if kind in {"generic", "attraction"} and self._is_the_destinations_own_page(url, item_name, dest_name):
            logger.info(
                "Destination's own page rejected as a specific %s link for '%s' (%s): %s",
                kind,
                item_name or "unknown",
                dest_name or "unknown destination",
                url,
            )
            return self._reject_retention(31)
        if kind in {"generic", "attraction"} and self._is_category_style_activity(item_name):
            if self._is_generic_geographic_url_for_category(url, item_name):
                logger.info(
                    "Category-style activity rejected generic geography URL for %s '%s': %s",
                    kind,
                    item_name,
                    url,
                )
                return self._reject_retention(21)
            if self._is_category_offer_listing_url(url):
                logger.info(
                    "Category-style activity rejected offer/listing URL for %s '%s': %s",
                    kind,
                    item_name,
                    url,
                )
                return self._reject_retention(22)
        if kind == "scenic drive" and not self._is_route_specific_scenic_drive_url(url):
            logger.info("Scenic-drive URL rejected (non-route target) for '%s': %s", item_name, url)
            return self._reject_retention(23)
        if allow_alltrails and self._is_alltrails_trail_url(url):
            if is_seed:
                # Seeds were attached via the relaxed seed standard (see
                # _search_alltrails_for_seed_relaxed / dipstick60 "The Narrows"
                # investigation), which intentionally skips the length/gain/
                # difficulty confidence gate below. Re-validating a seed's
                # already-attached AllTrails link against that stricter,
                # non-seed-aware gate here would silently undo the seed
                # exemption and discard a correct, explicitly-requested link.
                if not self._alltrails_url_meets_seed_relaxed_standard(url, item_name):
                    return self._reject_retention(24)
            else:
                if not self._meets_alltrails_publish_confidence(url, item_name, dest_name):
                    return self._reject_retention(25)
                if not self._passes_alltrails_post_search_filters(url, item_name, dest_name):
                    return self._reject_retention(26)
        if not is_safe_fallback:
            # Keep the direct-batch fail-closed rule narrow: only reject a curated
            # row when the URL is explicitly dead (404/410). Generic landing pages
            # and vague search results still need to clear the normal relevance gate.
            if (
                self._direct_batch_is_authoritative()
                and kind in {"attraction", "en-route stop", "en_route_stop", "restaurant"}
                and isinstance(candidate, dict)
                and not self._candidate_mentions_conflicting_destination(candidate, dest_name, item_name=item_name)
                and self._direct_batch_row_matches_item(candidate, item_name, dest_name)
            ):
                ok, status, fetch_text = self._fetch_page_text(url, timeout=8)
                if (
                    not ok
                    and self._is_definitively_dead_status(status)
                    and not self._is_bot_block_false_negative_dead_status(url, status)
                ):
                    logger.info(
                        "Rejected dead authoritative direct-batch candidate for %s '%s' (%s): %s",
                        kind,
                        item_name or "unknown",
                        dest_name or "unknown destination",
                        url,
                    )
                    return self._reject_retention(27)
                item_tokens = self._significant_tokens(item_name)
                # En-route stops sit along the route, not inside the
                # destination itself, so the deep relevance gate's
                # destination-token-on-page requirement below is inapplicable
                # to them by construction -- not just for single-word names.
                # Restricting this exemption to len(item_tokens) <= 1 alone
                # dropped real, row-matched, multi-word official pages
                # (dipstick67: "Corona Arch" -> blm.gov/visit/corona-arch-trail,
                # "Dead Horse Point State Park Overlook" -> stateparks.utah.gov)
                # purely because the page never repeats "Canyonlands National
                # Park" -- a park these stops are not actually part of.
                # Attractions/restaurants keep the stricter single-token-only
                # bar; only en-route stops get this wider exemption.
                is_multi_token_en_route_stop = (
                    kind in {"en-route stop", "en_route_stop"} and len(item_tokens) > 1
                )
                if (
                    (len(item_tokens) <= 1 or is_multi_token_en_route_stop)
                    and self._candidate_text_matches_item_tokens(candidate, item_tokens)
                ):
                    redirect_target = self._redirect_target_lacks_item_relevance(
                        url, item_name, dest_name, item_tokens, kind, fetch_text
                    )
                    if redirect_target:
                        logger.info(
                            "Rejected direct-batch %s %s '%s' (%s): redirects to generic page %s -> %s",
                            "row-matched en-route stop" if is_multi_token_en_route_stop else "single-token item",
                            kind,
                            item_name or "unknown",
                            dest_name or "unknown destination",
                            url,
                            redirect_target,
                        )
                        return self._reject_retention(28)
                    logger.info(
                        "Preserved direct-batch %s %s '%s' (%s): %s",
                        "row-matched en-route stop" if is_multi_token_en_route_stop else "single-token item",
                        kind,
                        item_name or "unknown",
                        dest_name or "unknown destination",
                        url,
                    )
                    return url

            deep_check = not allow_shallow_relevance
            if not self._is_relevant_result(
                url, item_name, dest_name, candidate=candidate, deep_check=deep_check,
                item_description=item_description,
            ):
                return self._reject_retention(29)

        policy_class = self._classify_url_policy_class(url)
        blocked_classes = getattr(self, "_url_policy_blocked_classes", set(DEFAULT_URL_POLICY_BLOCKED_CLASSES))
        policy_mode = getattr(self, "_url_policy_mode", DEFAULT_URL_POLICY_MODE)
        blocked = policy_class in blocked_classes
        if blocked and policy_mode == "enforce":
            if allow_google_maps_search and policy_class in {"google_maps_search", "google_maps_dir"}:
                if policy_class == "google_maps_search" and item_name:
                    # Real dipstick67 bug: the direct-batch harvest is AI-authored
                    # HTML, and its embedded Google Maps search links carry
                    # whatever query text the model happened to write -- e.g.
                    # "Sunrise Point Bryce Canyon National Park UT". Reproduced
                    # live: that exact query returns a multi-result disambiguation
                    # list (Sunrise Point tied against the unrelated, similarly-
                    # named "Sunset Point" and an out-of-state "Sunrise Point"
                    # cliff), because Google's parser treats the trailing bare
                    # state code as broadening the search rather than pinning it.
                    # Dropping the "UT" and keeping the same item+destination
                    # text resolves straight to the single correct place. Rather
                    # than trust arbitrary AI-authored query text verbatim,
                    # rebuild it here with the same controlled builder already
                    # used everywhere else a google_maps_search fallback URL is
                    # constructed from scratch -- item_name + dest_name, with no
                    # redundant state/zip suffix.
                    #
                    # En-route stops are a real, separately-documented case of
                    # this same problem (project owner: "Red Hollow Canyon...
                    # falls back to a Map link for the primary, but not one
                    # specific to that location"). The plain
                    # `_maps_fallback_query_text` builder used above omits the
                    # destination whenever the item name alone already looks
                    # "location-qualified" (contains a state name, "national
                    # park", etc.) or shares a token with the destination --
                    # heuristics tuned for attractions/restaurants, which live
                    # *inside* the destination itself. An en-route stop lives
                    # along the leg *between* two places and has no such
                    # association, so the same heuristic firing here produces
                    # an inconsistently under-qualified query purely based on
                    # the stop's own name shape, not on whether the query is
                    # actually specific enough. `_en_route_maps_fallback_query_text`
                    # is the existing, en-route-specific variant (already used
                    # for every other en-route maps fallback query built
                    # elsewhere in this file) that unconditionally appends
                    # "near {dest}" whenever the destination isn't already
                    # present -- reused here rather than duplicated.
                    #
                    # EXCEPTION (2026-08-22): this rebuild exists to sanitize
                    # AI-AUTHORED query text. A bare "lat,lng" query is not
                    # that -- it is a coordinate this code built itself from a
                    # geocode that `_prune_en_route_stops_by_geometry` already
                    # resolved and sanity-checked against the actual route. It
                    # is strictly more precise than any name query, so
                    # rebuilding it into one is a downgrade.
                    #
                    # Caught on the 2026-08-22 baseline run, where every
                    # coordinate link built by en_route_source: "maps" was
                    # silently rewritten into a name search here -- the mode
                    # still produced a Maps link, so it looked like it worked,
                    # while the precision it exists for was being discarded.
                    if self._is_coordinate_maps_query_url(url):
                        rebuilt_query = ""
                    elif kind in {"en-route stop", "en_route_stop"}:
                        rebuilt_query = self._en_route_maps_fallback_query_text(item_name, "", dest_name)
                    else:
                        rebuilt_query = self._maps_fallback_query_text(item_name, dest_name)
                    if rebuilt_query:
                        url = f"https://www.google.com/maps/search/?api=1&query={quote(rebuilt_query)}"
                logger.info(
                    "URL policy exception for direct-batch harvest [%s] for %s '%s' (%s): %s",
                    policy_class,
                    kind,
                    item_name or "unknown",
                    dest_name or "unknown destination",
                    url,
                )
            else:
                logger.info(
                    "URL policy rejected [%s] for %s '%s' (%s): %s",
                    policy_class,
                    kind,
                    item_name or "unknown",
                    dest_name or "unknown destination",
                    url,
                )
                return self._reject_retention(30)
        if blocked and policy_mode == "monitor":
            logger.info(
                "URL policy monitor hit [%s] for %s '%s' (%s): %s",
                policy_class,
                kind,
                item_name or "unknown",
                dest_name or "unknown destination",
                url,
            )
        return url

    def _is_url_domain_denied(self, url: str) -> bool:
        denylist = getattr(self, "_url_domain_denylist", frozenset())
        if not denylist:
            return False
        host = (urlparse(url).netloc or "").strip().lower()
        if not host:
            return False
        host = host.split(":", 1)[0].strip(".")
        if host.startswith("www."):
            host = host[4:]
        for blocked in denylist:
            normalized = (blocked or "").strip().lower().lstrip(".")
            if not normalized:
                continue
            if host == normalized or host.endswith(f".{normalized}"):
                return True
        return False

    @staticmethod
    def _classify_url_policy_class(url: str) -> str:
        lower = (url or "").lower()
        if "google.com/maps/dir/" in lower or "maps.google.com/maps/dir/" in lower:
            return "google_maps_dir"
        if "google.com/maps/search" in lower:
            return "google_maps_search"
        if "maps.google.com" in lower and ("?q=" in lower or "&q=" in lower or "?query=" in lower or "&query=" in lower):
            return "google_maps_search"
        if "google.com/search" in lower:
            return "google_search"
        if any(domain in lower for domain in ("facebook.com", "instagram.com", "tiktok.com", "x.com", "twitter.com")):
            return "social_media"
        if "alltrails.com" in lower:
            return "alltrails"
        return "general"

    @classmethod
    def _is_incomplete_google_maps_place_url(cls, url: str | None) -> bool:
        parsed = urlparse(str(url or "").strip())
        if not cls._is_google_maps_domain(parsed.netloc):
            return False
        path_l = (parsed.path or "").lower()
        query_l = (parsed.query or "").lower()
        if path_l.startswith("/maps/place/"):
            return not (("/data=" in path_l) or ("cid=" in query_l) or ("ftid=" in query_l))
        return False

    def _meets_alltrails_publish_confidence(self, url: str, item_name: str, dest_name: str) -> bool:
        threshold = str(
            getattr(
                self,
                "_alltrails_min_confidence_for_publish",
                "low",
            )
            or "low"
        ).strip().lower()
        ranks = {"low": 1, "medium": 2, "high": 3}
        confidence = self._alltrails_confidence_level(url, item_name, dest_name)
        return ranks.get(confidence, 1) >= ranks.get(threshold, 3)

    def _alltrails_confidence_level(self, url: str, item_name: str, dest_name: str) -> str:
        if not self._is_alltrails_trail_url(url):
            return "high"
        if not self._alltrails_slug_matches_item(url, item_name):
            return "low"
        if self._alltrails_slug_has_numbered_suffix(url):
            return "low"

        item_tokens = self._significant_tokens(item_name)
        slug_extra_terms = self._alltrails_slug_extra_term_count(url, item_name)

        ok, status, text = self._fetch_page_text(url, timeout=8)
        if ok:
            lower_text = (text or "").lower()
            if any(marker in lower_text for marker in ALLTRAILS_404_MARKERS):
                return "low"
            if self._text_matches_item_tokens(lower_text, item_tokens):
                return "high"
            if slug_extra_terms == 0:
                return self._boost_alltrails_confidence_via_corroboration("medium", url, item_name, dest_name)
            return self._corroborate_alltrails_slug_with_extra_terms(url, item_name, dest_name)

        if self._is_definitively_dead_status(status):
            return "low"

        # Secondary liveness probe for bot-blocked/sparse fetches: dead slugs can
        # still return 404/410 to HEAD/GET verification even when page text fetch
        # is blocked.
        verified_ok, verified_status = self._verify_url_cached(url)
        if not verified_ok and self._is_definitively_dead_status(verified_status):
            return "low"

        blocked = isinstance(status, int) and status in (401, 403)
        if blocked:
            # Blocked fetches are common; only strict slug matches qualify as medium.
            if slug_extra_terms == 0:
                return self._confidence_for_blocked_exact_slug_match(url, item_name, dest_name)
            return self._corroborate_alltrails_slug_with_extra_terms(url, item_name, dest_name)

        return "low"

    def _confidence_for_blocked_exact_slug_match(self, url: str, item_name: str, dest_name: str) -> str:
        """AllTrails bot-blocks (401/403) essentially all automated verification
        fetches, so for a slug that exactly matches the item name (no extra
        qualifier terms -- see _corroborate_alltrails_slug_with_extra_terms for
        that case) a blocked fetch alone can never distinguish "genuinely
        correct link" from "direct-batch-harvest link whose slug the AI
        invented" -- both look identical from here (403, slug text matches).

        This used to grant "medium" (sufficient to publish under the default
        alltrails_min_confidence_for_publish) purely from that slug match, with
        corroboration only ever able to *upgrade* a default "medium" to "high"
        on a successful search -- never able to *downgrade* an unverifiable
        claim. That is precisely the gap that let a direct-batch-harvested,
        bot-blocked, slug-matching AllTrails URL for "Water Tanks via Capitol
        Gorge" (Capitol Reef) publish and 404: the "liveness check" meant to
        catch a fabricated link was functionally disabled by AllTrails' own
        bot-blocking, which hits genuinely correct links just as often as
        fabricated ones.

        Corroboration is now actually required, not just optionally consulted,
        but calibrated in two tiers precisely so this doesn't just flip the bug
        to the opposite failure mode (losing genuinely correct links, which
        AllTrails also blocks routinely):

          1. The narrow, metadata-filtered corroboration search
             (_get_filtered_alltrails_selection) -- if an independent,
             differently-queried search agrees on this exact canonical URL
             *and* its full rating/reviews/difficulty/mileage metadata is
             extractable and within the family-hike policy, that's the
             strongest evidence available and promotes straight to "high"
             (unchanged from the existing medium->high boost behavior for
             candidates that already corroborate this way).
          2. If that narrow search doesn't produce a match -- common, since it
             requires every metadata field to be extractable from a search
             snippet and the trail to fall within the difficulty/mileage/
             review-count policy, so plenty of genuinely correct trails never
             clear it -- fall back to the broader, unfiltered
             site:alltrails.com search that _search_alltrails_for_trail()
             itself already treats as authoritative discovery in
             non-direct-batch mode. Landing on the same URL there is still
             real, independent corroboration -- just not "best trail"
             caliber -- so it earns "medium" (publish-eligible) rather than
             "high".
          3. Only when *neither* search corroborates the slug at all does this
             fall to "low" -- an independently-queried search never turned up
             so much as a hint of this exact page, which is the actual signal
             a fabricated slug produces that a bot-block alone cannot supply.

        Opt-in via the same _enable_filtered_alltrails_selection flag that
        gates the other corroboration paths in this file (this costs one or
        two extra search calls per borderline candidate); when disabled,
        preserves the original fail-open "medium" default so deployments that
        don't want the extra search cost keep the pre-existing behavior
        unchanged.
        """
        if not bool(getattr(self, "_enable_filtered_alltrails_selection", DEFAULT_ENABLE_FILTERED_ALLTRAILS_SELECTION)):
            return "medium"

        try:
            filtered_url = self._get_filtered_alltrails_selection(item_name=item_name, dest_name=dest_name)
        except Exception:
            filtered_url = None
        if filtered_url and self._same_alltrails_trail(url, filtered_url):
            self._log_decision(
                kind="attraction",
                dest_name=dest_name,
                item_name=item_name,
                reason="alltrails_confidence_boosted_by_corroboration",
                message="alltrails confidence promoted medium->high: independent filtered search agreed",
                url=url,
            )
            return "high"

        try:
            variants = _build_alltrails_query_variants(item_name, dest_name)
            broad_url = self._search_first(
                variants,
                site_filter="alltrails.com",
                item_name=item_name,
                dest_name=dest_name,
                max_attempts=min(len(variants), int(getattr(self, "_max_alltrails_query_attempts", 5) or 5)),
            )
        except Exception:
            broad_url = None
        if broad_url and self._same_alltrails_trail(url, broad_url):
            self._log_decision(
                kind="attraction",
                dest_name=dest_name,
                item_name=item_name,
                reason="alltrails_confidence_corroborated_by_broad_search",
                message="alltrails confidence granted medium: independent broad search agreed on same URL despite bot-block",
                url=url,
            )
            return "medium"

        self._log_decision(
            kind="attraction",
            dest_name=dest_name,
            item_name=item_name,
            reason="alltrails_confidence_denied_no_corroboration",
            message="alltrails bot-blocked slug-only match could not be corroborated by any independent search; treating as unverified",
            url=url,
        )
        return "low"

    def _corroborate_alltrails_slug_with_extra_terms(self, url: str, item_name: str, dest_name: str) -> str:
        """A slug carrying extra terms beyond the item name -- e.g. a route-variant
        qualifier like "top-down" on "the-narrows-top-down" for item "The Narrows"
        -- already passed the strict entity-identity check in
        _alltrails_slug_matches_item; the extra terms alone don't prove it's the
        wrong trail, they just make it too risky to grant a default "medium" the
        way slug_extra_terms==0 candidates get via _boost_alltrails_confidence_via_
        corroboration (that default-medium-then-maybe-upgrade shape is fine when
        there's no extra term, but for a multi-word variant slug a default medium
        for every candidate, corroborated or not, would reopen exactly the kind
        of wrong-trail leak Theme B fixed). So instead of defaulting to medium,
        require an independent, differently-queried search to land on the exact
        same URL before granting any confidence above "low" at all -- true
        corroboration, not a default with an optional upgrade. Fixes a real gap
        found in dipstick58 (Zion "The Narrows" -> the-narrows-top-down): a
        correct, harvested, slug-matched candidate was rejected outright because
        AllTrails' bot-blocked page fetch could never single-handedly confirm it
        and the extra "top down" qualifier disqualified it from even attempting
        corroboration.
        """
        if not bool(getattr(self, "_enable_filtered_alltrails_selection", DEFAULT_ENABLE_FILTERED_ALLTRAILS_SELECTION)):
            return "low"
        try:
            corroborated_url = self._get_filtered_alltrails_selection(item_name=item_name, dest_name=dest_name)
        except Exception:
            return "low"
        if corroborated_url and self._same_alltrails_trail(url, corroborated_url):
            self._log_decision(
                kind="attraction",
                dest_name=dest_name,
                item_name=item_name,
                reason="alltrails_confidence_boosted_by_corroboration",
                message="alltrails confidence promoted low->high: independent search agreed despite extra slug terms",
                url=url,
            )
            return "high"
        return "low"

    def _boost_alltrails_confidence_via_corroboration(
        self, base_confidence: str, url: str, item_name: str, dest_name: str
    ) -> str:
        """Corroboration signal: when the primary page-text/liveness check can only
        get to "medium" confidence (page fetch was inconclusive or blocked), an
        independent secondary lookup pointing at the exact same trail page is
        strong evidence the candidate is genuinely correct -- promote it to
        "high". This never *lowers* confidence below "medium" on its own
        initiative, but once corroboration is actually attempted (opt-in via
        the same config flag that gates the filtered-selection search) a
        failure to confirm now falls through to "low" rather than being left
        at the publish-eligible "medium" default.

        Fixes a real gap found in dipstick60 (Capitol Reef "Water Tanks via
        Capitol Gorge"): AllTrails' own bot-blocking means a 403'd liveness
        check can *never* affirmatively confirm a candidate, so leaving
        "medium" as a fail-open default let a slug that the direct-batch
        harvest LLM had outright invented -- it never appeared in any real
        AllTrails search result -- sail past the "medium" publish threshold on
        slug-text matching alone, with no independent source ever having
        looked at the URL. Requiring corroboration to affirmatively succeed
        (mirroring the stricter standard `_corroborate_alltrails_slug_with_extra_terms`
        already uses for extra-slug-term candidates, per the dipstick58 fix)
        closes that gap without touching the "high" (page text/corroboration
        confirmed) or "low" (confirmed dead/mismatched) cases, which were
        already correct.
        """
        if base_confidence != "medium":
            return base_confidence
        if not bool(getattr(self, "_enable_filtered_alltrails_selection", DEFAULT_ENABLE_FILTERED_ALLTRAILS_SELECTION)):
            return base_confidence
        try:
            corroborated_url = self._get_filtered_alltrails_selection(item_name=item_name, dest_name=dest_name)
        except Exception:
            corroborated_url = None
        if corroborated_url and self._same_alltrails_trail(url, corroborated_url):
            self._log_decision(
                kind="attraction",
                dest_name=dest_name,
                item_name=item_name,
                reason="alltrails_confidence_boosted_by_corroboration",
                message="alltrails confidence promoted medium->high: independent search agreed",
                url=url,
            )
            return "high"
        self._log_decision(
            kind="attraction",
            dest_name=dest_name,
            item_name=item_name,
            reason="alltrails_confidence_corroboration_failed",
            message="alltrails confidence demoted medium->low: independent search could not confirm a bot-blocked candidate",
            url=url,
        )
        return "low"

    def _is_restaurant_ineligible(self, rest: dict[str, Any], dest_name: str) -> bool:
        """Return True when a restaurant entry should be excluded from recommendations.

        Checks, in order:
        1. Name-based denylist (known-closed / pre-opening venues from config)
        2. Page-text closure and pre-opening markers from the discovered URL
        """
        name = str(rest.get("name", "") or "").strip()
        name_lower = name.lower()

        if name_lower in getattr(self, "_restaurant_name_denylist", frozenset()):
            logger.info("  Restaurant name denylist hit: '%s' (%s)", name, dest_name)
            return True

        url = str(rest.get("url", "") or "").strip()
        if not url or any(url.lower().startswith(p) for p in SAFE_FALLBACK_URL_PREFIXES):
            return False  # Cannot check status from a fallback/search URL

        try:
            ok, _status, text = self._fetch_page_text(url, timeout=6)
            if not ok or not text:
                return False
            text_lower = text.lower()
            for marker in RESTAURANT_CLOSURE_MARKERS:
                if marker in text_lower:
                    logger.info(
                        "  Restaurant closure marker '%s' found for '%s' (%s): %s",
                        marker, name, dest_name, url,
                    )
                    return True
            for marker in RESTAURANT_PRE_OPENING_MARKERS:
                if marker in text_lower:
                    logger.info(
                        "  Restaurant pre-opening marker '%s' found for '%s' (%s): %s",
                        marker, name, dest_name, url,
                    )
                    return True
        except Exception:
            pass
        return False

    def _deduplicate_within_destination(self, dest: dict[str, Any]) -> None:
        """Remove scenic_drives entries whose name tokens duplicate a top_attractions entry.

        When the same geographic entity appears in both top_attractions and scenic_drives
        (e.g., Dead Horse Point State Park as both an attraction and a viewpoint card),
        the scenic_drives entry is the weaker representation and is removed.
        """
        ai = dest.get("ai_content", {}) if isinstance(dest.get("ai_content", {}), dict) else {}
        attractions = ai.get("top_attractions", []) or []
        drives = dest.get("scenic_drives", []) or []
        if not attractions and not drives:
            return

        attr_token_sets: list[frozenset[str]] = []
        for attr in attractions:
            name = str(attr.get("name", "") or "")
            tokens = frozenset(self._significant_tokens(name))
            if len(tokens) >= 2:
                attr_token_sets.append(tokens)

        if drives and attr_token_sets:
            kept_drives: list[dict[str, Any]] = []
            for drive in drives:
                title = str(drive.get("title", "") or "")
                drive_tokens = frozenset(self._significant_tokens(title))
                if not drive_tokens:
                    kept_drives.append(drive)
                    continue

                duplicate = False
                for attr_tokens in attr_token_sets:
                    if not attr_tokens:
                        continue
                    overlap = len(attr_tokens & drive_tokens)
                    min_len = min(len(attr_tokens), len(drive_tokens))
                    if min_len >= 2 and overlap / min_len >= 0.8:
                        logger.info(
                            "  Within-destination dedup: removing scenic drive '%s' "
                            "(duplicates attraction in '%s')",
                            title,
                            dest.get("name", ""),
                        )
                        # Recorded so the entity registry (and, transitively,
                        # schedule reconciliation) can see this removal --
                        # without this the schedule could keep referencing a
                        # drive that silently vanished here.
                        self._record_registry_entity_removal(
                            dest,
                            section_target="scenic_drives",
                            entity_class="scenic_drive",
                            display_name=title,
                            rejection_reason="duplicate_of_attraction",
                            description=str(drive.get("description", "") or ""),
                        )
                        duplicate = True
                        break

                if not duplicate:
                    kept_drives.append(drive)

            dest["scenic_drives"] = kept_drives

        # Merge top_attractions entries that point at the exact same URL --
        # the AI-generated content and the direct-batch link harvest run
        # independently, so the same real place can surface twice under
        # different names that both resolve to the same canonical page
        # (dipstick55 Theme F: "Telluride Mountain Village" and "Telluride
        # Mountain Village Gondola" both resolved to
        # telluride.com/discover/the-gondola/; Bryce Canyon's "Inspiration
        # Point" and "Sunset and Inspiration Points via Rim Trail and Bryce
        # Canyon Path" both resolved to the same AllTrails page). An exact
        # URL match is about as high-confidence a "same place" signal as
        # exists -- unlike name-similarity heuristics, it has no false-
        # positive risk from two genuinely distinct places that just share a
        # word (e.g. a third, different "Lower, Mid, and Upper Inspiration
        # Points" AllTrails page legitimately survives this pass since its
        # URL differs).
        if attractions:
            by_url: dict[str, list[dict[str, Any]]] = {}
            for attr in attractions:
                url = str(attr.get("url", "") or "").strip()
                if not url:
                    continue
                by_url.setdefault(url, []).append(attr)

            def _keep_rank(a: dict[str, Any]) -> tuple[int, int, int]:
                has_desc = 1 if str(a.get("description", "") or "").strip() else 0
                has_rating = 1 if (a.get("rating") is not None or str(a.get("raw_rating", "") or "").strip()) else 0
                name_len = len(str(a.get("name", "") or ""))
                # Prefer richer metadata, then a shorter (more likely
                # canonical) name.
                return (has_desc, has_rating, -name_len)

            to_remove_ids: set[int] = set()
            for url, group in by_url.items():
                if len(group) < 2:
                    continue

                best = max(group, key=_keep_rank)
                for attr in group:
                    if attr is best:
                        continue
                    to_remove_ids.add(id(attr))
                    logger.info(
                        "  Within-destination dedup: removing attraction '%s' "
                        "(duplicates '%s' via shared URL %s)",
                        attr.get("name", ""),
                        best.get("name", ""),
                        url,
                    )
                    self._record_registry_entity_removal(
                        dest,
                        section_target="top_attractions",
                        entity_class=(
                            "trail"
                            if self._is_trail_like_attraction(
                                str(attr.get("name", "") or ""),
                                str(attr.get("type", "") or ""),
                                str(attr.get("description", "") or ""),
                            )
                            else "attraction"
                        ),
                        display_name=str(attr.get("name", "") or ""),
                        rejection_reason="duplicate_of_attraction_same_url",
                        description=str(attr.get("description", "") or ""),
                    )

            if to_remove_ids:
                attractions = [attr for attr in attractions if id(attr) not in to_remove_ids]
                ai["top_attractions"] = attractions

        # Remove attraction cards that duplicate a retained en-route stop.
        getting_here = ai.get("getting_here", {}) if isinstance(ai.get("getting_here", {}), dict) else {}
        en_route_stops = getting_here.get("en_route_stops", []) or []
        stop_token_sets: list[frozenset[str]] = []
        for stop in en_route_stops:
            stop_name = str(stop.get("name", "") or "")
            tokens = frozenset(self._significant_tokens(stop_name))
            if len(tokens) >= 2:
                stop_token_sets.append(tokens)

        if stop_token_sets and attractions:
            kept_attractions: list[dict[str, Any]] = []
            for attr in attractions:
                attr_name = str(attr.get("name", "") or "")
                attr_tokens = frozenset(self._significant_tokens(attr_name))
                if len(attr_tokens) < 2:
                    kept_attractions.append(attr)
                    continue

                duplicate_stop = False
                for stop_tokens in stop_token_sets:
                    overlap = len(attr_tokens & stop_tokens)
                    min_len = min(len(attr_tokens), len(stop_tokens))
                    if min_len >= 2 and overlap >= 2 and overlap / min_len >= 0.67:
                        logger.info(
                            "  Within-destination dedup: removing attraction '%s' "
                            "(duplicates en-route stop in '%s')",
                            attr_name,
                            dest.get("name", ""),
                        )
                        # Recorded so the entity registry (and, transitively,
                        # schedule reconciliation) can see this removal --
                        # without this the schedule could keep referencing an
                        # attraction that silently vanished here.
                        self._record_registry_entity_removal(
                            dest,
                            section_target="top_attractions",
                            entity_class=(
                                "trail"
                                if self._is_trail_like_attraction(
                                    attr_name, str(attr.get("type", "") or ""), str(attr.get("description", "") or "")
                                )
                                else "attraction"
                            ),
                            display_name=attr_name,
                            rejection_reason="duplicate_of_en_route_stop",
                            description=str(attr.get("description", "") or ""),
                        )
                        duplicate_stop = True
                        break

                if not duplicate_stop:
                    kept_attractions.append(attr)

            ai["top_attractions"] = kept_attractions

    def _deduplicate_cross_destination_drives(self, trip: dict[str, Any]) -> None:
        """Remove scenic drives that duplicate attractions in other destinations.

        This prevents cross-destination concept duplication like "Kolob Canyons Road"
        appearing as a scenic drive in one stop while "Kolob Canyons" is a primary
        attraction in another stop.
        """
        destinations = trip.get("destinations", []) or []
        if len(destinations) < 2:
            return

        attraction_token_sets_by_dest: list[list[frozenset[str]]] = []
        for dest in destinations:
            ai = dest.get("ai_content", {}) if isinstance(dest.get("ai_content", {}), dict) else {}
            token_sets: list[frozenset[str]] = []
            for attr in ai.get("top_attractions", []) or []:
                tokens = frozenset(self._significant_tokens(str(attr.get("name", "") or "")))
                if len(tokens) >= 2:
                    token_sets.append(tokens)
            attraction_token_sets_by_dest.append(token_sets)

        for idx, dest in enumerate(destinations):
            drives = dest.get("scenic_drives", []) or []
            if not drives:
                continue
            kept: list[dict[str, Any]] = []
            for drive in drives:
                drive_tokens = frozenset(self._significant_tokens(str(drive.get("title", "") or "")))
                if len(drive_tokens) < 2:
                    kept.append(drive)
                    continue

                duplicate_elsewhere = False
                for other_idx, token_sets in enumerate(attraction_token_sets_by_dest):
                    if other_idx == idx:
                        continue
                    for attr_tokens in token_sets:
                        overlap = len(drive_tokens & attr_tokens)
                        min_len = min(len(drive_tokens), len(attr_tokens))
                        if min_len >= 2 and overlap / min_len >= 0.8:
                            logger.info(
                                "  Cross-destination dedup: removing scenic drive '%s' in '%s' "
                                "(duplicates attraction in '%s')",
                                drive.get("title", ""),
                                dest.get("name", ""),
                                destinations[other_idx].get("name", ""),
                            )
                            duplicate_elsewhere = True
                            break
                    if duplicate_elsewhere:
                        break

                if not duplicate_elsewhere:
                    kept.append(drive)

            dest["scenic_drives"] = kept

    def _deduplicate_attractions_against_en_route_stops_tripwide(self, trip: dict[str, Any]) -> None:
        """Remove attraction cards that duplicate any retained en-route stop across the trip.

        This prevents the same entity from being presented both as a destination
        attraction and as a transfer-leg stop in another destination card.
        """
        destinations = trip.get("destinations", []) or []
        if len(destinations) < 2:
            return

        stop_token_sets: list[frozenset[str]] = []
        for dest in destinations:
            ai = dest.get("ai_content", {}) if isinstance(dest.get("ai_content", {}), dict) else {}
            getting_here = ai.get("getting_here", {}) if isinstance(ai.get("getting_here", {}), dict) else {}
            stops = getting_here.get("en_route_stops", []) if isinstance(getting_here.get("en_route_stops", []), list) else []
            for stop in stops:
                if not isinstance(stop, dict):
                    continue
                stop_name = str(stop.get("name", "") or "")
                tokens = frozenset(self._significant_tokens(stop_name))
                if len(tokens) >= 2:
                    stop_token_sets.append(tokens)

        if not stop_token_sets:
            return

        for dest in destinations:
            ai = dest.get("ai_content", {}) if isinstance(dest.get("ai_content", {}), dict) else {}
            attractions = ai.get("top_attractions", []) if isinstance(ai.get("top_attractions", []), list) else []
            if not attractions:
                continue

            kept_attractions: list[dict[str, Any]] = []
            for attr in attractions:
                if not isinstance(attr, dict):
                    kept_attractions.append(attr)
                    continue
                attr_name = str(attr.get("name", "") or "")
                attr_tokens = frozenset(self._significant_tokens(attr_name))
                if len(attr_tokens) < 2:
                    kept_attractions.append(attr)
                    continue

                duplicate_stop = False
                for stop_tokens in stop_token_sets:
                    overlap = len(attr_tokens & stop_tokens)
                    min_len = min(len(attr_tokens), len(stop_tokens))
                    if min_len >= 2 and overlap >= 2 and overlap / min_len >= 0.67:
                        logger.info(
                            "  Cross-destination dedup: removing attraction '%s' in '%s' "
                            "(duplicates en-route stop elsewhere)",
                            attr_name,
                            dest.get("name", ""),
                        )
                        duplicate_stop = True
                        break

                if not duplicate_stop:
                    kept_attractions.append(attr)

            ai["top_attractions"] = kept_attractions

    @staticmethod
    def _is_route_specific_scenic_drive_url(url: str) -> bool:
        parsed = urlparse(url or "")
        path_and_query = f"{parsed.path or ''} {(parsed.query or '')}".lower()
        route_markers = (
            "scenic-drive",
            "scenic_drives",
            "scenic-drives",
            "byway",
            "route",
            "highway",
            "road",
            "drive",
        )
        return any(marker in path_and_query for marker in route_markers)

    @staticmethod
    def _title_claims_a_trail(item_name: str) -> bool:
        """True when the item's own name says it is a trail.

        The AllTrails-first rule for trail-like items exists because
        non-AllTrails trail URLs were being generated badly (owner decision).
        It keyed on `trail_like`, which is inferred from the description, so a
        rock formation whose blurb mentions a short walk was held to it too --
        and its correct nps.gov page was stripped for not being AllTrails.

        Owner rule, 2026-08-29: when a trail-type target has both available,
        prefer AllTrails if the title contains "trail", otherwise prefer NPS.
        The name is the item's own claim about what it is; the description is
        someone describing how to reach it.

        Matched on a word boundary rather than as a substring. Read literally
        the rule sends "Trailview Overlook" to AllTrails, which is an overlook
        that happens to start with those five letters -- the intent is an item
        that calls itself a trail. "Trails" plural is accepted.
        """
        return bool(re.search(TRAIL_NAME_PATTERN, str(item_name or "").lower()))

    @staticmethod
    def _retention_evidence_key(item_name: str, url: str) -> tuple[str, str]:
        # Punctuation removed rather than collapsed to a space, so "Queen's
        # Garden Trail" and "Queens Garden Trail" key alike. Both stages pass
        # the same attr["name"] today, but a key that depends on that staying
        # true is a silent miss rather than a visible failure.
        return (
            re.sub(r"[^a-z0-9]+", "", str(item_name or "").lower()),
            str(url or "").strip(),
        )

    def _remember_retention_evidence(
        self, item_name: str, url: str, *,
        candidate: dict[str, Any] | None, allow_shallow_relevance: bool,
    ) -> None:
        """Record the context a retention call was made with.

        Only recorded when there is something to recall -- a bare call adds
        nothing and must not overwrite a richer earlier one for the same
        (item, url).
        """
        if candidate is None and not allow_shallow_relevance:
            return
        if not hasattr(self, "_retention_evidence"):
            self._retention_evidence: dict[tuple[str, str], dict[str, Any]] = {}
        self._retention_evidence[self._retention_evidence_key(item_name, url)] = {
            "candidate": candidate,
            "allow_shallow_relevance": bool(allow_shallow_relevance),
        }

    def _recall_retention_evidence(self, item_name: str, url: str) -> dict[str, Any]:
        """The context discovery had for this (item, url), or empty."""
        store = getattr(self, "_retention_evidence", None) or {}
        return store.get(self._retention_evidence_key(item_name, url), {})

    def _reject_retention(self, exit_id: int) -> str:
        """Record which retention check refused, then return the empty string.

        Every rejection exit in _retain_discovered_url routes through here, so
        the branch that fired is recoverable instead of being inferred.
        """
        self._last_retention_rejection = (
            exit_id,
            _RETENTION_EXIT_LABELS.get(exit_id, "unknown"),
        )
        return ""

    def _log_rejected_url(self, kind: str, dest_name: str, item_name: str, url: str) -> None:
        """Log a URL thrown away after it had already been accepted.

        This is the audit pass re-running _retain_discovered_url over a url
        discovery had assigned, with different arguments -- no candidate row,
        no shallow-relevance allowance -- so the two stages can and do reach
        opposite verdicts on the same link.

        It wrote only a logger.warning, never a disposition event, so the
        discard was absent from the item's trail. Balanced Rock's record
        showed general search recovering
        nps.gov/arch/planyourvisit/balancedrock.htm and then simply stopped,
        with the item removed for having no verified URL and nothing saying
        what took it away.
        """
        lower_url = (url or "").lower()
        expected_policy_rejection = (
            "alltrails.com" in lower_url
            and kind in {"scenic drive", "en-route stop", "restaurant", "event"}
        )
        level = logging.INFO if (kind == "scenic drive" and url == "[removed]") or expected_policy_rejection else logging.WARNING
        logger.log(
            level,
            "Rejected %s URL for '%s' (%s): %s",
            kind,
            item_name or "unknown",
            dest_name or "unknown destination",
            url or "(empty)",
        )
        self._log_decision(
            kind=kind,
            dest_name=dest_name,
            item_name=item_name,
            reason="audit_discarded_previously_accepted_url",
            message=(
                "url accepted during discovery was rejected by the audit pass"
                f" [retention exit {getattr(self, '_last_retention_rejection', None) or 'not recorded'}]"
            ),
            url=url,
            level=logging.DEBUG,
        )

    @staticmethod
    def _annotate_registry_url_decision(
        item: dict[str, Any],
        *,
        rendered_url: str,
        rejection_reason: str | None = None,
    ) -> None:
        registry_meta = item.get("_registry", {}) if isinstance(item.get("_registry", {}), dict) else {}
        registry_meta["validation_status"] = "accepted"
        registry_meta["rendered_url"] = str(rendered_url or "")
        if rejection_reason:
            existing = [
                str(reason or "")
                for reason in (registry_meta.get("rejection_reasons", []) or [])
                if str(reason or "")
            ]
            if rejection_reason not in existing:
                existing.append(rejection_reason)
            registry_meta["rejection_reasons"] = existing
        item["_registry"] = registry_meta

    def _removal_trail(self, *, kind: str, dest_name: str, item_name: str) -> list[dict[str, Any]]:
        """The URLs this item was offered, and which check refused each one.

        The disposition thread already holds this; it was summarised into
        per-destination reason totals and otherwise dropped. Totals cannot
        answer "why was Prague Castle removed" -- a destination showing
        `url_collision_rejected: 6` does not say which six items, so the
        question could only be guessed at. Attached to the removal record so
        it survives into the status report.
        """
        threads = getattr(self, "_decision_threads_by_destination", {}) or {}
        by_trace = threads.get(str(dest_name or "").strip() or "unknown", {})
        # Gather every thread for this item, not just this kind. _trace_id keys
        # on kind|destination|item, so events logged as kind="search" or
        # "audit" for the same item live in separate threads -- and those are
        # exactly the later stages that can clear a url the batch had assigned.
        # Filtering to one kind made the trail stop at the last attraction-kind
        # event: Balanced Rock showed its real page being recovered and then
        # nothing, with the item removed and no record of what discarded it.
        wanted_item = str(item_name or "").strip().lower()
        events: list[dict] = []
        for thread in by_trace.values():
            for event in thread or []:
                if not isinstance(event, dict):
                    continue
                if str(event.get("item", "") or "").strip().lower() == wanted_item:
                    events.append(event)
        events.sort(key=lambda e: int(e.get("seq", 0) or 0))
        trace_id = self._trace_id(kind=kind, dest_name=dest_name, item_name=item_name)
        trail = []
        # A retry re-runs discovery against the same disposition threads, which
        # accumulate rather than reset, so a retried destination logs each
        # candidate once per pass. Clearing _registry_decisions before a retry
        # fixed the removal COUNTS; this is the same defect one structure over,
        # and it inflated candidates_considered instead. Measured on a Europe
        # run: Brussels, the only retried destination, carried 65 duplicated
        # events out of 131 while every other destination carried none.
        #
        # Deduped on (reason, url): the question this answers is "which URLs
        # were tried and what refused them", and an identical pair repeated is
        # the same answer, not a second one.
        seen_events: set[tuple[str, str]] = set()
        for event in events:
            if not isinstance(event, dict):
                continue
            signature = (str(event.get("reason", "") or ""), str(event.get("url", "") or ""))
            if signature in seen_events:
                continue
            seen_events.add(signature)
            # Keys are reason/source/url as written by
            # _record_disposition_thread_event. Reading the _log_decision
            # parameter names (reason_code/rendered_url) instead silently
            # yields empty strings, which reports every item as "0 candidates
            # considered" -- a plausible-looking finding rather than an error.
            trail.append({
                "reason": str(event.get("reason", "") or ""),
                "url": str(event.get("url", "") or ""),
                "source": str(event.get("source", "") or ""),
                "stage": str(event.get("kind", "") or ""),
                # The event carries a message and the extraction was dropping
                # it, so the retention-exit label added to diagnose Upheaval
                # Dome never reached the report it was written for.
                "detail": str(event.get("message", "") or "")[:200],
            })
        return trail

    def _record_registry_entity_removal(
        self,
        dest: dict[str, Any],
        *,
        section_target: str,
        entity_class: str,
        display_name: str,
        rejection_reason: str,
        description: str = "",
        kind: str = "",
        dest_name: str = "",
    ) -> None:
        decisions = dest.get("_registry_decisions", []) if isinstance(dest.get("_registry_decisions", []), list) else []
        trail = self._removal_trail(kind=kind, dest_name=dest_name, item_name=display_name) if kind else []
        decisions.append({
            "entity_class": entity_class,
            "display_name": display_name,
            "description": description,
            "section_target": section_target,
            "validation_status": "rejected",
            "rejection_reasons": [rejection_reason],
            "rendered_url": "",
            "metadata": {"removed": True},
            # every URL considered for this item, and the check that refused it
            "candidate_trail": trail,
            "candidates_considered": sum(1 for e in trail if e.get("url")),
        })
        dest["_registry_decisions"] = decisions

    @staticmethod
    def _item_has_verified_url(item: dict[str, Any]) -> bool:
        """True when `item["url"]` is a real, specific source link.

        A generic Google Maps search/directions URL is a best-guess text
        query, never confirmed to be about the right specific place -- it
        does not satisfy "verified" under the verified-link-or-seed policy
        (project owner decision, 2026-08-17). Only the `url` field counts;
        a `maps_url` fallback (kept separately for the optional map-icon
        link) never counts as verification on its own.

        EXCEPTION, owner decision 2026-08-22: a **coordinate** Maps link does
        count. The 2026-08-17 rule is about text queries -- "best-guess",
        "never confirmed to be about the right specific place". A `lat,lng`
        link is not a guess: it names one point on the earth, resolved by a
        geocoder rather than by a search phrase. That is the same bar
        en-route stops have used since they were given
        `_item_has_verified_route_geocode`, so this extends an existing rule
        rather than introducing a new one.

        This is what makes the paid per-item fallback removable. That path
        was 66% of a cold run ($3.86 of $5.85, 218 searches) and existed to
        find a website for items the batch had already failed to resolve --
        after which the verified-link-or-seed policy deleted 29 attractions
        anyway for not finding one. A free geocode answers "where is it"
        without buying a search.
        """
        url = str((item or {}).get("url", "") or "").strip()
        if not url:
            return False
        if URLDiscoverer._is_coordinate_maps_query_url(url):
            return True
        return URLDiscoverer._classify_url_policy_class(url) not in {
            "google_maps_search",
            "google_maps_dir",
        }

    @staticmethod
    def _is_unverifiable_maps_query(url: str) -> bool:
        """A Maps link that _item_has_verified_url will refuse.

        Exactly the inverse of that gate's Maps handling, deliberately: a text
        query ("?query=Balanced+Rock") is a best guess, a coordinate query
        ("?query=38.7,-109.5") names a point on the earth. Accepting the first
        as an item's url guarantees the item is deleted downstream -- and, worse,
        marks it resolved, so the per-item search that would have found its real
        page never runs.

        Measured on the 2026-08-29 runs: Prague Castle, St. Vitus Cathedral,
        Balanced Rock, Navajo Loop Trail and Queen's Garden Trail were all lost
        this way, with their official pages ranking second in a plain search.
        test-coverage.md already documents Maps links as usable "only after
        direct-batch and ordinary web search paths are exhausted"; this restores
        that, rather than changing it.
        """
        if not URLDiscoverer._is_google_maps_candidate_url(url):
            return False
        if URLDiscoverer._is_coordinate_maps_query_url(url):
            return False
        return URLDiscoverer._classify_url_policy_class(url) in {
            "google_maps_search",
            "google_maps_dir",
        }

    @staticmethod
    def _is_coordinate_maps_query_url(url: str | None) -> bool:
        """True for a Maps search URL whose query is a bare `lat,lng` pair.

        Distinguishes a coordinate this code constructed from a verified
        geocode from an AI-authored place-name query. The two need opposite
        treatment: name queries are sanitized and rebuilt, coordinates must be
        left exactly as they are.
        """
        candidate = str(url or "")
        if "google.com/maps/search/" not in candidate:
            return False
        match = re.search(r"[?&]query=([^&]+)", candidate)
        if not match:
            return False
        query = unquote(match.group(1)).strip()
        return bool(re.fullmatch(r"-?\d{1,3}(?:\.\d+)?\s*,\s*-?\d{1,3}(?:\.\d+)?", query))

    def _geocode_maps_url_for_item(
        self, item_name: str, dest_name: str, dest_latlng: tuple[float, float] | None = None
    ) -> str:
        """A coordinate Maps link for an item, resolved by a FREE geocode.

        Replaces the paid per-item fallback search. Reuses the same Nominatim
        path and persistent cache the en-route stops use, so a destination
        geocoded once is free for every later run and every later customer.

        The destination's own coordinate is passed as both viewbox corners
        when available, which biases the lookup tightly to the destination --
        the disambiguation that stops "Red Canyon" resolving to a same-named
        place hundreds of miles away.

        Returns "" when the geocode fails, which leaves the item exactly
        where it was: no URL, and subject to the normal retention policy.
        """
        name = str(item_name or "").strip()
        if not name:
            return ""
        try:
            coords = self._geocode_en_route_stop_for_route(
                name,
                origin_name="",
                dest_name=str(dest_name or ""),
                origin=dest_latlng,
                dest=dest_latlng,
            )
        except Exception as exc:  # pragma: no cover - defensive only
            logger.info("Geocode fallback failed for %r in %r: %s", name, dest_name, exc)
            return ""
        if not coords:
            return ""
        lat, lng = coords
        return f"https://www.google.com/maps/search/?api=1&query={quote(f'{lat},{lng}')}"

    def _en_route_maps_url(
        self,
        stop: dict[str, Any],
        stop_name: str,
        dest_name: str,
        dest_latlng: tuple[float, float] | None = None,
    ) -> str:
        """A Google Maps link for an en-route stop, built locally at zero cost.

        Why en-route stops resolve to Maps rather than a website: they are
        waypoints on a drive. The traveller needs to *find* the pullout, not
        read about it. Chasing websites for them was the single largest
        source of wasted discovery on the 2026-08-21 cold-start run -- 253 of
        301 batch candidate rejections were en-route stops, roughly four
        rejected candidates per stop. The domains show why it could not be
        tuned away: blm.gov (55), nps.gov (48), roadtripryan.com (31),
        fs.usda.gov (24). Those are the right domains offering the wrong
        page -- a land-agency landing page for a specific roadside pullout.
        The granularity simply does not exist to be found.

        Three forms, in descending order of how precisely they answer
        "where is it", and the order matters more than any one of them:

        1. The stop's route-verified geocode, when it has one. It resolves to
           the exact spot, and `_prune_en_route_stops_by_geometry` has already
           sanity-checked that coordinate against the actual route.
        2. A free, cached Nominatim geocode of the name, biased to the
           destination. Still a coordinate, just not one the route vouched
           for. Requires the bias box: unbiased it does not degrade, it
           misfires -- "Canyon Overlook" near Zion comes back in Georgia.
        3. A name search, which is a guess rendered as a link.

        Zero cost throughout: (2) shares the persistent cache the route
        geocodes fill, so a destination looked up once is free thereafter.
        """
        if self._item_has_verified_route_geocode(stop):
            lat = str(stop.get("geocode_lat", "") or "").strip()
            lng = str(stop.get("geocode_lng", "") or "").strip()
            if lat and lng:
                return f"https://www.google.com/maps/search/?api=1&query={quote(f'{lat},{lng}')}"
        # No route-verified geocode. Before settling for a name search, ask
        # the same free, cached geocode an attraction in this position gets
        # (see the `geocode_maps` block in audit_discovered_urls). A stop is
        # not entitled to less: the New England ride shipped Mount
        # Agamenticus, Wickford Village and Stony Creek as bare text queries
        # while every stop beside them carried a coordinate, purely because
        # nothing here asked.
        # Only with a bias box. Unbiased, this lookup is not a weaker
        # coordinate but a wrong one: "Canyon Overlook" near Zion resolves to
        # 34.26,-84.54 -- Georgia, some 1,700 miles off -- and renders as a
        # precise pin with nothing about it admitting the guess. The
        # destination's own coordinate is what makes the answer local, which
        # is why the attraction path passes one too. Without it, a name query
        # is the honest fallback: visibly a search, and treated as
        # unverifiable by `_is_unverifiable_maps_query`.
        if dest_latlng is not None:
            geo_url = self._geocode_maps_url_for_item(stop_name, dest_name, dest_latlng)
            if geo_url:
                return geo_url

        # A name search is the last resort, and it is the unverifiable form:
        # `_is_unverifiable_maps_query` will not treat it as evidence the
        # place was found. It is offered as a map, labelled as a map, and
        # left to the retention policy to judge.
        query_text = self._maps_fallback_query_text(stop_name, dest_name)
        if not query_text:
            return ""
        return f"https://www.google.com/maps/search/?api=1&query={quote(query_text)}"

    @staticmethod
    def _item_has_verified_route_geocode(item: dict[str, Any]) -> bool:
        """True when an en-route stop carries a real, route-plausible geocode.

        `route_waypoint_eligible` and `geocode_lat`/`geocode_lng` are set in
        exactly one place, `_prune_en_route_stops_by_geometry`, and only
        together: a Nominatim lookup for the stop's own name -- biased
        toward the actual route's viewbox, and sanity-checked against the
        route itself when the match fell outside it -- resolved to a real
        coordinate that then also passed route-geometry plausibility (not
        beyond the destination, not off in the wrong direction; a
        same-named place resolving far outside the route is tracked
        separately as `en_route_geometry_filtered_wrong_region` and never
        reaches this state). That is real, independent, externally
        checkable evidence that a place by this name exists at a specific
        location plausibly on this route -- distinct from, and not gated
        on, any dedicated web page existing for it.

        This matters specifically for en-route stops because many genuine
        ones (roadside pull-offs, scenic turnouts, historic markers) never
        have their own page anywhere on the internet, unlike a destination
        attraction (NPS/park page) or restaurant (own site/Yelp/
        TripAdvisor). A source-page-only verification bar removes those
        wholesale even when they are real, correctly located places: a
        dipstick67 production run under the verified-link-or-seed policy
        removed 68 of 77 en-route stops (~88%) trip-wide, against a 37%
        removal rate for attractions and 8% for restaurants -- and manual
        spot-checks of the removed names (Cliff Palace at Mesa Verde,
        Corona Arch, Dead Horse Point State Park Overlook, Checkerboard
        Mesa, Little Wild Horse Canyon Trailhead, Edge of the Cedars State
        Park Museum) showed real official pages exist for several of them
        (some even harvested by direct-batch and then rejected by a
        separate liveness/retention check -- a recall gap, not an absence
        of a page) while all of them geocode cleanly to their real,
        correct, route-plausible location.

        Reusing `route_waypoint_eligible` -- the same flag this codebase
        already trusts for route-ordering and Google Maps waypoint
        decisions -- means this isn't a new, untested verification signal;
        it only recognizes evidence already computed and already vetted
        for exactly this purpose upstream, instead of discarding it and
        replacing it with a coin flip on whether a URL happened to survive
        the harvest/retention pipeline separately.
        """
        if not isinstance(item, dict):
            return False
        if item.get("route_waypoint_eligible") is not True:
            return False
        lat = item.get("geocode_lat")
        lng = item.get("geocode_lng")
        return isinstance(lat, (int, float)) and isinstance(lng, (int, float))

    def _keep_item_if_verified_or_seed(
        self,
        dest: dict[str, Any],
        item: dict[str, Any],
        item_name: str,
        *,
        is_seed: bool,
        section_target: str,
        entity_class: str,
        kind: str,
        dest_name: str,
        extra_verified: bool = False,
        extra_verified_reason: str = "",
    ) -> bool:
        """Decide whether `item` stays in its section's list.

        Policy (project owner decision, 2026-08-17): a non-seed item with
        no real, verified, specific source URL -- after all discovery/
        search/retry attempts are exhausted -- is removed from the
        itinerary entirely, not shown with a caution badge and not shown
        with a maps-search fallback link. A seed item (the traveler's own
        explicit request via the manifest `seeds`/`en_route_seeds` fields)
        always stays, even unverified, because an unverifiable seed may be
        a typo/obscure-but-real place rather than a systemic pipeline
        failure -- silently dropping a traveler's own request would be a
        worse UX failure than showing it honestly-unverified.

        `extra_verified` is an opt-in, section-specific override for a
        caller that has its own distinct, real (non-URL) verification
        signal -- currently only en-route stops, via
        `_item_has_verified_route_geocode` -- so attractions and
        restaurants are entirely unaffected and keep the strict
        real-page-or-seed bar.

        Returns True (keep) or False (drop, after logging the removal for
        registry/audit visibility).
        """
        if is_seed or self._item_has_verified_url(item):
            return True
        if extra_verified:
            self._log_decision(
                kind=kind,
                dest_name=dest_name,
                item_name=item_name,
                reason=extra_verified_reason or "extra_verification_kept",
                message=(
                    "non-seed item kept: no source URL, but verified by an "
                    "alternate section-specific bar"
                ),
            )
            return True
        self._log_decision(
            kind=kind,
            dest_name=dest_name,
            item_name=item_name,
            reason="no_verified_url_removed",
            message="non-seed item removed: no real verified source URL survived discovery/audit",
        )
        self._record_registry_entity_removal(
            dest,
            section_target=section_target,
            entity_class=entity_class,
            display_name=item_name,
            description=str(item.get("description", "") or ""),
            rejection_reason="no_verified_url_removed",
            kind=kind,
            dest_name=dest_name,
        )
        return False

    # ── Attractions ──────────────────────────────────────────────────────────

    def _discover_attractions(
        self,
        ai: dict[str, Any],
        dest_name: str,
        nps_code: str | None,
        dest_dates: str | None = None,
        seed_names: list[str] | None = None,
        dest: dict[str, Any] | None = None,
    ) -> None:
        attraction_source_mode = str(
            getattr(self, "_attraction_source", DEFAULT_ATTRACTION_SOURCE) or DEFAULT_ATTRACTION_SOURCE
        )
        top_attractions = ai.get("top_attractions", [])
        if attraction_source_mode == "direct_link_batch":
            # Ask the batch for the items the itinerary ACTUALLY contains.
            #
            # Stage 3 invents the attraction list; the Stage 5b batch then
            # independently invents its own. Where they disagree, every
            # orphaned item costs a per-item fallback search. Measured on the
            # 2026-08-21 cold-start run: 81 of 82 `direct_batch_no_match`
            # events were attractions, and those fallbacks are 42% of all
            # token spend.
            #
            # The mechanism already existed -- `_direct_batch_seed_hint_clauses`
            # was built because manifest seeds went missing from the harvest
            # for exactly this reason ("the harvest prompt itself having no
            # mechanism at all to surface seeds as candidates"). It was only
            # ever fed seeds. Stage 3's own names have the identical problem
            # and no seed to speak for them.
            #
            # Prewarmed here because the batch is cached per destination and
            # the first fetch wins -- every later lookup hits that cache, so
            # the hints must be present on the first call or not at all.
            self._prewarm_attraction_batch_with_itinerary_items(
                ai=ai, dest_name=dest_name, dest_dates=str(dest_dates or ""), seed_names=seed_names,
            )
            top_attractions = self._prioritize_direct_batch_attractions(
                top_attractions,
                dest_name,
                dest_dates,
                seed_names=seed_names,
            )
            ai["top_attractions"] = top_attractions
        # THIRD AllTrails entry point. The switch already guards
        # _search_alltrails_for_trail, _search_alltrails_for_seed_relaxed and
        # _search_alltrails_for_trail_filtered -- and still leaked here.
        # Measured 2026-08-23 with trails disabled: the fallback path was
        # correctly at 0 calls while the trail direct BATCH ran for all ten
        # destinations, 20 capture files, and 8 AllTrails links reached the
        # output. Each guarded path made the leak look smaller without
        # closing it.
        alltrails_source_mode = str(
            getattr(self, "_alltrails_source", DEFAULT_ALLTRAILS_SOURCE) or DEFAULT_ALLTRAILS_SOURCE
        )
        if bool(getattr(self, "_disable_trails", False)):
            alltrails_source_mode = "disabled"
        if alltrails_source_mode == "direct_link_batch":
            top_attractions = self._prioritize_direct_batch_trails(
                top_attractions,
                dest_name,
                dest_dates,
                seed_names=seed_names,
            )
            ai["top_attractions"] = top_attractions
        seed_key_set = {
            re.sub(r"[^a-z0-9]+", " ", str(seed or "").lower()).strip()
            for seed in (seed_names or [])
            if str(seed or "").strip()
        }
        for attr in list(top_attractions):
            attr_name = attr.get("name", "")
            attr_key = re.sub(r"[^a-z0-9]+", " ", str(attr_name or "").lower()).strip()
            is_seed = bool(attr_key and attr_key in seed_key_set)
            attr_type = str(attr.get("type", "attraction") or "attraction").lower()
            attr_desc = str(attr.get("description", "") or "")
            attr_context = self._attraction_trail_context(attr)
            maps_fallback_url = f"https://www.google.com/maps/search/?api=1&query={quote(self._maps_fallback_query_text(attr_name, dest_name))}"

            if self._is_uninterested_attraction(attr_name, attr_type, attr_desc, dest_dates):
                attr["url"] = ""
                self._log_decision(
                    kind="attraction",
                    dest_name=dest_name,
                    item_name=attr_name,
                    reason="interest_filter_skipped",
                    message="attraction link skipped by interest filter",
                )
                continue

            trail_like = self._is_trail_like_attraction(attr_name, attr_type, attr_context)

            # GH #68 multi-site grouping: a grouped entry can defer the
            # "trail" or "attraction" category to its group base (rare --
            # the default only defers restaurants -- but configurable per
            # docs/design/multi-site-destination-grouping.md §5). Purely a
            # skip-gate; discovery logic below is otherwise untouched.
            item_category = "trail" if trail_like else "attraction"
            if category_deferred_to_base(
                dest,
                item_category,
                getattr(self, "_multi_site_base_owned_categories", DEFAULT_BASE_OWNED_CATEGORIES),
            ):
                attr["url"] = ""
                attr.pop("maps_url", None)
                self._log_decision(
                    kind="attraction",
                    dest_name=dest_name,
                    item_name=attr_name,
                    reason="base_owned_category_skipped",
                    message=f"{item_category} link discovery skipped — category deferred to group base",
                )
                continue

            trail_direct_batch_authoritative = (
                trail_like
                and str(getattr(self, "_alltrails_source", DEFAULT_ALLTRAILS_SOURCE) or DEFAULT_ALLTRAILS_SOURCE)
                == "direct_link_batch"
                and self._direct_batch_is_authoritative()
            )

            if trail_like and bool(getattr(self, "_disable_trails", False)):
                attr["url"] = ""
                attr.pop("maps_url", None)
                self._log_decision(
                    kind="attraction",
                    dest_name=dest_name,
                    item_name=attr_name,
                    reason="trail_links_disabled",
                    message="trail-like link omitted by no-trails option",
                )
                continue

            # In direct-link batch mode for non-trail attractions, treat batch
            # results as primary and avoid overriding with separate AI
            # candidate URLs before batch evaluation.
            should_consider_ai_candidate = (
                not self._direct_batch_is_authoritative()
                and (
                    (trail_like and not trail_direct_batch_authoritative)
                    or (not trail_like and attraction_source_mode != "direct_link_batch")
                )
            )
            if should_consider_ai_candidate:
                ai_candidate_url = self._resolve_ai_candidate_url(
                    item=attr,
                    item_name=attr_name,
                    dest_name=dest_name,
                    allow_alltrails=trail_like,
                    trail_like=trail_like,
                    kind="attraction",
                )
                if ai_candidate_url:
                    attr["url"] = ai_candidate_url
                    self._log_decision(
                        kind="attraction",
                        dest_name=dest_name,
                        item_name=attr_name,
                        reason="ai_candidate_accepted",
                        message="attraction link (ai-candidate)",
                        url=ai_candidate_url,
                    )
                    continue

            # Trail-like items should prefer AllTrails first.
            if trail_like:
                direct_batch_url = self._search_alltrails_for_trail_from_direct_batch(attr_name, dest_name, str(dest_dates or ""))
                if direct_batch_url and self._direct_batch_is_authoritative():
                    direct_batch_url = self._prefer_canonical_alltrails_url(direct_batch_url, attr_name)
                    if (
                        self._passes_alltrails_post_search_filters(direct_batch_url, attr_name, dest_name)
                        and (
                            self._is_remembered_direct_batch_authoritative_url(direct_batch_url, attr_name)
                            or self._meets_alltrails_publish_confidence(direct_batch_url, attr_name, dest_name)
                        )
                    ):
                        attr["url"] = direct_batch_url
                        attr.update(
                            self._direct_batch_row_quality_metadata_for_url(
                                self._get_alltrails_direct_batch_rows_for_destination(dest_name, str(dest_dates or "")),
                                direct_batch_url,
                            )
                        )
                        self._log_decision(
                            kind="attraction",
                            dest_name=dest_name,
                            item_name=attr_name,
                            reason="direct_batch_accepted",
                            message="trail-like link (direct-link batch authoritative)",
                            url=direct_batch_url,
                        )
                        continue
                    self._log_decision(
                        kind="attraction",
                        dest_name=dest_name,
                        item_name=attr_name,
                        reason="direct_batch_threshold_violation",
                        message="trail-like direct-link batch candidate rejected by threshold checks",
                        url=direct_batch_url,
                    )
                url = self._search_alltrails_for_trail(attr_name, dest_name, str(dest_dates or ""))
                if (
                    url
                    and not (
                        self._direct_batch_is_authoritative()
                        and self._is_remembered_direct_batch_authoritative_url(url, attr_name)
                    )
                    and not self._meets_alltrails_publish_confidence(url, attr_name, dest_name)
                ):
                    logger.info(
                        "  trail-like link (alltrails) downgraded by confidence gate: %s -> %s",
                        attr_name,
                        url,
                    )
                    url = None
                if url:
                    attr["url"] = url
                    self._log_decision(
                        kind="attraction",
                        dest_name=dest_name,
                        item_name=attr_name,
                        reason="alltrails_accepted",
                        message="trail-like link (alltrails)",
                        url=url,
                    )
                    continue
                if is_seed:
                    relaxed_seed_url = self._search_alltrails_for_seed_relaxed(attr_name, dest_name)
                    if relaxed_seed_url:
                        attr["url"] = relaxed_seed_url
                        self._log_decision(
                            kind="attraction",
                            dest_name=dest_name,
                            item_name=attr_name,
                            reason="seed_alltrails_relaxed_accepted",
                            message="seed trail kept via relaxed alltrails fallback",
                            url=relaxed_seed_url,
                        )
                        continue
                # Trail exhausted all AllTrails paths; try NPS before a maps fallback.
                # Trails in NPS parks often have dedicated nps.gov hike pages.
                trail_nps_code = nps_code or self._infer_item_nps_code(attr_name)
                if trail_nps_code and not self._direct_batch_is_authoritative():
                    trail_fanout_result = self._search_attraction_from_item_query_fanout(
                        item_name=attr_name,
                        dest_name=dest_name,
                        nps_code=trail_nps_code,
                    )
                    if isinstance(trail_fanout_result, tuple):
                        trail_fanout_url, trail_fanout_source = trail_fanout_result
                    elif isinstance(trail_fanout_result, str) and trail_fanout_result:
                        trail_fanout_url, trail_fanout_source = trail_fanout_result, "fanout"
                    else:
                        trail_fanout_url, trail_fanout_source = None, "no_match"
                    if trail_fanout_url and not self._is_alltrails_trail_url(trail_fanout_url):
                        attr["url"] = trail_fanout_url
                        self._log_decision(
                            kind="attraction",
                            dest_name=dest_name,
                            item_name=attr_name,
                            reason=f"trail_nps_fanout_{trail_fanout_source}_accepted",
                            message="trail NPS page recovered via fanout after AllTrails no-match",
                            url=trail_fanout_url,
                        )
                        continue
                if self._direct_batch_is_authoritative():
                    # The trail-like classification (_is_trail_like_attraction) is a
                    # broad keyword catch-all -- e.g. a viewpoint whose description
                    # happens to mention "a short walk" reads as trail-like even
                    # though its actual type is "viewpoint". When that
                    # misclassification sends an item down the AllTrails-only path
                    # and it predictably finds no matching trail row there, don't
                    # give up outright: the (already-harvested, zero-extra-cost)
                    # attraction direct-batch rows may still have the real item --
                    # e.g. dipstick58's real "Bryce Point" (type "viewpoint",
                    # trail_like only because its description said "a short walk")
                    # had a correct, harvested NPS row ("Bryce Point Overlook",
                    # nps.gov/brca/planyourvisit/brycepoint.htm) that this
                    # trail-only path never got a chance to check because
                    # authoritative mode locks trail-like items to the trail
                    # source and never falls through to the general
                    # attraction_source_mode == "direct_link_batch" branch below.
                    fallback_attraction_url = self._search_attraction_from_direct_batch(
                        attr_name, dest_name, str(dest_dates or "")
                    )
                    if fallback_attraction_url:
                        attr["url"] = fallback_attraction_url
                        attr.update(
                            self._direct_batch_row_quality_metadata_for_url(
                                self._get_attraction_direct_batch_rows_for_destination(
                                    dest_name, str(dest_dates or "")
                                ),
                                fallback_attraction_url,
                            )
                        )
                        self._log_decision(
                            kind="attraction",
                            dest_name=dest_name,
                            item_name=attr_name,
                            reason="trail_like_misclassified_attraction_batch_recovered",
                            message="no trail match for trail-like item; recovered via attraction direct-batch row",
                            url=fallback_attraction_url,
                        )
                        continue
                    # No direct-link batch row matched this trail-like item
                    # either directly (AllTrails) or via the misclassified-
                    # attraction recovery above. Authoritative mode forbids
                    # trusting an AI-suggested url_candidate for a specific
                    # canonical URL -- that's an unverified assertion with no
                    # independent search grounding, the exact fabrication
                    # risk this mode exists to block (see
                    # test_discover_attractions_direct_batch_authoritative_
                    # recovers_seed_from_ai_candidate). A real, live, grounded
                    # search result is different in kind: it comes from an
                    # actual search-API call and is independently qualified
                    # by the same specificity/relevance/policy-class/liveness
                    # gates every non-authoritative attraction link on this
                    # path already goes through (_search_first below is the
                    # same call the "For NPS parks" section a few lines down
                    # makes). One more such attempt here doesn't reopen that
                    # risk, so try it before giving up on a real link.
                    trail_general_search_url = self._search_first(
                        _build_query_variants(attr_name, dest_name, "trail hike"),
                        site_filter="nps.gov" if trail_nps_code else None,
                        site_hint=(f"site:nps.gov/{trail_nps_code}" if trail_nps_code else None),
                        item_name=attr_name,
                        dest_name=dest_name,
                        allow_alltrails=True,
                    )
                    if trail_general_search_url:
                        attr["url"] = trail_general_search_url
                        self._log_decision(
                            kind="attraction",
                            dest_name=dest_name,
                            item_name=attr_name,
                            reason="authoritative_no_match_recovered_via_general_search",
                            message="trail-like link recovered via general search after authoritative direct-batch no-match",
                            url=trail_general_search_url,
                        )
                        continue
                    # General search also came up empty. The same safe
                    # Google-Maps-search fallback every other "no URL found"
                    # attraction gets (see _assign_attraction_maps_fallback_
                    # or_fail_closed) applies here -- it's a name+destination
                    # search link, not a claim of a specific correct source
                    # page.
                    self._assign_attraction_maps_fallback_or_fail_closed(
                        attr,
                        attr_name=attr_name,
                        dest_name=dest_name,
                        maps_fallback_url=maps_fallback_url,
                    )
                    self._log_decision(
                        kind="attraction",
                        dest_name=dest_name,
                        item_name=attr_name,
                        reason="direct_batch_source_locked_no_match",
                        message="trail-like link omitted; authoritative direct-link batch had no usable result; maps fallback applied where not fail-closed",
                        url=str(attr.get("url", "") or ""),
                    )
                    continue
                attr.pop("url", None)
                q = self._maps_fallback_query_text(attr_name, dest_name)
                attr["maps_url"] = f"https://www.google.com/maps/search/?api=1&query={quote(q)}"
                attr["url"] = attr["maps_url"]
                self._log_decision(
                    kind="attraction",
                    dest_name=dest_name,
                    item_name=attr_name,
                    reason="trail_maps_fallback_assigned",
                    message="trail-like canonical link omitted; maps fallback assigned",
                    url=attr["maps_url"],
                )
                continue

            if attraction_source_mode == "direct_link_batch":
                existing_url = str(attr.get("url", "") or "").strip()
                if existing_url:
                    cleaned_existing = self._retain_discovered_url(
                        existing_url,
                        attr_name,
                        dest_name,
                        allow_alltrails=False,
                        kind="attraction",
                    )
                    if cleaned_existing:
                        if self._direct_batch_is_authoritative() and self._is_google_maps_candidate_url(cleaned_existing):
                            cleaned_existing = ""
                        if cleaned_existing:
                            attr["url"] = cleaned_existing
                            if self._is_google_maps_candidate_url(cleaned_existing):
                                attr["maps_url"] = maps_fallback_url
                            else:
                                # This shortcut (URL already attached before this
                                # loop ran, e.g. by _prioritize_direct_batch_
                                # attractions) skipped the same row-metadata
                                # merge the fresh-lookup branch below does,
                                # silently leaving rating/votes/description
                                # empty even when the matched row had them
                                # (dipstick55 Theme D: "Red Hills Desert Garden"
                                # rendered with no rating badge and no teaser
                                # despite its harvested row having both).
                                attr.update(
                                    self._direct_batch_row_quality_metadata_for_url(
                                        self._get_attraction_direct_batch_rows_for_destination(
                                            dest_name, str(dest_dates or "")
                                        ),
                                        cleaned_existing,
                                    )
                                )
                            self._log_decision(
                                kind="attraction",
                                dest_name=dest_name,
                                item_name=attr_name,
                                reason="direct_batch_existing_url_preserved",
                                message="attraction link preserved from direct-link batch row",
                                url=cleaned_existing,
                            )
                            continue
                batch_url = self._search_attraction_from_direct_batch(attr_name, dest_name, str(dest_dates or ""))
                if batch_url:
                    attr["url"] = batch_url
                    attr.update(
                        self._direct_batch_row_quality_metadata_for_url(
                            self._get_attraction_direct_batch_rows_for_destination(dest_name, str(dest_dates or "")),
                            batch_url,
                        )
                    )
                    self._log_decision(
                        kind="attraction",
                        dest_name=dest_name,
                        item_name=attr_name,
                        reason="direct_batch_accepted",
                        message="attraction link (direct-link batch)",
                        url=batch_url,
                    )
                    continue
                if self._direct_batch_is_authoritative():
                    # The direct-link-batch harvest didn't find a matching row
                    # for this item, and authoritative mode means we must not
                    # trust an AI-suggested url_candidate to attach a
                    # *specific* source URL -- that's an unverified assertion
                    # with no independent search grounding, the confidently-
                    # wrong-link risk this mode exists to block (see
                    # test_discover_attractions_direct_batch_authoritative_
                    # recovers_seed_from_ai_candidate). A real, live, grounded
                    # search result doesn't carry that same risk: it comes
                    # from an actual search-API call and is independently
                    # qualified by the same specificity/relevance/policy-
                    # class/liveness gates every non-authoritative attraction
                    # link on this path already goes through (_search_first
                    # below is the same call the "For NPS parks" section a
                    # few lines down makes). Try one more such attempt before
                    # giving up on a real link.
                    site_hint = f"site:nps.gov/{nps_code}" if nps_code else None
                    general_search_url = self._search_first(
                        _build_query_variants(attr_name, dest_name, "attraction landmark museum viewpoint"),
                        site_filter="nps.gov" if nps_code else None,
                        site_hint=site_hint,
                        item_name=attr_name,
                        dest_name=dest_name,
                        allow_alltrails=trail_like,
                    )
                    if general_search_url:
                        attr["url"] = general_search_url
                        self._log_decision(
                            kind="attraction",
                            dest_name=dest_name,
                            item_name=attr_name,
                            reason="authoritative_no_match_recovered_via_general_search",
                            message="attraction link recovered via general search after authoritative direct-batch no-match",
                            url=general_search_url,
                        )
                        continue
                    # General search also came up empty. A real, harvested
                    # attraction name still deserves the same safe
                    # Google-Maps-search fallback every other "no URL found"
                    # attraction gets below -- it's a name+destination search
                    # link, not a claim of a specific correct source page, so
                    # it doesn't carry that fabrication risk. Apply the same
                    # fail-closed exceptions (category-style activity,
                    # ambiguous geographic name, enforce-policy block) that
                    # already guard that fallback elsewhere in this function.
                    self._assign_attraction_maps_fallback_or_fail_closed(
                        attr,
                        attr_name=attr_name,
                        dest_name=dest_name,
                        maps_fallback_url=maps_fallback_url,
                    )
                    self._log_decision(
                        kind="attraction",
                        dest_name=dest_name,
                        item_name=attr_name,
                        reason="discovery_completed",
                        message="attraction link",
                        url=str(attr.get("url", "") or ""),
                    )
                    continue

            # For NPS parks, prefer nps.gov results
            site_hint = f"site:nps.gov/{nps_code}" if nps_code else None
            url = self._search_first(
                _build_query_variants(attr_name, dest_name, "attraction landmark museum viewpoint"),
                site_filter="nps.gov" if nps_code else None,
                site_hint=site_hint,
                item_name=attr_name,
                dest_name=dest_name,
                allow_alltrails=trail_like,
            )
            # Category-style park activities (e.g., stargazing) often map better
            # to thematic NPS pages than exact-name queries.
            if not url and nps_code and self._is_category_style_activity(attr_name):
                url = self._search_first(
                    _build_nps_activity_query_variants(attr_name, dest_name),
                    site_filter="nps.gov",
                    site_hint=site_hint,
                    item_name=attr_name,
                    dest_name=dest_name,
                    allow_alltrails=trail_like,
                )
            # Fallback: broad search; keep AllTrails allowed for trail-like items.
            if not url:
                url = self._search_first(
                    _build_query_variants(attr_name, dest_name, "attraction landmark museum viewpoint"),
                    item_name=attr_name,
                    dest_name=dest_name,
                    allow_alltrails=trail_like,
                )
            # Non-trail attractions can optionally use deterministic Google Maps
            # Things to do/place targets when web/NPS pages are unavailable.
            if not url and not trail_like:
                maps_place_url = self._search_first(
                    _build_attraction_maps_query_variants(attr_name, dest_name),
                    site_filter="google.com/maps",
                    item_name=attr_name,
                    dest_name=dest_name,
                    allow_alltrails=False,
                )
                if maps_place_url:
                    url = maps_place_url
                    attr["maps_url"] = maps_fallback_url
                    self._log_decision(
                        kind="attraction",
                        dest_name=dest_name,
                        item_name=attr_name,
                        reason="maps_place_accepted",
                        message="attraction link (maps place)",
                        url=maps_place_url,
                    )
            if url:
                attr["url"] = url
            else:
                if self._is_category_style_activity(attr_name):
                    attr["url"] = ""
                    self._log_decision(
                        kind="attraction",
                        dest_name=dest_name,
                        item_name=attr_name,
                        reason="category_activity_fail_closed",
                        message="attraction maps fallback omitted for category-style activity",
                    )
                elif self._is_ambiguous_geographic_feature_name(attr_name):
                    attr["url"] = ""
                    self._log_decision(
                        kind="attraction",
                        dest_name=dest_name,
                        item_name=attr_name,
                        reason="maps_fallback_omitted_ambiguous_geography",
                        message="attraction maps fallback omitted for ambiguous geographic feature",
                    )
                else:
                    policy_mode = str(getattr(self, "_url_policy_mode", DEFAULT_URL_POLICY_MODE) or DEFAULT_URL_POLICY_MODE)
                    blocked_classes = set(getattr(self, "_url_policy_blocked_classes", set(DEFAULT_URL_POLICY_BLOCKED_CLASSES)) or set())
                    if policy_mode == "enforce" and "google_maps_search" in blocked_classes:
                        attr["url"] = ""
                        attr.pop("maps_url", None)
                        self._log_decision(
                            kind="attraction",
                            dest_name=dest_name,
                            item_name=attr_name,
                            reason="maps_fallback_omitted_policy_enforce",
                            message="attraction maps fallback omitted by enforce policy",
                        )
                    else:
                        attr["url"] = maps_fallback_url
                        attr["maps_url"] = attr["url"]
                        self._log_decision(
                            kind="attraction",
                            dest_name=dest_name,
                            item_name=attr_name,
                            reason="maps_fallback_assigned",
                            message="attraction maps fallback assigned",
                            url=attr["url"],
                        )

            self._log_decision(
                kind="attraction",
                dest_name=dest_name,
                item_name=attr_name,
                reason="discovery_completed",
                message="attraction link",
                url=(url or ""),
            )

        # Second pass: remove closed non-seed attractions regardless of discovery path.
        for attr in list(top_attractions):
            attr_name = str(attr.get("name", "") or "")
            attr_key = re.sub(r"[^a-z0-9]+", " ", attr_name.lower()).strip()
            is_seed = bool(attr_key and attr_key in seed_key_set)
            closure_text = " ".join(
                part for part in (
                    str(attr.get("description", "") or ""),
                    str(attr.get("practical_note", "") or ""),
                ) if part
            )
            attr_url = str(attr.get("url", "") or "").strip()
            if attr_url and not any(attr_url.lower().startswith(p) for p in SAFE_FALLBACK_URL_PREFIXES):
                ok, _status, page_text = self._fetch_page_text(attr_url, timeout=8)
                if ok and page_text:
                    closure_text = f"{closure_text} {page_text}".strip()
            if self._has_attraction_closure_marker(closure_text):
                if is_seed:
                    closure_note = "Currently closed; verify status before you go."
                    existing_note = str(attr.get("practical_note", "") or "").strip()
                    if closure_note.lower() not in existing_note.lower():
                        attr["practical_note"] = f"{existing_note} {closure_note}".strip() if existing_note else closure_note
                    # A seed can't be dropped outright (it's the user's explicit
                    # pick), so the canonical link is unlinked here -- it may
                    # point at a page describing a closure and shouldn't be
                    # presented as a confident "this is the right, current
                    # page" link. But the note above explicitly tells the user
                    # to "verify status before you go", which is not actionable
                    # if both url and maps_url are left empty. Give the same
                    # safe maps-search fallback every other "no URL found"
                    # attraction gets (same fail-closed exceptions) so the
                    # verification the note asks for is actually possible.
                    fallback_query_url = (
                        "https://www.google.com/maps/search/?api=1&query="
                        f"{quote(self._maps_fallback_query_text(attr_name, dest_name))}"
                    )
                    self._assign_attraction_maps_fallback_or_fail_closed(
                        attr,
                        attr_name=attr_name,
                        dest_name=dest_name,
                        maps_fallback_url=fallback_query_url,
                    )
                    self._log_decision(
                        kind="attraction",
                        dest_name=dest_name,
                        item_name=attr_name,
                        reason="closure_unlinked_seed",
                        message="seeded attraction is closed; hyperlink removed",
                        url=str(attr.get("url", "") or ""),
                    )
                    continue
                top_attractions.remove(attr)
                self._record_registry_entity_removal(
                    dest=dest if isinstance(dest, dict) else {"_registry_decisions": []},
                    section_target="top_attractions",
                    entity_class="attraction",
                    display_name=attr_name,
                    description=str(attr.get("description", "") or ""),
                    rejection_reason="closure_removed",
                )
                self._log_decision(
                    kind="attraction",
                    dest_name=dest_name,
                    item_name=attr_name,
                    reason="closure_removed",
                    message="attraction page is closed and was removed",
                )

    def _assign_attraction_maps_fallback_or_fail_closed(
        self,
        attr: dict,
        *,
        attr_name: str,
        dest_name: str,
        maps_fallback_url: str,
    ) -> None:
        """Assign the safe Google-Maps-search fallback link for an attraction
        with no discovered canonical URL, unless one of the existing
        fail-closed exceptions applies (category-style activity, ambiguous
        geographic feature name, or enforce-mode policy blocking the
        google_maps_search URL class).

        This is the same "no URL found" disposition logic historically inline
        at the bottom of the general (non-direct-batch, non-trail) attraction
        loop body. It is factored out so the authoritative direct-link-batch
        "no match" branch (attraction_source_mode == "direct_link_batch" with
        self._direct_batch_is_authoritative() and no batch row match) can
        reuse it verbatim instead of unconditionally leaving the attraction
        with no link at all. A maps-search-query fallback is not a claim that
        a specific page is the correct source for this item -- it is a
        deterministic search-by-name link, categorically safer than trusting
        an unverified direct hyperlink or AI-suggested candidate URL, which is
        the actual fabrication risk authoritative mode exists to block.
        """
        if self._is_category_style_activity(attr_name):
            attr["url"] = ""
            self._log_decision(
                kind="attraction",
                dest_name=dest_name,
                item_name=attr_name,
                reason="category_activity_fail_closed",
                message="attraction maps fallback omitted for category-style activity",
            )
            return
        if self._is_ambiguous_geographic_feature_name(attr_name):
            attr["url"] = ""
            self._log_decision(
                kind="attraction",
                dest_name=dest_name,
                item_name=attr_name,
                reason="maps_fallback_omitted_ambiguous_geography",
                message="attraction maps fallback omitted for ambiguous geographic feature",
            )
            return
        policy_mode = str(getattr(self, "_url_policy_mode", DEFAULT_URL_POLICY_MODE) or DEFAULT_URL_POLICY_MODE)
        blocked_classes = set(getattr(self, "_url_policy_blocked_classes", set(DEFAULT_URL_POLICY_BLOCKED_CLASSES)) or set())
        if policy_mode == "enforce" and "google_maps_search" in blocked_classes:
            attr["url"] = ""
            attr.pop("maps_url", None)
            self._log_decision(
                kind="attraction",
                dest_name=dest_name,
                item_name=attr_name,
                reason="maps_fallback_omitted_policy_enforce",
                message="attraction maps fallback omitted by enforce policy",
            )
            return
        attr["url"] = maps_fallback_url
        attr["maps_url"] = attr["url"]
        self._log_decision(
            kind="attraction",
            dest_name=dest_name,
            item_name=attr_name,
            reason="maps_fallback_assigned",
            message="attraction maps fallback assigned",
            url=attr["url"],
        )

    @staticmethod
    def _infer_item_nps_code(item_name: str) -> str | None:
        """Return an NPS park code when the item name itself names a known NPS park."""
        lower = re.sub(r"\s+", " ", (item_name or "").lower()).strip()
        hints = {
            "zion": "zion",
            "bryce canyon": "brca",
            "capitol reef": "care",
            "arches": "arch",
            "canyonlands": "cany",
            "yellowstone": "yell",
            "yosemite": "yose",
            "grand canyon": "grca",
            "rocky mountain": "romo",
            "bandelier": "band",
            "mesa verde": "meve",
            "petrified forest": "pefo",
        }
        for hint, code in hints.items():
            if hint in lower:
                return code
        return None

    def _is_uninterested_attraction(
        self,
        attr_name: str,
        attr_type: str,
        description: str,
        dest_dates: str | None,
    ) -> bool:
        haystack = f" {attr_name} {attr_type} {description} ".lower()

        uninterested_keywords = getattr(
            self,
            "_uninterested_keywords",
            DEFAULT_UNINTERESTED_ATTRACTION_KEYWORDS,
        )
        if any(keyword in haystack for keyword in uninterested_keywords):
            return True

        ski_keywords = getattr(
            self,
            "_seasonal_ski_keywords",
            DEFAULT_SKI_ATTRACTION_KEYWORDS,
        )
        ski_signal = False
        for keyword in ski_keywords:
            k = str(keyword or "").strip().lower()
            if not k:
                continue
            if k == "ski":
                if re.search(r"\bski(?:ing|er|ers)?\b", haystack):
                    ski_signal = True
                    break
                continue
            if k in haystack:
                ski_signal = True
                break

        if not ski_signal:
            return False

        months = self._extract_month_numbers(dest_dates or "")
        if not months:
            return False

        in_season = set(getattr(self, "_ski_in_season_months", DEFAULT_SKI_IN_SEASON_MONTHS))
        return not any(month in in_season for month in months)

    @staticmethod
    def _extract_month_numbers(text: str) -> set[int]:
        out: set[int] = set()
        lowered = (text or "").lower()
        for month_name, month_number in MONTH_NAME_TO_NUMBER.items():
            if re.search(rf"\b{month_name}\b", lowered):
                out.add(month_number)
        return out

    def _direct_link_batch_limit(self) -> int:
        return max(5, int(getattr(self, "_direct_link_batch_count", DEFAULT_DIRECT_LINK_BATCH_COUNT) or DEFAULT_DIRECT_LINK_BATCH_COUNT))

    def _day_scaled_direct_batch_count(self, dates: str, *, items_per_day: int, buffer_multiplier: int = 2) -> int:
        """Right-size a trail/attraction harvest request to roughly what will
        actually be kept, instead of always asking for the flat configured
        ceiling regardless of how short the stay is.

        _prioritize_direct_batch_attractions/_trails only ever keep up to
        items_per_day * day_count rows -- a fixed count of e.g. 20 for a
        2-day stay needing 6-9 items asks the model to generate more than
        double what will ever be used, directly costing completion tokens
        and generation time. buffer_multiplier covers rows that don't survive
        rating/matching/URL-resolution filtering; capped at
        _direct_link_batch_limit() so this never asks for MORE than before,
        only less for the common case of short stays.
        """
        day_count = self._infer_destination_day_count(dates)
        target = max(1, items_per_day) * max(1, day_count)
        return min(self._direct_link_batch_limit(), max(5, target * buffer_multiplier))

    def _direct_batch_is_authoritative(self) -> bool:
        return bool(getattr(self, "_direct_batch_authoritative", DEFAULT_DIRECT_BATCH_AUTHORITATIVE))

    @staticmethod
    def _collision_key(url: str) -> str:
        """Normalised form for "is this the same page as that one".

        Scheme, www., trailing slash and query/fragment are dropped: an
        aggregator will happily serve the same restaurant page under http and
        https, with and without www, and with tracking parameters attached.
        Comparing raw strings would let the same page through twice.
        """
        candidate = str(url or "").strip().lower()
        if not candidate:
            return ""
        candidate = candidate.split("#", 1)[0].split("?", 1)[0]
        for prefix in ("https://", "http://"):
            if candidate.startswith(prefix):
                candidate = candidate[len(prefix):]
                break
        if candidate.startswith("www."):
            candidate = candidate[4:]
        return candidate.rstrip("/")

    @classmethod
    def _url_already_claimed(cls, url: str, claimed: set[str]) -> bool:
        key = cls._collision_key(url)
        return bool(key) and key in claimed

    def _item_fallback_when_batch_silent_enabled(self) -> bool:
        """Whether an item the authoritative batch could not place may still be
        searched for individually.

        Separate from _direct_batch_is_authoritative on purpose: authority is
        about whose answer wins, this is about what to do when there is no
        answer at all. Conflating the two turned "the batch is the source of
        truth" into "items the batch misses do not exist".
        """
        return bool(
            getattr(self, "_item_fallback_when_batch_silent", DEFAULT_ITEM_FALLBACK_WHEN_BATCH_SILENT)
        )

    @staticmethod
    def _normalize_direct_batch_authoritative_url(url: str | None) -> str:
        candidate = str(url or "").strip()
        if not candidate:
            return ""
        lower = candidate.lower()
        if not (lower.startswith("http://") or lower.startswith("https://")):
            return ""
        return candidate.split("#", 1)[0].strip()

    @staticmethod
    def _direct_batch_authoritative_item_key(item_name: str | None) -> frozenset[str]:
        """Token-based identity key for an item name, used to scope the
        remembered-authoritative-URL cache to the specific item it was
        validated for (see _remember_direct_batch_authoritative_url)."""
        return frozenset(URLDiscoverer._significant_tokens(str(item_name or "")))

    def _remember_direct_batch_authoritative_url(self, url: str | None, item_name: str | None = None) -> None:
        normalized = self._normalize_direct_batch_authoritative_url(url)
        if not normalized:
            return
        if not hasattr(self, "_request_cache_lock"):
            self._request_cache_lock = Lock()
        item_key = self._direct_batch_authoritative_item_key(item_name)
        with self._request_cache_lock:
            existing = getattr(self, "_direct_batch_authoritative_urls", None)
            if not isinstance(existing, dict):
                # Upgrade a legacy/externally-assigned flat set() (or missing attr)
                # in place rather than dropping whatever it already held.
                upgraded: dict[str, set[frozenset[str]]] = {}
                if isinstance(existing, set):
                    for legacy_url in existing:
                        upgraded[legacy_url] = {frozenset()}
                self._direct_batch_authoritative_urls = upgraded
            self._direct_batch_authoritative_urls.setdefault(normalized, set()).add(item_key)
        self._record_url_recommendation_source(normalized, "direct_batch")

    def _record_url_recommendation_source(self, url: str | None, source: str) -> None:
        """Minimal provenance tracking: which independent discovery mechanism(s)
        recommended a given URL for *some* item this run. Foundational
        corroboration plumbing -- deliberately cheap (no new network calls, just
        tagging URLs a source already produced) so it can be extended to more
        consumers later without cost concerns. See
        _url_recommendation_source_count for the one consumer wired in today
        (attraction tie-breaking)."""
        normalized = str(url or "").strip()
        if not normalized:
            return
        if not hasattr(self, "_url_recommendation_sources"):
            self._url_recommendation_sources = {}
        if not hasattr(self, "_request_cache_lock"):
            self._request_cache_lock = Lock()
        with self._request_cache_lock:
            self._url_recommendation_sources.setdefault(normalized, set()).add(source)

    def _url_recommendation_source_count(self, url: str | None) -> int:
        normalized = str(url or "").strip()
        if not normalized:
            return 0
        sources = getattr(self, "_url_recommendation_sources", {})
        return len(sources.get(normalized, ()))

    def _is_remembered_direct_batch_authoritative_url(self, url: str | None, item_name: str | None = None) -> bool:
        normalized = self._normalize_direct_batch_authoritative_url(url)
        if not normalized:
            return False
        remembered = getattr(self, "_direct_batch_authoritative_urls", None)
        if isinstance(remembered, set):
            # Legacy/externally-assigned flat set of URLs with no per-item context
            # (e.g. a test fixture built before this cache carried identity info).
            return normalized in remembered
        if not isinstance(remembered, dict):
            return False
        item_keys = remembered.get(normalized)
        if not item_keys:
            return False
        if item_name is None:
            # Caller only wants to know whether this URL was validated as
            # authoritative for *some* item this run (e.g. deciding whether a
            # bulk prewarm fetch is redundant) -- not attributing it to one.
            return True
        return self._direct_batch_authoritative_item_key(item_name) in item_keys

    @staticmethod
    def _batch_cache_key(destination: str, context: str = "") -> str:
        dest = str(destination or "").strip().lower()
        ctx = str(context or "").strip().lower()
        return f"{dest}||{ctx}" if ctx else dest

    @staticmethod
    def _capture_slug(value: str) -> str:
        token = re.sub(r"[^a-z0-9]+", "-", str(value or "").strip().lower()).strip("-")
        return token or "unknown"

    def _direct_batch_html_capture_dir(self) -> Path | None:
        if not bool(getattr(self, "_direct_batch_html_capture_enabled", DEFAULT_DIRECT_BATCH_HTML_CAPTURE_ENABLED)):
            return None
        run_output_dir = getattr(self, "_run_output_dir", None)
        if not run_output_dir:
            return None
        subdir = str(
            getattr(
                self,
                "_direct_batch_html_capture_subdir",
                DEFAULT_DIRECT_BATCH_HTML_CAPTURE_SUBDIR,
            )
            or DEFAULT_DIRECT_BATCH_HTML_CAPTURE_SUBDIR
        ).strip()
        if not subdir:
            return None
        sub_path = Path(subdir)
        if sub_path.is_absolute():
            return sub_path
        return Path(run_output_dir) / sub_path

    def _persist_direct_batch_html_capture(
        self,
        *,
        destination: str,
        dates: str,
        kind: str,
        key: str,
        system_prompt: str,
        user_prompt: str,
        query: str,
        html: str,
        rows: list[dict[str, Any]],
        provider: str = "",
    ) -> None:
        capture_dir = self._direct_batch_html_capture_dir()
        if capture_dir is None:
            return

        dest_slug = self._capture_slug(destination)
        dates_slug = self._capture_slug(dates) if str(dates or "").strip() else "no-dates"
        kind_slug = self._capture_slug(kind)
        key_slug = self._capture_slug(key)
        stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        base_name = f"{dest_slug}.{kind_slug}.{dates_slug}.{key_slug}.{stamp}"
        html_path = capture_dir / f"{base_name}.html"
        meta_path = capture_dir / f"{base_name}.meta.json"

        html_payload = str(html or "")
        query_text = str(query or "")
        if query_text and "<div class=\"direct_batch_query\">" not in html_payload.lower():
            html_payload = (
                "<div class=\"direct_batch_query\">"
                f"<strong>Query:</strong> {html_lib.escape(query_text)}"
                "</div>\n\n"
                f"{html_payload}"
            )

        payload = {
            "captured_at_utc": stamp,
            "destination": str(destination or ""),
            "dates": str(dates or ""),
            "kind": str(kind or ""),
            "cache_key": str(key or ""),
            "system_prompt": str(system_prompt or ""),
            "user_prompt": str(user_prompt or ""),
            "query": query_text,
            "row_count": len(rows or []),
            "rows": [dict(row) for row in rows if isinstance(row, dict)],
            "html_file": html_path.name,
            # Which client actually supplied the winning html/rows -- empty
            # when neither the primary nor the fallback produced anything
            # (a class name, e.g. "GrokSearch"/"ClaudeSearch", otherwise).
            # See _fetch_direct_batch_html_rows's cross-provider retry.
            "provider": str(provider or ""),
        }

        try:
            capture_dir.mkdir(parents=True, exist_ok=True)
            html_path.write_text(html_payload, encoding="utf-8")
            meta_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        except Exception as exc:
            logger.warning("Direct-batch HTML capture write failed for %s (%s): %s", destination, kind, exc)

    def replay_html_capture_directory(
        self,
        capture_dir: str | Path,
        output_path: str | Path | None = None,
    ) -> list[dict[str, Any]]:
        target = Path(capture_dir)
        if not target.exists():
            return []

        entries: list[dict[str, Any]] = []
        for html_path in sorted(target.glob("*.html")):
            meta_path = html_path.with_name(f"{html_path.stem}.meta.json")
            meta: dict[str, Any] = {}
            if meta_path.exists():
                try:
                    with meta_path.open("r", encoding="utf-8") as fh:
                        meta = json.loads(fh.read() or "{}")
                except Exception:
                    meta = {}

            try:
                html_text = html_path.read_text(encoding="utf-8")
            except Exception:
                continue

            rows = self._direct_batch_rows_from_html(html_text)
            clickable_links: list[str] = []
            for row in rows:
                label = str(row.get("title") or row.get("name") or "Item").strip() or "Item"
                source_type = str(row.get("source_type") or "official").strip() or "official"
                for url in self._direct_batch_row_url_candidates(row):
                    source_label = "Source" if url == self._normalize_direct_batch_authoritative_url(str(row.get("url") or "")) else "Maps" if self._is_google_maps_candidate_url(url) else "Link"
                    clickable_links.append(
                        f'<a href="{html_lib.escape(url, quote=True)}" title="{html_lib.escape(source_type)}">'
                        f'{html_lib.escape(label)} [{html_lib.escape(source_label)} / {html_lib.escape(source_type)}]</a>'
                    )

            entries.append(
                {
                    "destination": str(meta.get("destination") or ""),
                    "dates": str(meta.get("dates") or ""),
                    "kind": str(meta.get("kind") or ""),
                    "query": str(meta.get("query") or ""),
                    "html_file": html_path.name,
                    "meta_file": meta_path.name,
                    "rows": [dict(row) for row in rows if isinstance(row, dict)],
                    "clickable_links": clickable_links,
                }
            )

        if output_path is not None:
            output_file = Path(output_path)
            output_file.parent.mkdir(parents=True, exist_ok=True)
            report_html = ["<html><head><meta charset='utf-8'><title>URL Discovery Replay Report</title></head><body>"]
            for entry in entries:
                destination = entry.get("destination") or "Unknown destination"
                dates = entry.get("dates") or ""
                kind = entry.get("kind") or ""
                report_html.append(f"<h2>{html_lib.escape(destination)} | {html_lib.escape(kind)} | {html_lib.escape(dates)}</h2>")
                report_html.append(f"<p><strong>Query:</strong> {html_lib.escape(entry.get('query') or '')}</p>")
                for link in entry.get("clickable_links") or []:
                    report_html.append(f"<p>{link}</p>")
                if not (entry.get("clickable_links") or []):
                    report_html.append("<p>No clickable links</p>")
            report_html.append("</body></html>")
            output_file.write_text("\n".join(report_html), encoding="utf-8")

        return entries

    def _alltrails_direct_batch_query(self, dest_name: str, dates: str = "") -> str:
        max_miles = max(0.5, float(getattr(self, "_max_trail_miles", DEFAULT_MAX_TRAIL_MILES) or DEFAULT_MAX_TRAIL_MILES))
        date_clause = f" for dates {dates}" if str(dates or "").strip() else ""
        return (
            f"Generate clickable hikes from AllTrails for {dest_name}{date_clause} with trail length {max_miles:g} miles or less. "
            "Keep only likely-open routes with strong ratings and drop any item without a reliable link."
        )

    @staticmethod
    def _attraction_direct_batch_query(dest_name: str, dates: str = "") -> str:
        date_clause = f" ({dates})" if str(dates or "").strip() else ""
        return (
            "Generate lists of local points of interest, cultural landmarks, and tourist attractions "
            f"for {dest_name}{date_clause}, excluding hikes, with clickable links to source material and corresponding Google Maps content. "
            "Keep only highly rated items (>4.3), include a mixture of experiences, and keep only places likely open on the indicated dates. "
            "Include only suggestions with reliable clickable links."
        )

    @staticmethod
    def _attraction_maps_area_query(dest_name: str, dates: str = "") -> str:
        date_clause = f" ({dates})" if str(dates or "").strip() else ""
        return (
            "Find Google Maps attractions and things-to-do entries "
            f"for {dest_name}{date_clause}. "
            "Return item-specific links only and avoid generic destination landing pages."
        )

    def _batch_rating_floor(self) -> str:
        """Minimum rating to request, relaxed on a low-cost brief.

        4.3 is a high bar, and it interacts badly with a budget: a friterie or
        imbiss that locals rate 4.1 is exactly what "low-cost, no fine dining"
        wants, while 4.3-and-above skews toward destination restaurants.
        Removing the floor entirely was the first attempt and went too far --
        it is a genuine quality gate, and a test rightly held it in place.

        Lowering it rather than dropping it widens the cheap pool without
        admitting badly-reviewed places.
        """
        budget_text = re.sub(r"[-_]+", " ", str(getattr(self, "_trip_budget", "") or "").lower())
        low = any(
            k in budget_text
            for k in ("budget", "cheap", "economy", "value", "frugal", "low cost",
                      "inexpensive", "affordable", "modest", "shoestring", "no fine dining")
        )
        return "4.0" if low else "4.3"

    def _batch_price_clause(self) -> str:
        """The budget instruction, shared by BOTH restaurant prompts.

        There are two: a system prompt that sets the output contract and the
        item count, and a user prompt naming the destination. The budget
        wording was added to the user prompt only, so the system prompt went on
        saying "Keep only highly rated items (>4.3)" -- a rating floor with no
        price guidance, which is exactly what selects for fine dining. Half the
        instruction was arguing with the other half.
        """
        budget_text = re.sub(r"[-_]+", " ", str(getattr(self, "_trip_budget", "") or "").lower())
        if any(
            k in budget_text
            for k in ("budget", "cheap", "economy", "value", "frugal", "low cost",
                      "inexpensive", "affordable", "modest", "shoestring", "no fine dining")
        ):
            return (
                "PRICE IS THE PRIMARY FILTER. Return everyday, inexpensive places at the "
                "$ and $$ price levels: friteries, imbiss and street-food counters, market "
                "halls, bakeries and sandwich shops, neighbourhood taverns and family "
                "trattorias, student and worker canteens. At least half the list must be $. "
                "EXCLUDE fine dining, tasting menus, Michelin-starred and hotel restaurants "
                "entirely -- they are wrong for this trip no matter how well reviewed. "
            )
        if any(k in budget_text for k in ("luxury", "premium", "splurge", "upscale", "high end")):
            return "Favour upscale places, $$$ and $$$$ price levels. "
        return ""

    def _restaurant_direct_batch_query(self, dest_name: str, dates: str = "") -> str:
        date_clause = f" ({dates})" if str(dates or "").strip() else ""
        # The budget belongs in the REQUEST. Asking for "highly rated (>4.3)"
        # with no price guidance selects for fine dining, which is how a
        # low-cost brief produced Ciel Bleu, De Kas, Yamazato and RIJKS at
        # Amsterdam. Filtering afterwards could only make the section smaller,
        # never cheaper -- Amsterdam ended with two restaurants, one of them
        # $$$$, because there were no inexpensive candidates to keep.
        price_clause = self._batch_price_clause()
        return (
            "Generate a list of local restaurants "
            f"for {dest_name}{date_clause} with clickable links to source material and corresponding Google Maps content. "
            f"{price_clause}"
            "For EVERY item state the price level as exactly one of $, $$, $$$ or $$$$, "
            "and state the cuisine as a food style (Thai, Vietnamese, bakery, brewpub) -- "
            "never a city or district name. "
            "Keep only well-reviewed items, include cuisine variety, and keep only places likely open on the indicated dates. "
            "Include only suggestions with reliable clickable links."
        )

    def _en_route_direct_batch_query(self, dest_name: str, dates: str = "", origin_name: str = "") -> str:
        date_clause = f" ({dates})" if str(dates or "").strip() else ""
        origin = str(origin_name or "").strip()
        route_clause = (
            f"for the drive from {origin} to {dest_name}{date_clause}"
            if origin
            else f"for the drive into {dest_name}{date_clause}"
        )
        min_rating = float(getattr(self, "_place_interest_min_rating", DEFAULT_PLACE_INTEREST_MIN_RATING))
        min_votes = int(getattr(self, "_place_interest_min_votes", DEFAULT_PLACE_INTEREST_MIN_VOTES))
        return (
            "Generate a separate en-route list of scenic stopovers, viewpoints, and quick cultural detours "
            f"{route_clause}, with clickable links and matching Google Maps references. "
            "Prefer specific official or authoritative pages for each stop over generic destination landing pages, park home pages, or visitor-center pages. "
            "Only include options with detour off route of 20 minutes or less. "
            f"Only include places rated {min_rating:g}+ by at least {min_votes} reviewers. "
            "Exclude gas stations, convenience stores, welcome centers, and rest areas. "
            "Include only suggestions with reliable clickable links."
        )

    def _harvest_section_for(self, cache: dict[str, Any]) -> str:
        """Which section a harvest cache dict belongs to, by identity.

        Identity rather than a parameter threaded through every call site: the
        four caches are long-lived attributes and the callers already pass the
        dict itself. An unrecognised cache returns "", which counts as a cold
        lookup — under-reporting warmth is the direction that fails safe, since
        the number exists to explain why a run was *cheap*.
        """
        for section, attr in HARVEST_CACHE_SECTIONS.items():
            if getattr(self, attr, None) is cache:
                return section
        return ""

    def _note_harvest_lookup(
        self, cache: dict[str, Any], key: str, *, served_from_cache: bool
    ) -> None:
        """Classify one harvest lookup as warm, repeat, or cold.

        - **warm** — served from a harvest this run inherited from disk. The only
          one that means "this route was already paid for on an earlier day".
        - **repeat** — served from a harvest this same run performed. Free, and
          not evidence about the route.
        - **cold** — about to buy a search.

        The distinction is the whole point. A plain hit rate counts warm and
        repeat together and therefore rises with the number of destinations that
        share a query, which has nothing to do with how well-travelled the route
        is.
        """
        # Same `hasattr` guard the rest of this class uses: instances are built
        # in more ways than __init__, and a counter must never be the reason a
        # harvest raises.
        if not hasattr(self, "_harvest_freshness"):
            self._harvest_freshness = {"warm": 0, "repeat": 0, "cold": 0}
        if not hasattr(self, "_harvest_preloaded_keys"):
            self._harvest_preloaded_keys = {}
        if not served_from_cache:
            self._harvest_freshness["cold"] += 1
            return
        section = self._harvest_section_for(cache)
        preloaded = self._harvest_preloaded_keys.get(section) or set()
        self._harvest_freshness["warm" if key in preloaded else "repeat"] += 1

    def forget_failures(self) -> dict[str, int]:
        """Drop cached *negative* results so a retry can genuinely try again.

        **This is what made the retry pass unable to resolve anything.** The
        pass reuses this instance so in-memory caches survive -- deliberately,
        so it does not re-buy work already paid for -- but the caches hold
        failures as well as successes. A lookup that failed the first time
        returned its cached failure on the second, so a retry re-derived the
        first pass's conclusions and reported them as a fresh result. Measured
        across 9 runs and 58 destination-instances before this existed: 27
        retries, 0 resolved.

        **Only negatives go.** A URL that was found and verified is still found
        and verified; re-buying it would make a retry cost as much as a run.
        What is forgotten is the record of *not* finding something, which is the
        only thing a second attempt could possibly change:

        - `_url_cache` entries holding `None` -- a per-item search that found
          nothing. Module-level and shared across instances, so this clears
          them for the process, which is what a retry within that process wants.
        - the direct-batch HTML failure cooldowns, which exist precisely to stop
          a *repeat within one pass* and should not outlive the pass that set them
        - verification, AllTrails and Wayback fetch results whose ok-flag is
          false

        The persistent harvest caches are untouched and need no filtering: an
        empty harvest is already never written to them, on the deliberate
        grounds that "an empty batch is often a transient upstream hiccup, not
        'this destination has no attractions'".

        Returns what it dropped, so a caller can record whether forgetting
        anything was even possible.
        """
        dropped = {"searches": 0, "batch_cooldowns": 0, "verifications": 0, "fetches": 0}

        if not hasattr(self, "_request_cache_lock"):
            self._request_cache_lock = Lock()

        with self._request_cache_lock:
            for key in [k for k, v in _url_cache.items() if not v]:
                del _url_cache[key]
                dropped["searches"] += 1

            cooldowns = getattr(self, "_direct_batch_html_failure_ts", None)
            if isinstance(cooldowns, dict):
                dropped["batch_cooldowns"] = len(cooldowns)
                cooldowns.clear()

            verify = getattr(self, "_verify_url_cache", None)
            if isinstance(verify, dict):
                for key in [k for k, v in verify.items() if not _cached_ok(v)]:
                    del verify[key]
                    dropped["verifications"] += 1

            for attr in ("_alltrails_fetch_cache", "_wayback_fetch_cache"):
                cache = getattr(self, attr, None)
                if not isinstance(cache, dict):
                    continue
                for key in [k for k, v in cache.items() if not _cached_ok(v)]:
                    del cache[key]
                    dropped["fetches"] += 1

        return dropped

    def harvest_freshness(self) -> dict[str, Any]:
        """How much of this run's harvesting was already on disk when it started.

        R10 in `commercialization-development-approach.md` is the reason this is
        recorded rather than merely computable: a cost distribution built from
        runs that are mostly warm, or mostly cold, is not the same distribution,
        and a run that does not say which one it was cannot be re-read later.
        Cheap to record now; impossible to reconstruct from history.
        """
        counts = dict(getattr(self, "_harvest_freshness", None) or
                      {"warm": 0, "repeat": 0, "cold": 0})
        lookups = counts["warm"] + counts["repeat"] + counts["cold"]
        billable = counts["warm"] + counts["cold"]
        return {
            **counts,
            "lookups": lookups,
            # Of the harvests this run would have had to buy had nothing been
            # cached, the share it did not. `repeat` is excluded from the
            # denominator: a second lookup of a key bought moments ago was never
            # going to be a separate purchase.
            "warm_ratio": round(counts["warm"] / billable, 4) if billable else None,
        }

    def _get_direct_batch_rows_for_destination(
        self,
        *,
        cache: dict[str, list[dict[str, Any]]],
        destination: str,
        query: str,
        cache_context: str = "",
    ) -> list[dict[str, Any]]:
        if not hasattr(self, "_request_cache_lock"):
            self._request_cache_lock = Lock()
        if not hasattr(self, "_search"):
            return []
        key = self._batch_cache_key(destination, cache_context)
        if not key:
            return []

        with self._request_cache_lock:
            cached = cache.get(key)
            if cached is not None:
                self._note_harvest_lookup(cache, key, served_from_cache=True)
                return cached

        self._note_harvest_lookup(cache, key, served_from_cache=False)
        self._note_fallback_call_site("direct_batch_rows")
        rows = self._search_cached(query, count=self._direct_link_batch_limit())
        normalized = [dict(row) for row in rows if isinstance(row, dict)]

        with self._request_cache_lock:
            cache[key] = normalized
        return normalized

    def _get_alltrails_direct_batch_rows_for_destination(
        self, dest_name: str, dates: str = "", seed_names: list[str] | None = None
    ) -> list[dict[str, Any]]:
        if not hasattr(self, "_alltrails_direct_batch_cache"):
            self._alltrails_direct_batch_cache = {}
        html_rows = self._get_direct_batch_html_rows_for_destination(
            cache=self._alltrails_direct_batch_cache,
            destination=dest_name,
            dates=dates,
            kind="trail",
            seed_names=seed_names,
        )
        if html_rows:
            return html_rows

        return self._get_direct_batch_rows_for_destination(
            cache=self._alltrails_direct_batch_cache,
            destination=dest_name,
            query=self._alltrails_direct_batch_query(dest_name, dates),
            cache_context=dates,
        )

    # The attraction batch prompt asks for `direct_link_batch_count` items
    # (20 by default). Naming more than that many wanted items invites the
    # model to drop some silently, so the hint list is capped to fit.
    _MAX_BATCH_ITEM_HINTS = 20

    def _prewarm_attraction_batch_with_itinerary_items(
        self,
        *,
        ai: dict[str, Any],
        dest_name: str,
        dest_dates: str,
        seed_names: list[str] | None,
    ) -> None:
        """Fetch the attraction batch once, naming the itinerary's own items.

        Seeds come first: they are the traveller's explicit asks and must not
        be crowded out of the cap by generated names.
        """
        # Route each name to the batch that can actually supply it.
        #
        # The attraction prompt says "excluding hikes" in its own text. The
        # first version of this prewarm fed it every itinerary item, hikes
        # included, and the 2026-08-22 baseline run shows exactly what that
        # produced at Zion: five names asked for, four of them hikes, and the
        # model correctly honoured its exclusion and returned ONE of them --
        # while the hint list displaced slots that would have held real
        # attractions. `direct_batch_no_match` went UP, 82 -> 108.
        #
        # Trail-like names go to the trail batch, which exists for them and
        # whose prompt asks for hikes.
        attraction_hints: list[str] = []
        trail_hints: list[str] = []
        seen: set[str] = set()
        by_name: dict[str, dict[str, Any]] = {}
        for item in (ai.get("top_attractions", []) or []):
            if isinstance(item, dict):
                by_name[str(item.get("name", "") or "")] = item
        for name in list(seed_names or []) + list(by_name):
            cleaned = str(name or "").replace("*", "").strip()
            key = re.sub(r"[^a-z0-9]+", " ", cleaned.lower()).strip()
            if not cleaned or key in seen:
                continue
            seen.add(key)
            item = by_name.get(name) or {}
            is_trail = self._is_trail_like_attraction(
                cleaned,
                str(item.get("type", "") or ""),
                self._attraction_trail_context(item),
            )
            target = trail_hints if is_trail else attraction_hints
            if len(target) < self._MAX_BATCH_ITEM_HINTS:
                target.append(cleaned)
        # FIFTH leak in the trails switch, and this one was introduced by the
        # hint-routing change itself: routing trail-like names to the trail
        # batch prewarms that batch, so it ran for all ten destinations on the
        # 2026-08-24 Core run with trails.enabled false -- 18 capture files,
        # 9 paid calls, roughly $0.57 of a $1.77 run spent on a disabled
        # category.
        #
        # Guarded here rather than at yet another call site: this is where the
        # trail fetch is *initiated*, and _retain_discovered_url already
        # enforces the switch on anything a trail URL reaches. Between the two
        # -- nothing starts a trail fetch, nothing retains a trail URL -- the
        # category is closed at both ends rather than along the path.
        if bool(getattr(self, "_disable_trails", False)):
            trail_hints = []
        for hints, fetch, label in (
            (attraction_hints, self._get_attraction_direct_batch_rows_for_destination, "attraction"),
            (trail_hints, self._get_alltrails_direct_batch_rows_for_destination, "trail"),
        ):
            if not hints:
                continue
            try:
                fetch(dest_name, dest_dates, seed_names=hints)
            except Exception as exc:  # pragma: no cover - defensive only
                # A prewarm is an optimisation. If it fails, the ordinary lazy
                # fetch still runs and discovery proceeds exactly as before.
                logger.info("%s batch prewarm failed for %s: %s", label, dest_name, exc)

    def _get_attraction_direct_batch_rows_for_destination(
        self, dest_name: str, dates: str = "", seed_names: list[str] | None = None
    ) -> list[dict[str, Any]]:
        if not hasattr(self, "_attraction_direct_batch_cache"):
            self._attraction_direct_batch_cache = {}
        html_rows = self._get_direct_batch_html_rows_for_destination(
            cache=self._attraction_direct_batch_cache,
            destination=dest_name,
            dates=dates,
            kind="attraction",
            seed_names=seed_names,
        )
        if html_rows:
            return html_rows
        return self._get_direct_batch_rows_for_destination(
            cache=self._attraction_direct_batch_cache,
            destination=dest_name,
            query=self._attraction_direct_batch_query(dest_name, dates),
            cache_context=dates,
        )

    def _get_restaurant_direct_batch_rows_for_destination(self, dest_name: str, dates: str = "", lodging_location: str = "") -> list[dict[str, Any]]:
        if not hasattr(self, "_restaurant_direct_batch_cache"):
            self._restaurant_direct_batch_cache = {}
        html_rows = self._get_direct_batch_html_rows_for_destination(
            cache=self._restaurant_direct_batch_cache,
            destination=dest_name,
            dates=dates,
            kind="restaurant",
            lodging_location=lodging_location,
        )
        if html_rows:
            return html_rows

        return self._get_direct_batch_rows_for_destination(
            cache=self._restaurant_direct_batch_cache,
            destination=dest_name,
            query=self._restaurant_direct_batch_query(dest_name, dates),
            cache_context=f"{dates}|search",
        )

    def _get_en_route_direct_batch_rows_for_destination(
        self,
        dest_name: str,
        dates: str = "",
        origin_name: str = "",
        seed_names: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        if not hasattr(self, "_en_route_direct_batch_cache"):
            self._en_route_direct_batch_cache = {}
        html_rows = self._get_direct_batch_html_rows_for_destination(
            cache=self._en_route_direct_batch_cache,
            destination=dest_name,
            dates=dates,
            kind="en_route_stop",
            origin_name=origin_name,
            seed_names=seed_names,
        )
        if html_rows:
            return html_rows

        return self._get_direct_batch_rows_for_destination(
            cache=self._en_route_direct_batch_cache,
            destination=dest_name,
            query=self._en_route_direct_batch_query(dest_name, dates, origin_name),
            cache_context=f"{dates}|search",
        )

    @staticmethod
    def _direct_batch_seed_hint_clauses(
        seed_names: list[str] | None, *, noun: str = "item"
    ) -> tuple[str, str]:
        """Build (system_clause, user_clause) text fragments that ask a
        direct-batch harvest call to specifically verify and include the
        traveler's manifest seed names, or ("", "") when there are none --
        so prompt construction stays byte-identical to before this existed
        whenever a destination has no seeds.

        Root cause this addresses: named, well-documented seeds (e.g.
        "Sunrise Point" at Bryce Canyon, "Imogene Pass" at Telluride) were
        repeatedly absent from the raw harvest candidate list entirely --
        not a matching/verification bug, but the harvest prompt itself
        having no mechanism at all to surface seeds as candidates, so an
        obscure-but-real seed had no real chance against more famous nearby
        attractions for the model's limited slot budget. Naming the seeds
        explicitly gives the model a concrete reason to specifically look
        for and include them, upstream of the existing verification/matching
        trust boundary (which this does not change).
        """
        names = [str(s or "").strip() for s in (seed_names or []) if str(s or "").strip()]
        if not names:
            return "", ""
        joined = "; ".join(names)
        system_clause = (
            f" The traveler specifically asked about these {noun}s -- verify each is a real, "
            "currently operating place and include any that check out among the items above, "
            f"even if less well-known than your other picks: {joined}."
        )
        user_clause = f" Also specifically verify and include, if real: {joined}."
        return system_clause, user_clause

    def _direct_batch_html_prompt(
        self,
        *,
        kind: str,
        dest_name: str,
        dates: str = "",
        origin_name: str = "",
        lodging_location: str = "",
        seed_names: list[str] | None = None,
    ) -> tuple[str, str] | None:
        date_clause = f" ({dates})" if str(dates or "").strip() else ""
        if kind == "trail":
            items_per_day = int(
                getattr(self, "_trail_direct_batch_items_per_day", DEFAULT_TRAIL_DIRECT_BATCH_ITEMS_PER_DAY)
                or DEFAULT_TRAIL_DIRECT_BATCH_ITEMS_PER_DAY
            )
            count = self._day_scaled_direct_batch_count(dates, items_per_day=items_per_day)
            max_miles = max(0.5, float(getattr(self, "_max_trail_miles", DEFAULT_MAX_TRAIL_MILES) or DEFAULT_MAX_TRAIL_MILES))
            min_rating = float(getattr(self, "_alltrails_rating_min", DEFAULT_ALLTRAILS_RATING_MIN))
            min_votes = int(getattr(self, "_alltrails_rating_min_votes", DEFAULT_ALLTRAILS_RATING_MIN_VOTES))
            system_prompt = (
                "Return HTML only. Emit one <h2> and one <ul> with exactly "
                f"{count} <li> hike items from AllTrails for the requested destination. "
                "Each <li> must begin with the trail name and include at least one AllTrails <a href=...> link; "
                "an additional official/source link is optional when available. "
                "After the links, include the trail's rating as a clear numeric value like '4.6/5' and its "
                "round-trip distance in miles like '3.2 mi', then a short descriptive note (8-15 words) about "
                "the trail's terrain or highlights, when available. "
                f"Keep only likely-open hikes of {max_miles:g} miles or less rated {min_rating:g}+ with at least {min_votes} reviews. "
                "Exclude generic listings and drop any item without a reliable trail-specific AllTrails link."
            )
            user_prompt = (
                f"Generate clickable hikes from AllTrails for {dest_name}{date_clause}. "
                "Include a rating, distance in miles, and a short descriptive note for each item when available."
            )
            seed_system, seed_user = self._direct_batch_seed_hint_clauses(seed_names, noun="hike")
            return system_prompt + seed_system, user_prompt + seed_user

        if kind == "attraction":
            items_per_day = int(
                getattr(self, "_attraction_direct_batch_items_per_day", DEFAULT_ATTRACTION_DIRECT_BATCH_ITEMS_PER_DAY)
                or DEFAULT_ATTRACTION_DIRECT_BATCH_ITEMS_PER_DAY
            )
            count = self._day_scaled_direct_batch_count(dates, items_per_day=items_per_day)
            system_prompt = (
                "Return HTML only. Emit one <h2> and one <ul> with exactly "
                f"{count} <li> attraction items for the requested destination. "
                "Exclude hikes and trails — those are covered separately. "
                "Each <li> must begin with the attraction name and include up to two links: "
                "<a href=...>Source</a> for the attraction's official or authoritative page, "
                "and <a href=\"https://www.google.com/maps/search/?api=1&query=Attraction+Name+Address+City+State\">Maps</a> as a precise Google Maps place or search link. "
                "Use the Maps link to target a specific place, not a generic destination overview. "
                "Include the attraction's rating as a clear numeric value like '4.7/5' or '4.7 stars' after the links, "
                "then a short descriptive note (8-15 words) about what makes the attraction worth visiting, when available. "
                "Keep only highly rated items (>4.3), include a mixture of experiences, "
                "and keep only places likely open on the indicated dates. "
                "Avoid generic destination listing pages, general travel guides, and broad area pages."
            )
            user_prompt = (
                "Generate a list of local points of interest, cultural landmarks, and tourist attractions "
                f"for {dest_name}{date_clause}, excluding hikes, "
                "with clickable links to source material and corresponding Google Maps content. "
                "Prefer specific place pages and precise Google Maps links over generic listing pages. "
                "Include a rating and a short descriptive note for each item when available, using a clear numeric format for the rating. "
                "Keep only highly rated items (>4.3), include a mixture of experiences, "
                "and keep only places likely open on the indicated dates. "
                "Include only suggestions with reliable clickable links."
            )
            seed_system, seed_user = self._direct_batch_seed_hint_clauses(seed_names, noun="attraction")
            return system_prompt + seed_system, user_prompt + seed_user

        if kind == "restaurant":
            count = int(
                getattr(
                    self,
                    "_restaurant_direct_batch_item_count",
                    DEFAULT_RESTAURANT_DIRECT_BATCH_ITEM_COUNT,
                )
                or DEFAULT_RESTAURANT_DIRECT_BATCH_ITEM_COUNT
            )
            system_prompt = (
                "Return HTML only. Emit one <h2> and one <ul> with exactly "
                f"{count} <li> restaurant items for the requested destination. "
                "Each <li> must begin with the restaurant name and include up to two links: "
                "<a href=...>Source</a> for the restaurant's own website or TripAdvisor page, "
                "and <a href=\"https://www.google.com/maps/search/?api=1&query=Restaurant+Name+Address+City+State\">Maps</a> as an address-qualified Google Maps search link. "
                "Include the restaurant's rating as a clear numeric value like '4.7/5' or '4.7 stars', a price indicator like '$$', '$$$', or 'moderate', and the cuisine or restaurant type (e.g. 'Italian', 'New American', 'Poke') when available, "
                "then a short descriptive note (8-15 words) about the food, atmosphere, or signature dishes -- real prose that adds detail beyond the cuisine or price, when available. "
                f"{self._batch_price_clause()}"
                f"Keep only items rated above {self._batch_rating_floor()}, include cuisine variety, "
                "and keep only likely-open, high-confidence options. "
                "Avoid generic destination listing pages."
            )
            location_clause = lodging_location if lodging_location else dest_name
            user_prompt = (
                f"Generate a list of local restaurants near {location_clause}{date_clause} "
                "with clickable links to source material and corresponding Google Maps content. "
                "Include a rating, price indicator, and the cuisine or restaurant type for each item when available, using a clear numeric or price format, "
                "and a short descriptive note about the food, atmosphere, or signature dishes for each item when available. "
                f"{self._batch_price_clause()}"
                f"Keep only items rated above {self._batch_rating_floor()}, include cuisine variety, "
                "and keep only places likely open on the indicated dates. "
                "Include only suggestions with reliable clickable links."
            )
            return system_prompt, user_prompt

        if kind == "en_route_stop":
            count = int(
                getattr(
                    self,
                    "_en_route_direct_batch_item_count",
                    DEFAULT_EN_ROUTE_DIRECT_BATCH_ITEM_COUNT,
                )
                or DEFAULT_EN_ROUTE_DIRECT_BATCH_ITEM_COUNT
            )
            min_rating = float(getattr(self, "_place_interest_min_rating", DEFAULT_PLACE_INTEREST_MIN_RATING))
            min_votes = int(getattr(self, "_place_interest_min_votes", DEFAULT_PLACE_INTEREST_MIN_VOTES))
            origin = str(origin_name or "").strip()
            route_clause = (
                f"for the drive from {origin} to {dest_name}{date_clause}"
                if origin
                else f"for the drive into {dest_name}{date_clause}"
            )
            system_prompt = (
                "Return HTML only. Emit one <h2> and one <ul> with exactly "
                f"{count} <li> en-route stopovers for the requested destination drive. "
                "Each <li> must begin with the stop name, include a short note, and explicitly state detour distance and detour time in plain text. "
                "Use a format like 'Stop Name - quick note - detour 8 mi / 15 min'. "
                "Also include up to two links: "
                "<a href=...>Source</a> for the stop's specific official or authoritative page, not a generic destination landing page or park home page, "
                "and <a href=\"https://www.google.com/maps/search/?api=1&query=Stop+Name+Address+City+State\">Maps</a> as an address-qualified Google Maps search link. "
                "Only include options with detour off route of 20 minutes or less. "
                f"Only include places rated {min_rating:g}+ by at least {min_votes} reviewers. "
                "Exclude gas stations, convenience stores, welcome centers, and rest areas. "
                "Keep only quick, realistic, likely-open scenic/cultural stopovers with reliable links."
            )
            user_prompt = (
                f"Generate en-route stopovers {route_clause} "
                "with clickable links and matching Google Maps references. "
                "Prefer specific official or authoritative pages for each stop over generic destination landing pages, park home pages, or visitor-center pages. "
                "Only include options with detour off route of 20 minutes or less. "
                f"Only include places rated {min_rating:g}+ by at least {min_votes} reviewers. "
                "Exclude gas stations, convenience stores, welcome centers, and rest areas. "
                "Include only suggestions with reliable clickable links."
            )
            seed_system, seed_user = self._direct_batch_seed_hint_clauses(seed_names, noun="stop")
            return system_prompt + seed_system, user_prompt + seed_user

        return None

    @staticmethod
    def _direct_batch_seed_line_suffix(seed_names: list[str] | None) -> str:
        """Per-destination-line counterpart to _direct_batch_seed_hint_clauses
        for the multi-destination prompt: appends the traveler's seed names
        for THIS destination only to its own dest_lines entry, or "" when it
        has none. See _direct_batch_html_prompt_multi."""
        names = [str(s or "").strip() for s in (seed_names or []) if str(s or "").strip()]
        if not names:
            return ""
        return f" -- must also verify and include, if real: {'; '.join(names)}"

    def _direct_batch_html_prompt_multi(
        self,
        *,
        kind: str,
        destinations: list[tuple[str, str]],
        seed_names_by_destination: dict[str, list[str]] | None = None,
    ) -> tuple[str, str] | None:
        """Multi-destination variant of _direct_batch_html_prompt: one call
        asking for several destinations at once, each its own
        <h2>Destination Name</h2><ul>...</ul> section -- see
        _prefetch_grouped_direct_batch / DEFAULT_DIRECT_BATCH_GROUP_SIZE.
        Deliberately does not cover en_route_stop: that kind depends on
        per-destination origin/route context that doesn't fit this shape
        cleanly, so it stays on the original one-call-per-destination path.

        seed_names_by_destination (destination name -> manifest seeds) is
        optional and only consulted for kind in {"trail", "attraction"} --
        see _direct_batch_seed_hint_clauses for why this exists. Omitted or
        empty for every destination leaves prompt text byte-identical to
        before this parameter existed.
        """
        if not destinations:
            return None

        if kind == "trail":
            max_miles = max(0.5, float(getattr(self, "_max_trail_miles", DEFAULT_MAX_TRAIL_MILES) or DEFAULT_MAX_TRAIL_MILES))
            min_rating = float(getattr(self, "_alltrails_rating_min", DEFAULT_ALLTRAILS_RATING_MIN))
            min_votes = int(getattr(self, "_alltrails_rating_min_votes", DEFAULT_ALLTRAILS_RATING_MIN_VOTES))
            items_per_day = int(
                getattr(self, "_trail_direct_batch_items_per_day", DEFAULT_TRAIL_DIRECT_BATCH_ITEMS_PER_DAY)
                or DEFAULT_TRAIL_DIRECT_BATCH_ITEMS_PER_DAY
            )
            dest_lines = [
                f"- {name}{f' ({dates})' if str(dates or '').strip() else ''}: exactly "
                f"{self._day_scaled_direct_batch_count(dates, items_per_day=items_per_day)} hikes"
                f"{self._direct_batch_seed_line_suffix((seed_names_by_destination or {}).get(name))}"
                for name, dates in destinations
            ]
            system_prompt = (
                "Return HTML only. For EACH destination listed below, emit one <h2>Destination Name</h2> "
                "(use the exact destination name given, nothing else in the header) followed by one <ul> "
                "with the specified number of <li> hike items from AllTrails for that destination only. "
                "Do not mix items between destinations. "
                "Each <li> must begin with the trail name and include at least one AllTrails <a href=...> link; "
                "an additional official/source link is optional when available. "
                "After the links, include the trail's rating as a clear numeric value like '4.6/5' and its "
                "round-trip distance in miles like '3.2 mi', then a short descriptive note (8-15 words) about "
                "the trail's terrain or highlights, when available. "
                f"Keep only likely-open hikes of {max_miles:g} miles or less rated {min_rating:g}+ with at least {min_votes} reviews. "
                "Exclude generic listings and drop any item without a reliable trail-specific AllTrails link."
            )
            if any((seed_names_by_destination or {}).get(name) for name, _dates in destinations):
                system_prompt += (
                    " Some destinations above list traveler-requested items after '--' -- verify each named "
                    "item is real and currently operating, and include any that check out among that "
                    "destination's items even if less well-known than your other picks."
                )
            user_prompt = (
                "Generate clickable hikes from AllTrails for these destinations:\n"
                + "\n".join(dest_lines)
                + "\nInclude a rating, distance in miles, and a short descriptive note for each item when available."
            )
            return system_prompt, user_prompt

        if kind == "attraction":
            items_per_day = int(
                getattr(self, "_attraction_direct_batch_items_per_day", DEFAULT_ATTRACTION_DIRECT_BATCH_ITEMS_PER_DAY)
                or DEFAULT_ATTRACTION_DIRECT_BATCH_ITEMS_PER_DAY
            )
            dest_lines = [
                f"- {name}{f' ({dates})' if str(dates or '').strip() else ''}: exactly "
                f"{self._day_scaled_direct_batch_count(dates, items_per_day=items_per_day)} attractions"
                f"{self._direct_batch_seed_line_suffix((seed_names_by_destination or {}).get(name))}"
                for name, dates in destinations
            ]
            system_prompt = (
                "Return HTML only. For EACH destination listed below, emit one <h2>Destination Name</h2> "
                "(use the exact destination name given, nothing else in the header) followed by one <ul> "
                "with the specified number of <li> attraction items for that destination only. "
                "Do not mix items between destinations. Exclude hikes and trails — those are covered separately. "
                "Each <li> must begin with the attraction name and include up to two links: "
                "<a href=...>Source</a> for the attraction's official or authoritative page, "
                "and <a href=\"https://www.google.com/maps/search/?api=1&query=Attraction+Name+Address+City+State\">Maps</a> as a precise Google Maps place or search link. "
                "Use the Maps link to target a specific place, not a generic destination overview. "
                "Include the attraction's rating as a clear numeric value like '4.7/5' or '4.7 stars' after the links, "
                "then a short descriptive note (8-15 words) about what makes the attraction worth visiting, when available. "
                "Keep only highly rated items (>4.3), include a mixture of experiences, "
                "and keep only places likely open on the indicated dates. "
                "Avoid generic destination listing pages, general travel guides, and broad area pages."
            )
            if any((seed_names_by_destination or {}).get(name) for name, _dates in destinations):
                system_prompt += (
                    " Some destinations above list traveler-requested items after '--' -- verify each named "
                    "item is real and currently operating, and include any that check out among that "
                    "destination's items even if less well-known than your other picks."
                )
            user_prompt = (
                "Generate local points of interest, cultural landmarks, and tourist attractions "
                "for these destinations, excluding hikes:\n"
                + "\n".join(dest_lines)
                + "\nInclude clickable links to source material and corresponding Google Maps content, "
                "a rating for each item when available using a clear numeric format, "
                "and a short descriptive note for each item when available. "
                "Keep only highly rated items (>4.3), include a mixture of experiences, "
                "and keep only places likely open on the indicated dates. "
                "Include only suggestions with reliable clickable links."
            )
            return system_prompt, user_prompt

        if kind == "restaurant":
            count = int(
                getattr(self, "_restaurant_direct_batch_item_count", DEFAULT_RESTAURANT_DIRECT_BATCH_ITEM_COUNT)
                or DEFAULT_RESTAURANT_DIRECT_BATCH_ITEM_COUNT
            )
            dest_lines = [
                f"- {name}{f' ({dates})' if str(dates or '').strip() else ''}"
                for name, dates in destinations
            ]
            system_prompt = (
                "Return HTML only. For EACH destination listed below, emit one <h2>Destination Name</h2> "
                "(use the exact destination name given, nothing else in the header) followed by one <ul> "
                f"with exactly {count} <li> restaurant items for that destination only. "
                "Do not mix items between destinations. "
                "Each <li> must begin with the restaurant name and include up to two links: "
                "<a href=...>Source</a> for the restaurant's own website or TripAdvisor page, "
                "and <a href=\"https://www.google.com/maps/search/?api=1&query=Restaurant+Name+Address+City+State\">Maps</a> as an address-qualified Google Maps search link. "
                "Include the restaurant's rating as a clear numeric value like '4.7/5' or '4.7 stars', a price indicator like '$$', '$$$', or 'moderate', and the cuisine or restaurant type (e.g. 'Italian', 'New American', 'Poke') when available, "
                "then a short descriptive note (8-15 words) about the food, atmosphere, or signature dishes -- real prose that adds detail beyond the cuisine or price, when available. "
                f"{self._batch_price_clause()}"
                f"Keep only items rated above {self._batch_rating_floor()}, include cuisine variety, "
                "and keep only likely-open, high-confidence options. "
                "Avoid generic destination listing pages."
            )
            user_prompt = (
                "Generate local restaurants near these destinations:\n"
                + "\n".join(dest_lines)
                + "\nInclude clickable links to source material and corresponding Google Maps content. "
                "Include a rating, price indicator, and the cuisine or restaurant type for each item when available, using a clear numeric or price format, "
                "and a short descriptive note about the food, atmosphere, or signature dishes for each item when available. "
                f"{self._batch_price_clause()}"
                f"Keep only items rated above {self._batch_rating_floor()}, include cuisine variety, "
                "and keep only places likely open on the indicated dates. "
                "Include only suggestions with reliable clickable links."
            )
            return system_prompt, user_prompt

        return None

    @staticmethod
    def _split_multi_destination_html(html_text: str) -> list[tuple[str, str]]:
        """Split a multi-destination direct-batch HTML response into
        (destination_header_text, section_html) pairs, one per <h2> section
        -- so each section can be fed independently through the existing
        single-destination _direct_batch_rows_from_html parser."""
        text = str(html_text or "")
        parts = re.split(r"<h2[^>]*>(.*?)</h2>", text, flags=re.IGNORECASE | re.DOTALL)
        sections: list[tuple[str, str]] = []
        for i in range(1, len(parts), 2):
            header = re.sub(r"<[^>]+>", "", parts[i]).strip()
            body = parts[i + 1] if i + 1 < len(parts) else ""
            if header:
                sections.append((header, body))
        return sections

    @staticmethod
    def _match_destination_section(dest_name: str, sections: list[tuple[str, str]]) -> int | None:
        """Match a destination name to its <h2> section, tolerating minor
        formatting drift from the model (exact match first, then either
        string containing the other)."""
        target = str(dest_name or "").strip().lower()
        if not target:
            return None
        for i, (header, _body) in enumerate(sections):
            if header.strip().lower() == target:
                return i
        for i, (header, _body) in enumerate(sections):
            h = header.strip().lower()
            if h and (target in h or h in target):
                return i
        return None

    def _group_already_cached(self, kind: str, group: list[dict]) -> bool:
        """True only if EVERY destination in group already has a real
        (non-empty) cached direct-batch result for this kind.

        Without this guard, _prefetch_grouped_direct_batch fires a fresh
        grouped call for every group/kind combination on every invocation --
        harmless if it only ever runs once per URLDiscoverer instance, but
        a real, expensive bug if discover_all runs more than once against
        the same destinations in one process (e.g. main.py's selective
        retry pass re-invoking URL discovery). Found 2026-08-15: a real run
        (dipstick55) captured every single (destination, kind) combo TWICE,
        ~1 hour apart, both from the grouped path -- a full duplicate pass
        that roughly doubled url_discovery's real Grok spend for zero
        benefit, since the second pass's results were near-identical to the
        first's. The existing single-destination getters already skip
        re-fetching a cache hit; this brings the grouped path in line with
        that same behavior instead of bypassing it.
        """
        cache_attr = {
            "attraction": "_attraction_direct_batch_cache",
            "restaurant": "_restaurant_direct_batch_cache",
            "trail": "_alltrails_direct_batch_cache",
        }.get(kind)
        if cache_attr is None:
            return False
        cache = getattr(self, cache_attr, None)
        if not cache:
            return False
        for dest in group:
            dest_name = str(dest.get("name", "") or "").strip()
            dates = str(dest.get("dates", "") or "")
            if not dest_name:
                return False
            key = self._batch_cache_key(dest_name, f"{dates}|html|{kind}")
            cached = cache.get(key)
            if not cached:
                return False
        return True

    def _prefetch_grouped_direct_batch(self, destinations: list[dict]) -> None:
        """Pre-fetch attraction/restaurant/trail direct-batch rows for
        groups of destinations in a single call each, populating the same
        per-kind caches the existing single-destination getters
        (_get_attraction_direct_batch_rows_for_destination etc.) check
        first -- so every existing call site transparently gets a cache hit
        instead of re-fetching, with zero changes needed at those call
        sites. A destination whose group call fails, times out, or gets
        dropped during splitting/matching simply isn't written into the
        cache, so it falls through to the normal single-destination path
        exactly as if grouping didn't exist -- this is purely additive.

        Controlled by self._direct_batch_group_size
        (DEFAULT_DIRECT_BATCH_GROUP_SIZE / url_discovery.direct_batch_group_size
        / DIRECT_BATCH_GROUP_SIZE env var). <=1 disables this entirely.
        """
        group_size = max(1, int(getattr(self, "_direct_batch_group_size", DEFAULT_DIRECT_BATCH_GROUP_SIZE) or 1))
        if group_size <= 1:
            return

        valid_destinations = [
            d for d in destinations if isinstance(d, dict) and str(d.get("name", "") or "").strip()
        ]
        if len(valid_destinations) < 2:
            return

        groups = [
            valid_destinations[i : i + group_size] for i in range(0, len(valid_destinations), group_size)
        ]
        jobs = [
            (kind, group)
            for kind in ("attraction", "restaurant", "trail")
            for group in groups
            if len(group) >= 2 and not self._group_already_cached(kind, group)
        ]
        if not jobs:
            return

        def _run_one(kind: str, group: list[dict]) -> None:
            try:
                self._fetch_and_cache_grouped_direct_batch(kind=kind, group=group)
            except Exception:
                logger.warning(
                    "Grouped direct-batch prefetch failed for kind=%s (%d destinations); "
                    "falling back to per-destination calls for this group",
                    kind,
                    len(group),
                    exc_info=True,
                )

        with instrumented_pool("url_discovery_grouped_prefetch", min(len(jobs), 8)) as pool:
            futs = [pool.submit(_run_one, kind, group) for kind, group in jobs]
            for f in as_completed(futs):
                f.result()

    def _fetch_and_cache_grouped_direct_batch(self, *, kind: str, group: list[dict]) -> None:
        pairs = [(str(d.get("name", "") or ""), str(d.get("dates", "") or "")) for d in group]
        # Manifest seeds (see manifest_parser.py: "Attraction/hike/experience
        # name hints") only apply to attraction/trail harvest -- restaurants
        # have no seed concept, and _direct_batch_html_prompt_multi ignores
        # this map for any other kind regardless.
        seed_names_by_destination = (
            {
                str(d.get("name", "") or ""): [
                    str(seed or "").strip() for seed in (d.get("seeds", []) or []) if str(seed or "").strip()
                ]
                for d in group
            }
            if kind in ("attraction", "trail")
            else None
        )
        prompt_pair = self._direct_batch_html_prompt_multi(
            kind=kind, destinations=pairs, seed_names_by_destination=seed_names_by_destination
        )
        if prompt_pair is None:
            return
        system_prompt, user_prompt = prompt_pair

        search_client = getattr(self, "_search", None)
        if search_client is None:
            return
        if hasattr(search_client, "is_circuit_open") and search_client.is_circuit_open():
            return

        group_dest_names = [str(d.get("name", "") or "").strip() for d in group]
        html = str(
            search_client.chat_completion(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                temperature=0.1,
                response_format=None,
                live_search=True,
                allowed_domains=self._allowed_domains_for_batch_kind(kind),
            )
            or ""
        )
        if not html:
            logger.info(
                "Grouped direct-batch harvest for kind=%s destinations=%s returned an empty "
                "response; falling back to per-destination calls for this group.",
                kind,
                group_dest_names,
            )
            return

        sections = self._split_multi_destination_html(html)
        if not sections:
            # Diagnostic for the split-matching failure mode: the model
            # returned content but _split_multi_destination_html found zero
            # <h2>...</h2> sections in it -- either the model didn't use the
            # requested <h2>Destination Name</h2> heading format at all (e.g.
            # markdown "## Name" or bold text instead of an <h2> tag), or the
            # whole response is something else entirely (an apology, a
            # truncated fragment, etc). Logging a head/tail snippet here
            # (rather than the full body) keeps this readable while still
            # showing the actual heading style the model chose, which is
            # exactly what's needed to tell those cases apart.
            snippet = html[:300].replace("\n", " ")
            logger.info(
                "Grouped direct-batch harvest for kind=%s destinations=%s produced no <h2> "
                "sections (html_length=%d); falling back to per-destination calls for this "
                "group. Response head: %r",
                kind,
                group_dest_names,
                len(html),
                snippet,
            )
            return

        cache_attr = {
            "attraction": "_attraction_direct_batch_cache",
            "restaurant": "_restaurant_direct_batch_cache",
            "trail": "_alltrails_direct_batch_cache",
        }.get(kind)
        if cache_attr is None:
            return
        if not hasattr(self, cache_attr):
            setattr(self, cache_attr, {})
        if not hasattr(self, "_request_cache_lock"):
            self._request_cache_lock = Lock()
        if not hasattr(self, "_direct_batch_html_failure_ts"):
            self._direct_batch_html_failure_ts = {}
        cache = getattr(self, cache_attr)

        min_required = self._direct_batch_min_required(kind)
        remaining = list(sections)
        for dest in group:
            dest_name = str(dest.get("name", "") or "").strip()
            dates = str(dest.get("dates", "") or "")
            if not dest_name:
                continue
            match_idx = self._match_destination_section(dest_name, remaining)
            if match_idx is None:
                # Diagnostic: show what headers WERE found in this response
                # so a heading-format mismatch (e.g. the model dropping the
                # state/park suffix, or emitting a section per subcategory
                # instead of per destination) is visible without re-running
                # anything.
                logger.info(
                    "Grouped direct-batch harvest for kind=%s: no <h2> section matched "
                    "destination '%s' (group=%s). Headers found in response: %s",
                    kind,
                    dest_name,
                    group_dest_names,
                    [header for header, _body in remaining],
                )
                continue
            header, body = remaining.pop(match_idx)
            rows = self._direct_batch_rows_from_html(body)
            filtered_rows = [dict(row) for row in rows if isinstance(row, dict)]
            key = self._batch_cache_key(dest_name, f"{dates}|html|{kind}")
            if not key:
                continue
            self._persist_direct_batch_html_capture(
                destination=dest_name,
                dates=dates,
                kind=kind,
                key=key,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                query="",
                html=body,
                rows=filtered_rows,
                provider=f"{search_client.__class__.__name__}(grouped x{len(group)})",
            )
            if len(filtered_rows) < min_required:
                # Below the same bar _fetch_direct_batch_html_rows enforces --
                # leave this destination's cache unpopulated so it falls
                # through to a normal, individually-retried single-destination
                # fetch rather than locking in a too-thin result.
                logger.info(
                    "Grouped direct-batch harvest for kind=%s: matched section for '%s' "
                    "(group=%s) but only parsed %d row(s), below min_required=%d; falling "
                    "back to a per-destination call for this destination.",
                    kind,
                    dest_name,
                    group_dest_names,
                    len(filtered_rows),
                    min_required,
                )
                continue
            with self._request_cache_lock:
                cache[key] = filtered_rows
                self._direct_batch_html_failure_ts.pop(key, None)
                self._mark_persistent_cache_dirty()

    def _direct_batch_html_cache_hit_or_recent_failure(
        self, cache: dict[str, list[dict[str, Any]]], key: str
    ) -> tuple[bool, list[dict[str, Any]]]:
        """Returns (should_short_circuit, rows). rows is only meaningful when
        should_short_circuit is True (a real cache hit); an empty list with
        should_short_circuit=True means "recent failure, return [] without
        touching the network." Caller must hold _request_cache_lock."""
        cached = cache.get(key)
        if cached is not None and len(cached) > 0:
            return True, [dict(row) for row in cached if isinstance(row, dict)]
        failed_at = self._direct_batch_html_failure_ts.get(key)
        cooldown = float(
            getattr(
                self,
                "_direct_batch_html_failure_cooldown_seconds",
                DEFAULT_DIRECT_BATCH_HTML_FAILURE_COOLDOWN_SECONDS,
            )
        )
        if failed_at is not None and (time.monotonic() - failed_at) < cooldown:
            return True, []
        return False, []

    def _batch_query_fingerprint(self, kind: str) -> str:
        """Short digest of the inputs that change what the batch is ASKED for.

        Only the budget varies per run today, so that is what is hashed. If the
        query text itself gains more variables, hash the rendered query instead
        -- the point is that two different asks must not share a cache entry.
        """
        import hashlib

        # Only the restaurant query varies with the budget today. Attractions
        # and en-route stops take no run-varying input, so their keys keep the
        # original shape and nothing needlessly re-fetches.
        #
        # This is narrow on purpose, and the narrowness is the weakness: a
        # future edit to the ATTRACTION prompt would be just as invisible as
        # the restaurant one was. The durable fix is to fingerprint the
        # rendered query for every kind, which means the query builders need a
        # uniform signature first.
        if str(kind or "").strip().lower() != "restaurant":
            return ""
        # Hash the QUERY, not the budget. Hashing the budget meant rewording the
        # prompt -- naming friteries and imbiss instead of "$ and $$ price
        # levels" -- produced an identical fingerprint and hit the same cached
        # rows, so the sharper ask never ran and Brussels came back unchanged.
        # Exactly the failure this fingerprint was added to prevent, one level
        # further in.
        try:
            # The item count shapes the SYSTEM prompt, which this does not
            # render, so it is folded in explicitly. Raising 8 -> 20 changed
            # nothing on the next run because the key never noticed. Third time
            # a change to the ask has been invisible here, and each time the
            # missing input was one I had not thought of.
            count = getattr(self, "_restaurant_direct_batch_item_count", "")
            rendered = f"{count}|{self._restaurant_direct_batch_query('__fingerprint__', '')}"
        except Exception:
            rendered = str(getattr(self, "_trip_budget", "") or "")
        if not rendered.strip():
            return ""
        return hashlib.sha1(rendered.encode("utf-8")).hexdigest()[:8]

    def _get_direct_batch_html_rows_for_destination(
        self,
        *,
        cache: dict[str, list[dict[str, Any]]],
        destination: str,
        dates: str,
        kind: str,
        origin_name: str = "",
        lodging_location: str = "",
        seed_names: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        if not hasattr(self, "_request_cache_lock"):
            self._request_cache_lock = Lock()
        if not hasattr(self, "_direct_batch_html_key_locks"):
            self._direct_batch_html_key_locks = {}
        if not hasattr(self, "_direct_batch_html_failure_ts"):
            self._direct_batch_html_failure_ts = {}
        # The QUERY must be part of the key. It was not, so changing the batch
        # prompt to ask for inexpensive restaurants changed nothing: Berlin and
        # Frankfurt were served the previous fine-dining rows straight from
        # .cache/url_discovery and the fix looked like it had failed. Any prompt
        # change was invisible until someone cleared the cache by hand, which
        # is a poor property for a component whose prompt is still being tuned.
        fingerprint = self._batch_query_fingerprint(kind)
        suffix = f"|{fingerprint}" if fingerprint else ""
        key = self._batch_cache_key(destination, f"{dates}|html|{kind}{suffix}")
        if not key:
            return []

        with self._request_cache_lock:
            hit, rows_or_empty = self._direct_batch_html_cache_hit_or_recent_failure(cache, key)
            # Counted once per logical lookup, here and not at the post-lock
            # re-check, which would count a coalesced caller twice. `hit` is also
            # true for a recent-failure cooldown, which served no harvest at all
            # -- `key in cache` separates the two, and a cooldown counts cold,
            # under-reporting warmth rather than over-reporting it.
            self._note_harvest_lookup(cache, key, served_from_cache=bool(hit) and key in cache)
            if hit:
                return rows_or_empty
            key_lock = self._direct_batch_html_key_locks.setdefault(key, Lock())

        # Coalesce concurrent callers for the same (destination, kind, dates):
        # only the thread that wins this lock actually hits the network: every
        # other caller waiting on it re-checks the cache/cooldown state below
        # and reuses that outcome instead of independently re-triggering the
        # same expensive multi-attempt harvest call.
        with key_lock:
            with self._request_cache_lock:
                hit, rows_or_empty = self._direct_batch_html_cache_hit_or_recent_failure(cache, key)
                if hit:
                    return rows_or_empty

            filtered_rows = self._fetch_direct_batch_html_rows(
                key=key,
                destination=destination,
                dates=dates,
                kind=kind,
                origin_name=origin_name,
                lodging_location=lodging_location,
                seed_names=seed_names,
            )
            with self._request_cache_lock:
                if filtered_rows:
                    cache[key] = filtered_rows
                    self._direct_batch_html_failure_ts.pop(key, None)
                    # Found 2026-08-15: this write updates the in-memory cache
                    # but, without this call, never marks the persistent
                    # cache dirty -- _save_persistent_caches' harvest-cache
                    # section (see _load_persistent_caches' matching read
                    # side, already wired up and enabled by default) never
                    # actually had anything to save, so a genuinely
                    # successful harvest was silently lost the moment the
                    # process exited, for both the single-destination path
                    # here and the grouped path (_fetch_and_cache_grouped_direct_batch).
                    # Real cost: a same-run retry pass constructing a fresh
                    # URLDiscoverer had no persisted cache to load from and
                    # had to refetch everything from scratch (dipstick55:
                    # every direct-batch call repeated in full, ~doubling
                    # that run's real Grok spend for no benefit).
                    self._mark_persistent_cache_dirty()
                else:
                    if key in cache:
                        # Empty direct-batch HTML captures are not authoritative
                        # and can poison later retries for a destination; allow a
                        # new fetch attempt once the failure cooldown expires
                        # rather than freezing the result set at [] forever.
                        del cache[key]
                    self._direct_batch_html_failure_ts[key] = time.monotonic()
            return filtered_rows

    def _fetch_direct_batch_html_rows(
        self,
        *,
        key: str,
        destination: str,
        dates: str,
        kind: str,
        origin_name: str = "",
        lodging_location: str = "",
        seed_names: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        prompt_pair = self._direct_batch_html_prompt(
            kind=kind,
            dest_name=destination,
            dates=dates,
            origin_name=origin_name,
            lodging_location=lodging_location,
            seed_names=seed_names,
        )
        if prompt_pair is None:
            return []
        system_prompt, user_prompt = prompt_pair

        html = ""
        provider_used = ""
        primary = getattr(self, "_search", None)
        if primary is not None:
            html = str(
                primary.chat_completion(
                    system_prompt=system_prompt,
                    user_prompt=user_prompt,
                    temperature=0.1,
                    response_format=None,
                    # Real search (2026-08-14 fix): the old live_search
                    # mechanism this flag used to gate was silently
                    # deprecated by xAI -- every harvest call in production
                    # was running on the model's training-data memory, not
                    # live search, regardless of this flag's value. Probe
                    # evidence: with search genuinely enabled, 21/21 embedded
                    # URLs matched Grok's own citations across 4 real test
                    # cases (582 citations); the previous behavior produced
                    # zero citations and no way to verify provenance at all.
                    live_search=True,
                    allowed_domains=self._allowed_domains_for_batch_kind(kind),
                )
                or ""
            )
            if html:
                provider_used = primary.__class__.__name__

        rows = self._direct_batch_rows_from_html(html)
        min_required = self._direct_batch_min_required(kind)
        circuit_open = bool(
            primary is not None
            and hasattr(primary, "is_circuit_open")
            and primary.is_circuit_open()
        )
        if (
            primary is not None
            and min_required > 0
            and len(rows) < min_required
            and not circuit_open
        ):
            # Firing a second expensive harvest call is the worst possible
            # moment to do it while the circuit breaker is open -- that state
            # means a recent burst of transient errors, and this retry-prompt
            # would just compound it. Accept the short first-pass batch
            # instead and let a later cache-refresh pick up more rows once
            # the provider recovers.
            retry_prompt = (
                f"{user_prompt} Return at least {min_required} valid, distinct <li> items for {destination}. "
                "Do not use generic listing pages, placeholders, or duplicate entity names."
            )
            retry_html = str(
                primary.chat_completion(
                    system_prompt=system_prompt,
                    user_prompt=retry_prompt,
                    temperature=0.1,
                    response_format=None,
                    # Real search (2026-08-14 fix): the old live_search
                    # mechanism this flag used to gate was silently
                    # deprecated by xAI -- every harvest call in production
                    # was running on the model's training-data memory, not
                    # live search, regardless of this flag's value. Probe
                    # evidence: with search genuinely enabled, 21/21 embedded
                    # URLs matched Grok's own citations across 4 real test
                    # cases (582 citations); the previous behavior produced
                    # zero citations and no way to verify provenance at all.
                    live_search=True,
                    allowed_domains=self._allowed_domains_for_batch_kind(kind),
                )
                or ""
            )
            retry_rows = self._direct_batch_rows_from_html(retry_html)
            if len(retry_rows) > len(rows):
                html = retry_html
                rows = retry_rows
                provider_used = primary.__class__.__name__

        # Cross-provider batch retry (2026-08-15 finding): when the primary's
        # batch harvest still hasn't produced enough rows -- including
        # entirely empty, e.g. during a primary-provider outage -- retry the
        # SAME purpose-built batch list prompt through the fallback client
        # before ever dropping to the narrower single-query mode
        # (_get_direct_batch_rows_for_destination). A live run found the
        # fallback's single generic .search() query structurally can't match
        # every specific named item a batch prompt covers (it wasn't built
        # to -- it predates this class having its own working batch
        # capability at all), so items were rendering with no URL despite
        # the fallback client itself being perfectly healthy. Retrying with
        # the fallback's own chat_completion(live_search=True) gives it a
        # fair shot at the same item-specific coverage the primary gets,
        # since both providers share that same batch-capable interface.
        # One attempt only (no retry-prompt escalation on the fallback) --
        # this is already a second full harvest call; a third would be
        # excessive under conditions that are already degraded.
        fallback = getattr(self, "_search_fallback", None)
        if (
            fallback is not None
            and fallback is not primary
            and min_required > 0
            and len(rows) < min_required
            and not (hasattr(fallback, "is_circuit_open") and fallback.is_circuit_open())
        ):
            fallback_html = str(
                fallback.chat_completion(
                    system_prompt=system_prompt,
                    user_prompt=user_prompt,
                    temperature=0.1,
                    response_format=None,
                    live_search=True,
                )
                or ""
            )
            fallback_rows = self._direct_batch_rows_from_html(fallback_html)
            if len(fallback_rows) > len(rows):
                html = fallback_html
                rows = fallback_rows
                provider_used = fallback.__class__.__name__

        query_text = self._direct_batch_html_prompt(
            kind=kind,
            dest_name=destination,
            dates=dates,
            origin_name=origin_name,
            lodging_location=lodging_location,
        )
        if query_text is not None:
            _system_prompt, _user_prompt = query_text
            effective_query = _user_prompt
        else:
            effective_query = ""

        filtered_rows = [dict(row) for row in rows if isinstance(row, dict)]
        self._persist_direct_batch_html_capture(
            destination=destination,
            dates=dates,
            kind=kind,
            key=key,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            query=effective_query,
            html=html,
            rows=filtered_rows,
            provider=provider_used,
        )
        return filtered_rows

    def _direct_batch_min_required(self, kind: str) -> int:
        if kind in {"trail", "attraction"}:
            return 1
        if kind == "restaurant":
            return max(1, int(getattr(self, "_restaurant_direct_batch_min_results", DEFAULT_RESTAURANT_DIRECT_BATCH_MIN_RESULTS) or DEFAULT_RESTAURANT_DIRECT_BATCH_MIN_RESULTS))
        if kind == "en_route_stop":
            return max(1, int(getattr(self, "_en_route_direct_batch_min_results", DEFAULT_EN_ROUTE_DIRECT_BATCH_MIN_RESULTS) or DEFAULT_EN_ROUTE_DIRECT_BATCH_MIN_RESULTS))
        return 0

    @staticmethod
    def _sanitize_direct_batch_description_text(text: str) -> str:
        cleaned = str(text or "").strip()
        if not cleaned:
            return ""
        cleaned = re.sub(r"https?://\S+", "", cleaned)
        cleaned = re.sub(r"\bLinks?\s*:.*$", "", cleaned, flags=re.IGNORECASE)
        # "Source"/"Maps"/"AllTrails" are anchor-text artifacts from the harvest
        # format (<a>Source</a> <a>Maps</a> or <a>AllTrails</a>), not organic
        # description content. They can appear anywhere in the text -- not just
        # trailing -- when rating/price/cuisine follow the links (e.g.
        # "Name - Source Maps 4.6/5 $$ Cuisine").
        cleaned = re.sub(r"\b(?:Source|Maps?|AllTrails)\b", "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"\s+[|/]+\s+", " ", cleaned)
        cleaned = re.sub(r"\s+", " ", cleaned).strip(" -:|,;")
        return cleaned

    # Restaurant-row filler vocabulary: cuisine, meal-type, and price-tier
    # words that show up in harvested rows with no real prose at all (e.g.
    # "(high volume), $$ American." or "(French/Southwestern fine dining)")
    # -- these must not count as "real words" when judging whether anything
    # substantive remains, since they're just a restatement of the rating/
    # price/cuisine fields that are already extracted into their own columns.
    _METADATA_ONLY_FILLER_WORDS = frozenset({
        "volume", "high", "low", "moderate",
        "mexican", "italian", "american", "bbq", "barbecue", "steakhouse",
        "thai", "indian", "japanese", "sushi", "chinese", "mediterranean",
        "greek", "french", "seafood", "vietnamese", "korean", "pizza",
        "burger", "bistro", "cafe", "diner", "grill", "brewpub", "bakery",
        "coffee", "cuisine", "food", "dining", "fine", "fast", "casual",
        "contemporary", "southwestern", "breakfast", "lunch", "dinner",
        "eatery", "kitchen", "style", "upscale",
    })

    @classmethod
    def _is_metadata_only_residual_text(cls, text: str, name: str = "") -> bool:
        """True when text carries only the item's own name plus rating/price/
        cuisine tokens and no real prose -- i.e. nothing worth surfacing as a
        description/teaser. Some harvested rows have no separator between the
        name and its trailing metadata, so the name itself must be discounted
        before judging whether anything substantive remains."""
        cleaned = str(text or "").strip()
        if not cleaned:
            return True
        name_clean = str(name or "").strip()
        if name_clean:
            cleaned = re.sub(re.escape(name_clean), "", cleaned, count=1, flags=re.IGNORECASE).strip()
            if not cleaned:
                return True
        residual = re.sub(r"\d+(?:\.\d+)?\s*(?:/\s*5|out\s+of\s+5|stars?)", "", cleaned, flags=re.IGNORECASE)
        residual = re.sub(r"[\$#]{1,4}", "", residual)
        # Review-volume qualifiers ("(high volume)", "(very high volume)") are
        # pure metadata carried over from the harvested rating line, not prose.
        residual = re.sub(r"\(?\s*(?:very\s+)?(?:high|low|moderate)\s+volume\s*\)?", "", residual, flags=re.IGNORECASE)
        # Compound cuisine labels ("Thai/Japanese", "Mexican-American") are still
        # metadata, not prose -- collapse slash/hyphen/ampersand-joined alpha runs
        # to a placeholder that doesn't count as a word before counting. Only
        # collapse when every joined word is itself known filler/cuisine
        # vocabulary -- a real compound descriptor like "Chef-driven" (as in
        # "Chef-driven Southwestern plates.") must survive as two real words,
        # not get eaten by the same rule that erases "Thai/Japanese".
        def _collapse_compound_if_all_filler(match: "re.Match[str]") -> str:
            words = [w for w in re.split(r"[/\-&]", match.group(0)) if w.strip()]
            if words and all(w.strip().lower() in cls._METADATA_ONLY_FILLER_WORDS for w in words):
                return "x"
            return match.group(0)

        residual = re.sub(
            r"\b[A-Za-z]+(?:\s*[/\-&]\s*[A-Za-z]+)+\b", _collapse_compound_if_all_filler, residual
        )
        residual_words = re.findall(r"[A-Za-z]{3,}", residual)
        # A word list of pure cuisine/meal-type/price-tier filler doesn't count
        # as real content either -- e.g. "(Contemporary American)" or "American
        # fast food/pizza." are just restatements of rating/price/cuisine
        # metadata dressed up as a sentence fragment, not an actual descriptive
        # note about the place.
        substantive_words = [w for w in residual_words if w.lower() not in cls._METADATA_ONLY_FILLER_WORDS]
        return len(substantive_words) <= 1

    @staticmethod
    def _infer_direct_batch_quality_metadata(text: str, url: str) -> dict[str, Any]:
        blob = str(text or "")
        lower_url = (url or "").lower()
        out: dict[str, Any] = {}

        rating_match = re.search(r"(\d+(?:\.\d+)?)\s*(?:/\s*5|out\s+of\s+5|stars?)", blob, flags=re.IGNORECASE)
        if rating_match:
            out["rating"] = float(rating_match.group(1))
            out["raw_rating"] = str(rating_match.group(0)).strip()
        else:
            rating_float = None
            for pattern in (r"\b(4\.[0-9]|5\.0)\b", r"\b(4\.[0-9]|5\.0)\s*/\s*5\b"):
                match = re.search(pattern, blob, flags=re.IGNORECASE)
                if match:
                    rating_float = float(match.group(1))
                    out["rating"] = rating_float
                    out["raw_rating"] = str(match.group(0)).strip()
                    break

        votes_match = re.search(r"(\d{1,3}(?:,\d{3})*|\d+)\s*(?:reviews?|ratings?|votes?)", blob, flags=re.IGNORECASE)
        if votes_match:
            out["votes"] = int(votes_match.group(1).replace(",", ""))

        if "tripadvisor" in lower_url:
            out["source_type"] = "tripadvisor"
        elif "yelp" in lower_url:
            out["source_type"] = "yelp"
        elif "google.com/maps" in lower_url:
            out["source_type"] = "google_maps"
        else:
            out["source_type"] = "official"

        return out

    @staticmethod
    def _infer_restaurant_metadata_from_text_and_url(text: str, url: str) -> dict[str, Any]:
        blob = str(text or "")
        lowered = blob.lower()
        out: dict[str, Any] = {}

        # Trailing boundary must also accept a comma: the common harvest
        # format is "Name - 4.7/5, $$$, Cuisine" (comma-delimited metadata,
        # no space between the price run and the following comma), which
        # the previous whitespace-or-end-of-string-only boundary silently
        # failed to match -- confirmed against real dipstick61 output
        # ("Wood Ash Rye - 4.7/5, $$$, New American", "Cliffside Restaurant
        # - 4.4/5, $$$, Upscale American") where price_range never got set
        # despite the source clearly stating it.
        price_match = re.search(r"(?:^|\s)(\${1,4})(?:\s|,|$)", blob)
        if price_match:
            out["price_range"] = price_match.group(1)

        cuisine_keywords = {
            "mexican": "Mexican",
            "italian": "Italian",
            "american": "American",
            "bbq": "BBQ",
            "barbecue": "BBQ",
            "steakhouse": "Steakhouse",
            "thai": "Thai",
            "indian": "Indian",
            "japanese": "Japanese",
            "sushi": "Japanese",
            "chinese": "Chinese",
            "mediterranean": "Mediterranean",
            "greek": "Greek",
            "french": "French",
            "seafood": "Seafood",
            "vietnamese": "Vietnamese",
            "korean": "Korean",
            "pizza": "Pizza",
            "burger": "American",
            "bistro": "Bistro",
            "cafe": "Cafe",
            "café": "Cafe",
            "poke": "Hawaiian",
            "hawaiian": "Hawaiian",
            "pies": "Bakery",
            "bakery": "Bakery",
        }
        for key, label in cuisine_keywords.items():
            if re.search(rf"\b{re.escape(key)}\b", lowered):
                out["cuisine"] = label
                break

        reserve_signal = bool(re.search(r"\b(reservations?|book ahead|popular|waitlist)\b", lowered))
        if reserve_signal:
            out["reserve_recommended"] = True

        # Last resort for maps links: parse query text and attempt the same
        # cuisine extraction when list text is sparse.
        if "cuisine" not in out:
            parsed = urlparse(str(url or "").strip())
            query = parse_qs(parsed.query or "")
            q_text = " ".join(
                v for key in ("query", "q", "name") for v in query.get(key, [])
            ).lower()
            for key, label in cuisine_keywords.items():
                if re.search(rf"\b{re.escape(key)}\b", q_text):
                    out["cuisine"] = label
                    break

        return out

    # A leading "- " / "* " / "• " marker at the start of a line, i.e. a
    # Markdown bullet rather than an HTML <li>.
    _MARKDOWN_BULLET_LINE_RE = re.compile(r"^\s*[-*•]\s+(\S.*)$")
    _HREF_ATTR_RE = re.compile(r"""href\s*=\s*["']([^"']+)["']""", re.IGNORECASE)

    @classmethod
    def _direct_batch_records_from_markdown_bullets(cls, html_text: str) -> list[dict[str, Any]]:
        """Fallback record extraction for direct-batch harvest replies that come
        back as a Markdown "- Name - detail <a href=...>Source</a>" bullet list
        instead of the requested <ul><li> markup.

        Real evidence (dipstick63, Moab -> Canyonlands en-route-stop harvest):
        Grok returned 8 genuine, well-formed candidates -- including "Dead
        Horse Point State Park Overlook", the obvious real stop on that leg --
        as a Markdown bullet list. _DirectBatchHTMLListParser only recognizes
        <li> elements, so it silently produced zero records and every one of
        those real candidates was discarded before reaching en_route_stops,
        even though the exact same prompt against the exact same leg returned
        proper <li> markup (and 8 parsed rows) on other runs. This is a
        one-off provider formatting slip, not a discovery/verification
        failure or a duplicate -- so recovering it here, rather than treating
        the empty parse as final, is the correct fix.
        """
        records: list[dict[str, Any]] = []
        for line in str(html_text or "").splitlines():
            match = cls._MARKDOWN_BULLET_LINE_RE.match(line)
            if not match:
                continue
            content = match.group(1)
            urls = [html_lib.unescape(href.strip()) for href in cls._HREF_ATTR_RE.findall(content) if href.strip()]
            raw_text = re.sub(r"<[^>]+>", " ", content)
            raw_text = html_lib.unescape(raw_text)
            raw_text = re.sub(r"\s+", " ", raw_text).strip()
            if not raw_text and not urls:
                continue
            records.append({"name": "", "urls": urls, "raw_text": raw_text})
        return records

    @classmethod
    def _direct_batch_rows_from_html(cls, html_text: str) -> list[dict[str, Any]]:
        parser = _DirectBatchHTMLListParser()
        try:
            parser.feed(str(html_text or ""))
        except Exception:
            return []

        records = parser.records
        if not records:
            records = cls._direct_batch_records_from_markdown_bullets(str(html_text or ""))

        rows: list[dict[str, Any]] = []
        for record in records:
            raw_text = str(record.get("raw_text", "") or "").strip()
            # Strip anchor-label artifacts (Source/Maps/AllTrails) from anywhere
            # in the text, not just the tail end. A trailing-only strip was
            # sufficient while no prompt put real content after these labels,
            # but once a kind's format sandwiches the links between the name
            # and a rating/dash-separated note (e.g. "Name <a>Source</a>
            # <a>Maps</a> 4.8/5 - short note", used by the attraction/trail
            # prompts once they started asking for a descriptive note), a
            # trailing-only strip left "Source"/"Maps" glued into the name
            # extracted below.
            semantic_text = re.sub(r"\b(?:Source|Maps?|AllTrails)\b", "", raw_text, flags=re.IGNORECASE)
            semantic_text = re.sub(r"\s{2,}", " ", semantic_text).strip(" -:|,;")
            name = str(record.get("name", "") or semantic_text).strip()
            # Strip cuisine or type annotations Grok appends in parentheses, e.g. "Cafe X (Mexican)"
            name = re.sub(r"\s*\([^)]{1,40}\)\s*$", "", name).strip()
            urls = [
                cls._normalize_direct_batch_authoritative_url(url)
                for url in list(record.get("urls", []))
                if cls._normalize_direct_batch_authoritative_url(url)
            ]
            if not name and not urls:
                continue

            maps_urls = [url for url in urls if cls._is_google_maps_candidate_url(url)]
            non_maps_urls = [url for url in urls if not cls._is_google_maps_candidate_url(url)]
            # Prefer a direct Source link; keep Maps URL as fallback metadata only.
            preferred = non_maps_urls[0] if non_maps_urls else (maps_urls[0] if maps_urls else "")
            maps_fallback = maps_urls[0] if maps_urls else ""
            display_name = name
            detail_text = semantic_text
            parts = [part.strip() for part in re.split(r"\s+[\-\u2013\u2014]\s+", semantic_text) if part.strip()]
            if len(parts) > 1:
                display_name = parts[0]
                detail_text = " - ".join(parts[1:])
            # A rating glued directly onto the name with no dash separator
            # (e.g. "Sunset Point 4.8/5 - Iconic canyon overlook...", where the
            # dash only separates the rating from the trailing note) is never
            # part of the real name -- truncate there rather than trust the
            # dash-split alone to have found the true boundary.
            rating_leak = re.search(
                r"\d+(?:\.\d+)?\s*(?:/\s*5\b|out\s+of\s+5\b|stars?\b)", display_name, flags=re.IGNORECASE
            )
            if rating_leak and rating_leak.start() > 0:
                display_name = display_name[: rating_leak.start()].strip(" -:|,;")
            # Real Grok output for a "rating, then a short note" instruction
            # doesn't reliably use a dash separator (observed: "Name 4.4/5
            # Interactive exhibits and award-winning film..." with just a
            # space) -- the dash-split above then finds nothing and
            # detail_text is left as the whole name+rating+note blob. Locate
            # the actual boundary directly: name, then an optional rating,
            # then an optional trailing distance-in-miles (trail rows), and
            # treat everything remaining after whichever of those was found
            # last as the real detail/note text. This also correctly handles
            # the dash-separated shape (the separator is just leading
            # punctuation stripped off the front) and the has-no-rating
            # "Name - detail" shape (cursor stays right after the name).
            cursor = 0
            name_match = re.search(re.escape(display_name), semantic_text, flags=re.IGNORECASE) if display_name else None
            if name_match:
                cursor = name_match.end()
            trailing_rating_match = re.search(
                r"\d+(?:\.\d+)?\s*(?:/\s*5\b|out\s+of\s+5\b|stars?\b)", semantic_text[cursor:], flags=re.IGNORECASE
            )
            if trailing_rating_match:
                cursor += trailing_rating_match.end()
                distance_match = re.match(r"\s*\d+(?:\.\d+)?\s*mi\b\.?", semantic_text[cursor:], flags=re.IGNORECASE)
                if distance_match:
                    cursor += distance_match.end()
            cursor_detail_text = semantic_text[cursor:].strip(" -:|,;")
            if cursor_detail_text and len(cursor_detail_text) < len(detail_text):
                detail_text = cursor_detail_text
            snippet_parts = [raw_text or display_name] if (raw_text or display_name) else []
            if urls:
                snippet_parts.append("Links: " + " ".join(urls))
            description_text = str(detail_text or semantic_text or display_name or "").strip()
            practical_note = ""
            detour_miles = cls._extract_en_route_detour_miles_from_text(description_text)
            detour_minutes = cls._extract_en_route_detour_minutes_from_text(description_text)
            description_text = cls._sanitize_direct_batch_description_text(description_text)
            if detour_miles is not None:
                description_text = re.sub(r"\bdetour\b[^,.;&]*\b(?:mile|miles|mi)\b[^,.;&]*", "", description_text, flags=re.IGNORECASE).strip(" -:|,")
            if detour_minutes is not None:
                description_text = re.sub(r"\bdetour\b[^,.;&]*\b(?:minute|minutes|min)\b[^,.;&]*", "", description_text, flags=re.IGNORECASE).strip(" -:|,")
            if cls._is_metadata_only_residual_text(description_text, name=name):
                description_text = ""
            if description_text and description_text.lower() != name.lower():
                practical_note = description_text
            quality_meta = cls._infer_direct_batch_quality_metadata(
                " ".join(part for part in [semantic_text, description_text] if part),
                preferred,
            )
            restaurant_meta = cls._infer_restaurant_metadata_from_text_and_url(
                " ".join(part for part in [semantic_text, description_text] if part),
                preferred,
            )
            rows.append(
                {
                    "title": display_name,
                    "name": display_name,
                    "url": preferred,
                    "maps_url": maps_fallback,
                    "snippet": " ".join(part for part in snippet_parts if part),
                    "description": practical_note,
                    "practical_note": practical_note,
                    "detour_distance_miles": detour_miles,
                    "detour_time_minutes": detour_minutes,
                    "cuisine": restaurant_meta.get("cuisine", ""),
                    "price_range": restaurant_meta.get("price_range", ""),
                    "reserve_recommended": bool(restaurant_meta.get("reserve_recommended", False)),
                    "rating": quality_meta.get("rating"),
                    "raw_rating": quality_meta.get("raw_rating"),
                    "votes": quality_meta.get("votes"),
                    "source_type": quality_meta.get("source_type", "official"),
                }
            )
        return rows

    def _backfill_attractions_from_trail_batch(
        self,
        *,
        dest: dict[str, Any],
        ai: dict[str, Any],
        dest_name: str,
        dest_dates: str,
        eligible: list[dict[str, Any]],
        original_count: int,
    ) -> int:
        """Refill attraction slots emptied by URL removal, using trails already bought.

        The trail direct batch harvests far more than the itinerary consumes.
        Measured on the 2026-08-21 cold-start run: 84 rows across 10
        destinations, every one carrying an AllTrails URL, and only 24
        published -- 60 harvested and discarded (71%). Bryce alone harvested
        10 and published 2.

        Meanwhile the same run removed 23 attractions for having no verified
        URL. Those two facts are the same problem seen from both ends: slots
        were emptied for want of a link while verified, trail-specific links
        sat unused in a batch we had already paid for.

        Bounded deliberately by `original_count` -- the number of attractions
        ai_content produced for this destination BEFORE removals. This
        backfills emptied slots and never exceeds what content generation
        already decided was right, so `attractions_per_day * day_count` is
        honoured without this function needing to know either value.

        Promoted trails are NOT treated as seeds. They face the full
        publish-confidence and post-search filters, so an unvetted trail
        cannot enter the itinerary through this path.
        """
        headroom = original_count - len(eligible)
        if headroom <= 0:
            return 0
        cache = getattr(self, "_alltrails_direct_batch_cache", None)
        if not isinstance(cache, dict) or not cache:
            return 0
        rows = cache.get(self._batch_cache_key(dest_name, f"{dest_dates}|html|trail")) or []
        if not rows:
            return 0

        def _key(value: str) -> str:
            return re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).strip()

        # Never duplicate something the itinerary already carries, in any
        # section -- an entity promoted here would otherwise appear twice.
        taken_names = {_key(a.get("name", "")) for a in eligible}
        taken_names |= {
            _key(s.get("name", ""))
            for s in (ai.get("getting_here", {}) or {}).get("en_route_stops", []) or []
        }
        taken_names |= {_key(d.get("title", "")) for d in (dest.get("scenic_drives", []) or [])}
        taken_urls = {str(a.get("url", "") or "").strip() for a in eligible}

        added = 0
        for row in rows:
            if added >= headroom:
                break
            if not isinstance(row, dict):
                continue
            name = str(row.get("name") or row.get("title") or "").strip()
            # The batch has been observed emitting literal markdown in names
            # ("**Balanced Rock Loop**"); those asterisks would otherwise
            # become part of the rendered name and of every match key.
            name = name.replace("*", "").strip()
            if not name or _key(name) in taken_names:
                continue
            url = self._normalize_direct_batch_authoritative_url(row.get("url", ""))
            if not url or url in taken_urls or not self._is_alltrails_trail_url(url):
                continue
            if self._candidate_mentions_conflicting_destination(row, dest_name, item_name=name):
                continue
            cleaned = self._retain_discovered_url(
                url,
                name,
                dest_name,
                allow_alltrails=True,
                kind="attraction",
                is_seed=False,
                candidate=row,
            )
            if not cleaned:
                continue
            promoted = {
                "name": name,
                "type": "trail",
                "url": cleaned,
                "description": str(row.get("description", "") or ""),
                "practical_note": str(row.get("practical_note", "") or ""),
                "is_seed": False,
                "promoted_from_trail_batch": True,
            }
            if row.get("rating") is not None:
                promoted["rating"] = row.get("rating")
            if str(row.get("maps_url", "") or "").strip():
                promoted["maps_url"] = str(row.get("maps_url", "")).strip()
            eligible.append(promoted)
            taken_names.add(_key(name))
            taken_urls.add(cleaned)
            added += 1
            self._log_decision(
                kind="attraction",
                dest_name=dest_name,
                item_name=name,
                reason="promoted_from_trail_batch",
                message=f"backfilled an emptied attraction slot with an already-harvested trail: {cleaned}",
                url=cleaned,
            )
        return added

    # Batch `kind` values are not link_types keys: the trail batch is kind
    # "trail" while its taxonomy entry is link_types.hike. Only categories
    # with a single authoritative source belong here -- attractions,
    # restaurants and en-route stops are heterogeneous by nature, and
    # link_types.scenic_drive sets discovery_site_filter: null explicitly.
    _BATCH_KIND_TO_LINK_TYPE: dict[str, str] = {"trail": "hike"}

    @staticmethod
    def _read_link_type_site_filters(config_path: str) -> dict[str, str]:
        """link_types.<key>.discovery_site_filter, for keys that declare one.

        This config existed since the taxonomy was written and was never read
        by any module -- the site filter was applied only after results came
        back, so we paid to search the whole web and then discarded whatever
        was off-domain. Read defensively: a bad config must not fail a run.
        """
        try:
            import yaml

            with open(config_path, "r", encoding="utf-8") as fh:
                cfg = yaml.safe_load(fh) or {}
            out: dict[str, str] = {}
            for key, entry in (cfg.get("link_types") or {}).items():
                if not isinstance(entry, dict):
                    continue
                site = str(entry.get("discovery_site_filter") or "").strip()
                if site:
                    out[str(key)] = site
            return out
        except Exception:
            return {}

    def _allowed_domains_for_batch_kind(self, kind: str) -> list[str]:
        """Domains to constrain the server-side search to, or [] for unconstrained."""
        link_type = self._BATCH_KIND_TO_LINK_TYPE.get(str(kind or "").strip().lower())
        if not link_type:
            return []
        site = (getattr(self, "_link_type_site_filters", None) or {}).get(link_type, "")
        return [site] if site else []

    @staticmethod
    def _read_search_model_override(config_path: str) -> str:
        """`url_discovery.search_model` from config, or "" when unset.

        Deliberately read defensively and never raised from: a malformed or
        missing config must leave discovery on the content model rather than
        failing the run over a cost optimisation.
        """
        try:
            import yaml

            with open(config_path, "r", encoding="utf-8") as fh:
                cfg = yaml.safe_load(fh) or {}
            return str(((cfg.get("url_discovery") or {}).get("search_model") or "")).strip()
        except Exception:
            return ""

    def _cached_alltrails_batch_url_for_item(self, dest_name: str, dates: str, item_name: str) -> str:
        """An AllTrails URL the trail direct batch ALREADY harvested for this item.

        Reads the trail batch cache only -- never triggers a fetch -- so this
        can never add a paid search call. A destination whose trail batch has
        not run yet simply yields nothing.

        Why this exists: each category reads its own direct batch
        (`_search_attraction_from_direct_batch`, `_search_en_route_stop_...`,
        `_search_alltrails_for_trail_...`), so an item never sees a URL a
        different category already bought for it. Measured on the 2026-08-21
        cold-start run, 11 items appeared in both the trail batch and another
        category's batch, and 4 of them published the weaker link -- two of
        those being the bare park homepage `nps.gov/brca/` where the trail
        batch held a trail-specific AllTrails URL.

        Canyon Overlook Trail is the worked example: harvested from Zion's
        trail batch with its AllTrails URL, then classified as an en-route
        stop and published with a generic nps.gov page instead.
        """
        cache = getattr(self, "_alltrails_direct_batch_cache", None)
        if not isinstance(cache, dict) or not cache:
            return ""
        rows = cache.get(self._batch_cache_key(dest_name, f"{dates}|html|trail")) or []
        for row in rows:
            if not isinstance(row, dict):
                continue
            if self._candidate_mentions_conflicting_destination(row, dest_name, item_name=item_name):
                continue
            url = self._normalize_direct_batch_authoritative_url(row.get("url", ""))
            if not url or not self._is_alltrails_trail_url(url):
                continue
            if self._direct_batch_url_matches_item(url, item_name, dest_name):
                return url
        return ""

    def _search_alltrails_for_trail_from_direct_batch(self, item_name: str, dest_name: str, dates: str = "") -> str | None:
        rows = self._get_alltrails_direct_batch_rows_for_destination(dest_name, dates)
        if not rows:
            self._log_decision(
                kind="attraction",
                dest_name=dest_name,
                item_name=item_name,
                reason="direct_batch_empty",
                message="alltrails direct-link batch empty",
            )
            return None

        if self._direct_batch_is_authoritative():
            matching_urls: list[str] = []
            for row in rows:
                if self._candidate_mentions_conflicting_destination(row, dest_name, item_name=item_name):
                    continue
                structured_url = self._normalize_direct_batch_authoritative_url(row.get("url", ""))
                for raw_url in self._direct_batch_row_url_candidates(row):
                    if raw_url != structured_url and not self._direct_batch_url_matches_item(raw_url, item_name, dest_name):
                        # Preserve raw capture URLs even when the row title is generic,
                        # provided the URL itself is item-relevant and survives the
                        # normal acceptance checks below.
                        if not self._is_alltrails_trail_url(raw_url):
                            continue
                    if not self._is_alltrails_trail_url(raw_url):
                        continue
                    cleaned = self._retain_discovered_url(
                        raw_url,
                        item_name,
                        dest_name,
                        allow_alltrails=True,
                        kind="attraction",
                        candidate=row,
                        allow_shallow_relevance=True,
                    )
                    if cleaned:
                        matching_urls.append(cleaned)

            selected = matching_urls[0] if matching_urls else ""
            if selected:
                self._remember_direct_batch_authoritative_url(selected, item_name)
                self._log_decision(
                    kind="attraction",
                    dest_name=dest_name,
                    item_name=item_name,
                    reason="direct_batch_selected_authoritative",
                    message="alltrails direct-link batch authoritative candidate selected",
                    url=selected,
                )
                return selected
            self._log_decision(
                kind="attraction",
                dest_name=dest_name,
                item_name=item_name,
                reason="direct_batch_no_match",
                message="alltrails direct-link batch had no item-matching trail",
            )
            return None

        best: tuple[int, float, int, str] | None = None
        max_miles = float(getattr(self, "_max_trail_miles", DEFAULT_MAX_TRAIL_MILES) or DEFAULT_MAX_TRAIL_MILES)
        for row in rows:
            if self._candidate_mentions_conflicting_destination(row, dest_name):
                continue
            raw_url = str(row.get("url", "") or "").strip()
            if not self._is_alltrails_trail_url(raw_url):
                continue
            normalized = self._strip_alltrails_tracking(raw_url)
            if self._alltrails_slug_has_numbered_suffix(normalized):
                continue
            if not self._alltrails_slug_matches_item(normalized, item_name):
                continue
            if self._has_alltrails_closure_marker(self._candidate_text_blob(row)):
                continue

            meta = self._extract_alltrails_candidate_metadata(row)
            miles = meta.get("miles")
            if miles is not None and max_miles > 0 and float(miles) > max_miles + 0.15:
                continue

            scored = dict(row)
            scored["url"] = normalized
            score = self._score_candidate_result(
                scored,
                item_name,
                dest_name,
                specific=True,
                site_filter="alltrails.com",
            )
            rating = float(meta.get("rating") or 0.0)
            reviews = int(meta.get("reviews") or 0)
            rank = (score, rating, reviews, normalized)
            if best is None or rank > best:
                best = rank

        if not best:
            self._log_decision(
                kind="attraction",
                dest_name=dest_name,
                item_name=item_name,
                reason="direct_batch_no_match",
                message="alltrails direct-link batch had no matching trail",
            )
            return None

        selected = self._prefer_canonical_alltrails_url(best[-1], item_name)
        self._remember_direct_batch_authoritative_url(selected, item_name)
        self._log_decision(
            kind="attraction",
            dest_name=dest_name,
            item_name=item_name,
            reason="direct_batch_selected",
            message="alltrails direct-link batch candidate selected",
            url=selected or "",
        )
        return selected

    def _search_attraction_from_direct_batch(self, item_name: str, dest_name: str, dates: str = "") -> str | None:
        rows = self._get_attraction_direct_batch_rows_for_destination(dest_name, dates)
        if not rows:
            return None

        if self._direct_batch_is_authoritative():
            matching_map_urls: list[str] = []
            matching_other_urls: list[str] = []
            row_level_non_map_urls: list[str] = []
            row_level_map_urls: list[str] = []
            saw_item_match = False
            matched_row_count = 0
            accepted_candidates: list[tuple[int, int, int, int, int, int, str]] = []
            for row in rows:
                if self._candidate_mentions_conflicting_destination(row, dest_name, item_name=item_name):
                    continue
                structured_url = self._normalize_direct_batch_authoritative_url(row.get("url", ""))
                row_match_strength = self._direct_batch_row_match_strength(row, item_name, dest_name)
                row_is_item_match = row_match_strength > 0
                saw_item_match = saw_item_match or row_is_item_match
                matched_row_count += 1 if row_is_item_match else 0
                for raw_url in self._direct_batch_row_url_candidates(row):
                    shallow_relevance = True
                    item_url_match = self._direct_batch_url_matches_item(raw_url, item_name, dest_name)
                    if not row_is_item_match and not item_url_match:
                        # A row that does not identify the requested item, and a URL
                        # that does not either, must never stand in for the item —
                        # otherwise an unrelated attraction's link can be selected
                        # below when no row in the batch actually matches.
                        continue
                    if raw_url != structured_url and not item_url_match:
                        if self._is_google_maps_candidate_url(raw_url):
                            continue
                    if self._is_alltrails_trail_url(raw_url):
                        continue
                    cleaned = self._retain_discovered_url(
                        raw_url,
                        item_name,
                        dest_name,
                        allow_alltrails=False,
                        kind="attraction",
                        candidate=row,
                        allow_shallow_relevance=shallow_relevance,
                        allow_google_maps_search=True,
                    )
                    if not cleaned:
                        self._log_decision(
                            kind="attraction",
                            dest_name=dest_name,
                            item_name=item_name,
                            reason="direct_batch_candidate_rejected",
                            message="direct-link batch candidate rejected during retention",
                            url=raw_url,
                        )
                        continue
                    is_maps = self._is_google_maps_candidate_url(cleaned)
                    is_row_primary = raw_url == structured_url
                    item_tokens = self._significant_tokens(item_name)
                    title_or_name = str(row.get("title") or row.get("name") or "").strip()
                    specificity = (
                        3 if self._direct_batch_url_matches_item(cleaned, item_name, dest_name) else 0
                    ) + (
                        2 if self._looks_like_item_specific_homepage(cleaned, item_name) else 0
                    ) + (
                        1 if bool(item_tokens and title_or_name and self._text_matches_item_tokens(title_or_name, item_tokens)) else 0
                    )
                    rank = (
                        # Corroboration signal: a row reached only through the
                        # lenient single-anchor-token fallback (strength 1) must
                        # never outrank -- or get pooled equally with -- a row
                        # that satisfies the full required token overlap
                        # (strength 2), e.g. two different "* Temple" entries in
                        # a "St. George" destination once the shared
                        # destination-name token is excluded from matching.
                        row_match_strength,
                        # A URL independently recommended by more than one
                        # discovery mechanism this run (e.g. also surfaced via
                        # AI-candidate resolution) is a corroboration signal in
                        # its own right -- prefer it in ties.
                        self._url_recommendation_source_count(cleaned),
                        specificity,
                        2 if is_maps else 0,
                        1 if is_row_primary else 0,
                        1,
                        cleaned,
                    )
                    accepted_candidates.append(rank)
                    if is_maps:
                        if is_row_primary or raw_url == self._normalize_direct_batch_authoritative_url(row.get("maps_url", "")):
                            row_level_map_urls.append(cleaned)
                        else:
                            matching_map_urls.append(cleaned)
                    else:
                        if is_row_primary:
                            row_level_non_map_urls.append(cleaned)
                        else:
                            matching_other_urls.append(cleaned)
            selected = ""
            if matched_row_count == 1:
                if row_level_non_map_urls:
                    selected = self._select_preferred_direct_batch_url(row_level_non_map_urls, kind="attraction")
                elif row_level_map_urls:
                    selected = self._select_preferred_direct_batch_url(row_level_map_urls, kind="attraction")
            elif accepted_candidates:
                best_strength = max(entry[0] for entry in accepted_candidates)
                strongest_candidates = [entry for entry in accepted_candidates if entry[0] == best_strength]
                candidate_urls = [entry[6] for entry in strongest_candidates]
                selected = self._select_preferred_direct_batch_url(candidate_urls, kind="attraction")
                if not selected:
                    selected = max(
                        strongest_candidates,
                        key=lambda entry: (entry[1], entry[2], entry[3], entry[4], entry[5]),
                    )[6]
            if selected and self._is_unverifiable_maps_query(selected):
                # Do not let a text-query Maps link stand as the attraction's
                # url: it cannot satisfy _item_has_verified_url, so returning
                # it resolves the item into deletion and suppresses the search
                # that would have found a real page. Fall through instead.
                self._log_decision(
                    kind="attraction",
                    dest_name=dest_name,
                    item_name=item_name,
                    reason="direct_batch_maps_query_not_accepted_as_url",
                    message=(
                        "maps text query declined as the item url; continuing to "
                        "search rather than resolving to an unverifiable link"
                    ),
                    url=selected,
                )
                selected = ""
            if selected:
                self._remember_direct_batch_authoritative_url(selected, item_name)
                self._log_decision(
                    kind="attraction",
                    dest_name=dest_name,
                    item_name=item_name,
                    reason="direct_batch_selected_authoritative",
                    message="attraction link (direct-link batch authoritative)",
                    url=selected,
                )
                return selected
            self._log_decision(
                kind="attraction",
                dest_name=dest_name,
                item_name=item_name,
                reason="direct_batch_no_accepted_candidates" if saw_item_match else "direct_batch_no_match",
                message=(
                    "direct-link batch had item matches but no accepted candidates"
                    if saw_item_match
                    else "direct-link batch had no item-matching candidates"
                ),
            )
            return None

        item_tokens = self._significant_tokens(item_name)
        ranked_maps: list[tuple[int, str]] = []
        ranked_other: list[tuple[int, str]] = []
        for row in rows:
            if self._candidate_mentions_conflicting_destination(row, dest_name):
                continue
            raw_url = str(row.get("url", "") or "").strip()
            if not raw_url or self._is_alltrails_trail_url(raw_url):
                continue
            if self._is_obviously_generic_url(raw_url.lower()):
                continue
            normalized = self._normalize_restaurant_url(raw_url)
            if not normalized:
                continue
            if item_tokens and not self._candidate_text_matches_item_tokens(row, item_tokens):
                continue
            scored = dict(row)
            scored["url"] = normalized
            score = self._score_candidate_result(
                scored,
                item_name,
                dest_name,
                specific=True,
                site_filter=None,
            )
            bucket = ranked_maps if self._is_deterministic_google_maps_place_url(normalized) else ranked_other
            bucket.append((score, normalized))

        for ranked in (ranked_maps, ranked_other):
            for _score, candidate_url in sorted(ranked, key=lambda row: row[0], reverse=True)[:4]:
                cleaned = self._retain_discovered_url(
                    candidate_url,
                    item_name,
                    dest_name,
                    allow_alltrails=False,
                    kind="attraction",
                )
                if cleaned:
                    return cleaned
        return None

    def _search_attraction_from_item_query_fanout(
        self,
        *,
        item_name: str,
        dest_name: str,
        nps_code: str | None,
    ) -> tuple[str | None, str]:
        """Per-item fallback fan-out for attraction URL discovery.

        Used only when destination-level direct-batch authoritative mode has no
        item match. We keep fail-closed semantics if this per-item fan-out also
        fails.
        """
        if self._direct_batch_is_authoritative():
            return None, "authoritative_direct_batch_lockout"
        maps_url = self._search_first(
            _build_attraction_maps_query_variants(item_name, dest_name),
            site_filter="google.com/maps",
            item_name=item_name,
            dest_name=dest_name,
            allow_alltrails=False,
        )
        if maps_url:
            return maps_url, "maps"

        maps_area_url = self._search_attraction_from_maps_area_pool(item_name, dest_name)
        if maps_area_url:
            return maps_area_url, "maps_area"

        category = "attraction landmark museum viewpoint"
        site_hint = f"site:nps.gov/{nps_code}" if nps_code else None
        nps_url = self._search_first(
            _build_query_variants(item_name, dest_name, category),
            site_filter="nps.gov" if nps_code else None,
            site_hint=site_hint,
            item_name=item_name,
            dest_name=dest_name,
            allow_alltrails=False,
        )
        if nps_url:
            return nps_url, "nps"

        if nps_code and self._is_category_style_activity(item_name):
            activity_url = self._search_first(
                _build_nps_activity_query_variants(item_name, dest_name),
                site_filter="nps.gov",
                site_hint=site_hint,
                item_name=item_name,
                dest_name=dest_name,
                allow_alltrails=False,
            )
            if activity_url:
                return activity_url, "nps_activity"

        web_url = self._search_first(
            _build_query_variants(item_name, dest_name, category),
            item_name=item_name,
            dest_name=dest_name,
            allow_alltrails=False,
        )
        if web_url:
            return web_url, "web"
        return None, "no_match"

    def _get_attraction_maps_area_rows_for_destination(self, dest_name: str, dates: str = "") -> list[dict[str, Any]]:
        if not hasattr(self, "_request_cache_lock"):
            self._request_cache_lock = Lock()
        if not hasattr(self, "_attraction_maps_area_cache"):
            self._attraction_maps_area_cache = {}

        key = self._batch_cache_key(dest_name, f"{dates}|maps_area")
        if not key:
            return []

        with self._request_cache_lock:
            cached = self._attraction_maps_area_cache.get(key)
            if cached is not None:
                return [dict(row) for row in cached if isinstance(row, dict)]

        self._note_fallback_call_site("attraction_maps_area_rows")
        rows = self._search_cached(
            self._attraction_maps_area_query(dest_name, dates),
            count=max(10, self._direct_link_batch_limit()),
        )
        normalized = [dict(row) for row in rows if isinstance(row, dict)]
        with self._request_cache_lock:
            self._attraction_maps_area_cache[key] = normalized
        return [dict(row) for row in normalized]

    def _search_attraction_from_maps_area_pool(self, item_name: str, dest_name: str) -> str | None:
        rows = self._get_attraction_maps_area_rows_for_destination(dest_name)
        if not rows:
            return None

        ranked_maps: list[tuple[int, str]] = []
        ranked_other: list[tuple[int, str]] = []
        for row in rows:
            if not self._direct_batch_row_matches_item(row, item_name, dest_name):
                continue

            structured_url = self._normalize_direct_batch_authoritative_url(row.get("url", ""))
            for raw_url in self._direct_batch_row_url_candidates(row):
                if raw_url != structured_url and not self._direct_batch_url_matches_item(raw_url, item_name, dest_name):
                    continue
                if self._is_alltrails_trail_url(raw_url):
                    continue

                cleaned = self._retain_discovered_url(
                    raw_url,
                    item_name,
                    dest_name,
                    allow_alltrails=False,
                    kind="attraction",
                    allow_google_maps_search=True,
                )
                if not cleaned:
                    continue
                if (
                    self._classify_url_policy_class(cleaned) == "google_maps_search"
                    and not self._direct_batch_url_matches_item(cleaned, item_name, dest_name)
                ):
                    continue

                scored = dict(row)
                scored["url"] = cleaned
                score = self._score_candidate_result(
                    scored,
                    item_name,
                    dest_name,
                    specific=True,
                    site_filter=None,
                )
                bucket = ranked_maps if self._is_google_maps_candidate_url(cleaned) else ranked_other
                bucket.append((score, cleaned))

        for ranked in (ranked_maps, ranked_other):
            if ranked:
                ranked.sort(key=lambda row: row[0], reverse=True)
                return ranked[0][1]
        return None

    def _search_restaurant_from_direct_batch(self, item_name: str, dest_name: str, dates: str = "", lodging_location: str = "") -> str | None:
        rows = self._get_restaurant_direct_batch_rows_for_destination(dest_name, dates, lodging_location=lodging_location)
        if not rows:
            return None

        if self._direct_batch_is_authoritative():
            matching_map_urls: list[str] = []
            matching_other_urls: list[str] = []
            saw_item_match = False
            for row in rows:
                if self._candidate_mentions_conflicting_destination(row, dest_name, item_name=item_name):
                    continue
                row_is_item_match = self._direct_batch_row_matches_item(row, item_name, dest_name)
                saw_item_match = saw_item_match or row_is_item_match
                structured_url = self._normalize_direct_batch_authoritative_url(row.get("url", ""))
                for raw_url in self._direct_batch_row_url_candidates(row):
                    item_url_match = self._direct_batch_url_matches_item(raw_url, item_name, dest_name)
                    if not row_is_item_match and not item_url_match:
                        # An unmatched row/URL is never treated as the requested item inline.
                        # The only sanctioned no-match fallback is the post-loop raw-capture
                        # pass below, which applies the full generic-landing-page gate
                        # (`_is_generic_restaurant_landing_url`) instead of a narrower inline
                        # heuristic that could accept an unrelated restaurant's link.
                        continue
                    is_maps = self._is_google_maps_candidate_url(raw_url)
                    shallow_relevance = self._direct_batch_authoritative and not is_maps
                    if raw_url != structured_url:
                        if is_maps and not item_url_match:
                            continue
                    if raw_url == structured_url and self._direct_batch_is_authoritative():
                        # Raw direct-batch HTML captures are the contract baseline. Preserve the
                        # primary raw URL unless it is an obvious area-list page such as a
                        # TripAdvisor restaurant index or a destination-wide dining landing page.
                        lower = (raw_url or "").lower()
                        obvious_area_listing = (
                            "tripadvisor." in lower and "/restaurants-" in lower
                            or any(marker in lower for marker in ("best-restaurants-near", "restaurants-near"))
                            or "/restaurants/" in urlparse(raw_url).path.lower()
                        )
                        if obvious_area_listing:
                            self._log_decision(
                                kind="restaurant",
                                dest_name=dest_name,
                                item_name=item_name,
                                reason="direct_batch_candidate_rejected_generic",
                                message="direct-link batch candidate rejected (generic/area landing)",
                                url=raw_url,
                            )
                            continue
                    elif self._is_generic_restaurant_landing_url(raw_url, item_name, dest_name,
                            item_tokens=self._restaurant_significant_tokens(item_name)):
                        self._log_decision(
                            kind="restaurant",
                            dest_name=dest_name,
                            item_name=item_name,
                            reason="direct_batch_candidate_rejected_generic",
                            message="direct-link batch candidate rejected (generic/area landing)",
                            url=raw_url,
                        )
                        continue
                    cleaned = self._retain_discovered_url(
                        raw_url,
                        item_name,
                        dest_name,
                        allow_alltrails=False,
                        kind="restaurant",
                        candidate=row,
                        allow_shallow_relevance=shallow_relevance,
                        allow_google_maps_search=True,
                    )
                    if not cleaned:
                        self._log_decision(
                            kind="restaurant",
                            dest_name=dest_name,
                            item_name=item_name,
                            reason="direct_batch_candidate_rejected",
                            message="direct-link batch candidate rejected during retention",
                            url=raw_url,
                        )
                        continue
                    if self._is_google_maps_candidate_url(cleaned):
                        matching_map_urls.append(cleaned)
                    else:
                        matching_other_urls.append(cleaned)
            selected = ""
            if matching_other_urls:
                selected = self._select_preferred_direct_batch_url(matching_other_urls, kind="restaurant")
            elif matching_map_urls:
                selected = self._select_preferred_direct_batch_url(matching_map_urls, kind="restaurant")
            if selected and matching_map_urls and self._is_generic_restaurant_landing_url(selected, item_name, dest_name):
                selected = self._select_preferred_direct_batch_url(matching_map_urls, kind="restaurant")
            # No unmatched-row raw-capture fallback: per the fail-closed named-entity
            # contract, a destination batch with no row/URL match for this item must
            # publish no canonical URL rather than borrow another restaurant's link.
            if selected:
                self._remember_direct_batch_authoritative_url(selected, item_name)
                return selected
            self._log_decision(
                kind="restaurant",
                dest_name=dest_name,
                item_name=item_name,
                reason="direct_batch_no_accepted_candidates" if saw_item_match else "direct_batch_no_match",
                message=(
                    "direct-link batch had item matches but no accepted candidates"
                    if saw_item_match
                    else "direct-link batch had no item-matching candidates"
                ),
            )
            return None

        item_tokens = self._significant_tokens(item_name)
        ranked_maps: list[tuple[int, str]] = []
        ranked_other: list[tuple[int, str]] = []
        for row in rows:
            if item_tokens and not self._candidate_text_matches_item_tokens(row, item_tokens):
                continue
            for raw_url in self._direct_batch_row_url_candidates(row):
                candidate_url = self._normalize_restaurant_url(raw_url)
                if not candidate_url:
                    if self._is_google_maps_candidate_url(raw_url):
                        candidate_url = raw_url.split("#", 1)[0]
                    else:
                        continue
                if self._is_generic_restaurant_landing_url(candidate_url, item_name, dest_name,
                        item_tokens=self._restaurant_significant_tokens(item_name)):
                    continue
                cleaned = self._retain_discovered_url(
                    candidate_url,
                    item_name,
                    dest_name,
                    allow_alltrails=False,
                    kind="restaurant",
                    allow_google_maps_search=True,
                )
                if not cleaned:
                    continue
                scored = dict(row)
                scored["url"] = cleaned
                score = self._score_candidate_result(
                    scored,
                    item_name,
                    dest_name,
                    specific=True,
                    site_filter=None,
                )
                bucket = ranked_maps if self._is_google_maps_candidate_url(cleaned) else ranked_other
                bucket.append((score, cleaned))

        for ranked in (ranked_maps, ranked_other):
            if ranked:
                ranked.sort(key=lambda row: row[0], reverse=True)
                return ranked[0][1]
        return None

    def _is_generic_restaurant_landing_url(self, url: str, item_name: str, dest_name: str, *, item_tokens: list[str] | None = None) -> bool:
        candidate = str(url or "").strip()
        if not candidate:
            return True

        lower_url = unquote(candidate).replace("+", " ").lower()
        parsed = urlparse(candidate)
        host = (parsed.netloc or "").lower()
        path_l = unquote(parsed.path or "").lower()

        # Area/list pages should not be linked as a specific restaurant target.
        if "tripadvisor." in host and (
            path_l.startswith("/restaurants-") or path_l.startswith("/restaurantsnear-")
        ):
            return True
        if any(marker in lower_url for marker in ("best-restaurants-near", "restaurants-near", "restaurantsnear")):
            return True
        if "/restaurants/" in path_l and not self._is_google_maps_candidate_url(candidate):
            return True

        tokens = item_tokens if item_tokens is not None else self._significant_tokens(item_name)
        required_overlap = self._required_general_token_matches(len(tokens)) if tokens else 0

        if self._is_google_maps_candidate_url(candidate):
            path_lower = (parsed.path or "").lower()
            query_lower = (parsed.query or "").lower()

            if "/maps/search" not in path_lower:
                if ("q=" in query_lower or "query=" in query_lower) and tokens:
                    overlap = sum(1 for token in tokens if token in lower_url)
                    if overlap < required_overlap:
                        return True
                return False

            if re.search(r"\brestaurants?\s+near\b", lower_url):
                return True
            if tokens:
                overlap = sum(1 for token in tokens if token in lower_url)
                if overlap < required_overlap:
                    return True
            return False

        if not tokens:
            return False

        path_tokens_text = unquote(parsed.path or "").replace("-", " ").replace("_", " ").lower()
        host_tokens_text = host.replace("-", " ")
        token_overlap = sum(
            1 for token in tokens if (token in path_tokens_text or token in host_tokens_text)
        )
        return token_overlap < required_overlap

    @staticmethod
    def _direct_batch_url_priority(url: str, *, kind: str) -> int:
        lower = (url or "").lower()
        is_maps_search = (
            "google.com/maps/search" in lower
            or "maps.google.com" in lower and ("?q=" in lower or "&q=" in lower or "?query=" in lower or "&query=" in lower)
        )
        is_maps_place = "/maps/place/" in lower or "maps.google.com/place" in lower
        is_tripadvisor = "tripadvisor" in lower

        if kind == "restaurant":
            if is_tripadvisor:
                return 30
            if is_maps_search:
                return 40
            if is_maps_place:
                return 50
            return 10

        if kind == "en_route_stop":
            if is_tripadvisor:
                return 25
            if is_maps_search:
                return 35
            if is_maps_place:
                return 45
            return 15

        if kind == "attraction":
            # Lower number wins (see _select_preferred_direct_batch_url). A specific
            # official/source page is always more useful than a Maps search query or
            # a generic listing page, so those must rank worst, not best -- this
            # block previously had maps_search/maps_place scored as the *lowest*
            # (winning) numbers, which meant a vague Maps search link was preferred
            # over the attraction's own official page whenever both were candidates.
            parsed = urlparse(url or "")
            path_l = (unquote(parsed.path or "") or "").lower()
            generic_markers = (
                "/places-to-go/",
                "/cities-and-towns/",
                "/things-to-do/",
                "/things2do/",
                "/attractions/",
                "/activities/",
                "/explore/",
                "/food/",
                "/restaurants/",
                "/dining/",
                "/visit/",
                "/about/",
                "/home",
            )
            if is_maps_search:
                return 90
            if is_tripadvisor:
                return 70
            if any(marker in path_l for marker in generic_markers):
                return 60
            if is_maps_place:
                return 30
            if "nps.gov" in lower and "/planyourvisit/" in path_l:
                return 20
            return 10

        if is_tripadvisor:
            return 25
        if is_maps_search:
            return 35
        if is_maps_place:
            return 45
        return 10

    def _select_preferred_direct_batch_url(self, candidates: list[str], *, kind: str) -> str:
        valid = [url for url in candidates if url and str(url).strip()]
        if not valid:
            return ""
        ordered = sorted(valid, key=lambda url: self._direct_batch_url_priority(url, kind=kind))
        return ordered[0]

    def _search_en_route_stop_from_direct_batch(
        self,
        item_name: str,
        dest_name: str,
        dates: str = "",
        origin_name: str = "",
    ) -> str | None:
        rows = self._get_en_route_direct_batch_rows_for_destination(dest_name, dates, origin_name)
        if not rows:
            self._log_decision(
                kind="en_route_stop",
                dest_name=dest_name,
                item_name=item_name,
                reason="direct_batch_empty",
                message="en-route direct-link batch empty",
            )
            return None

        if self._direct_batch_is_authoritative():
            matching_map_urls: list[tuple[int, str]] = []
            matching_other_urls: list[tuple[int, str]] = []
            saw_item_match = False
            for row in rows:
                if self._candidate_mentions_conflicting_destination(row, dest_name, item_name=item_name):
                    continue
                row_is_item_match = self._direct_batch_row_matches_item(row, item_name, dest_name)
                saw_item_match = saw_item_match or row_is_item_match
                structured_url = self._normalize_direct_batch_authoritative_url(row.get("url", ""))
                for raw_url in self._direct_batch_row_url_candidates(row):
                    shallow_relevance = (
                        raw_url != structured_url and not self._is_google_maps_candidate_url(raw_url)
                    )
                    if raw_url != structured_url:
                        if self._is_google_maps_candidate_url(raw_url) and not self._direct_batch_url_matches_item(raw_url, item_name, dest_name):
                            continue
                    if self._is_alltrails_trail_url(raw_url):
                        continue
                    cleaned = self._retain_discovered_url(
                        raw_url,
                        item_name,
                        dest_name,
                        allow_alltrails=False,
                        kind="en-route stop",
                        candidate=row,
                        allow_shallow_relevance=shallow_relevance,
                        allow_google_maps_search=True,
                    )
                    if not cleaned:
                        self._log_decision(
                            kind="en_route_stop",
                            dest_name=dest_name,
                            item_name=item_name,
                            reason="direct_batch_candidate_rejected",
                            message="direct-link batch candidate rejected during retention",
                            url=raw_url,
                        )
                        continue
                    scored = dict(row)
                    scored["url"] = cleaned
                    score = self._score_candidate_result(
                        scored,
                        item_name,
                        dest_name,
                        specific=True,
                        site_filter=None,
                    )
                    if self._is_google_maps_candidate_url(cleaned):
                        matching_map_urls.append((score, cleaned))
                    else:
                        matching_other_urls.append((score, cleaned))

            selected = ""
            # En-route stops are navigation waypoints, so a Maps result is
            # normally preferred over a source page. But a generic
            # maps-search/-dir URL is documented as never qualifying as
            # canonical entity evidence (docs/design/url-discovery-and-audit.md)
            # and _item_has_verified_url explicitly refuses to count it as
            # "verified" -- so picking one here over an already-retained real
            # source page doesn't just deprioritize the source page, it
            # guarantees the item gets discarded outright at audit with
            # nothing to show (dipstick67: Mancos State Park's real
            # cpw.state.co.us page was found and preserved, then out-selected
            # for a maps-search URL, which then failed audit and the whole
            # stop was removed). A deterministic Maps *place* URL (its own
            # policy class, not search/dir) IS audit-verified, so it keeps
            # winning over a source page exactly as before.
            best_map = (
                sorted(matching_map_urls, key=lambda row: row[0], reverse=True)[0][1]
                if matching_map_urls else ""
            )
            best_other = (
                sorted(matching_other_urls, key=lambda row: row[0], reverse=True)[0][1]
                if matching_other_urls else ""
            )
            if (
                best_map
                and best_other
                and self._classify_url_policy_class(best_map) in {"google_maps_search", "google_maps_dir"}
            ):
                selected = best_other
            elif best_map:
                selected = best_map
            else:
                selected = best_other
            if selected:
                self._remember_direct_batch_authoritative_url(selected, item_name)
                self._log_decision(
                    kind="en_route_stop",
                    dest_name=dest_name,
                    item_name=item_name,
                    reason="direct_batch_selected_authoritative",
                    message="en-route link (direct-link batch authoritative)",
                    url=selected,
                )
                return selected
            self._log_decision(
                kind="en_route_stop",
                dest_name=dest_name,
                item_name=item_name,
                reason="direct_batch_no_accepted_candidates" if saw_item_match else "direct_batch_no_match",
                message=(
                    "en-route direct-link batch had item matches but no accepted candidates"
                    if saw_item_match
                    else "en-route direct-link batch had no item-matching link"
                ),
            )
            return None

        item_tokens = self._significant_tokens(item_name)
        ranked_maps: list[tuple[int, str]] = []
        ranked_other: list[tuple[int, str]] = []
        for row in rows:
            if item_tokens and not self._candidate_text_matches_item_tokens(row, item_tokens):
                continue
            for raw_url in self._direct_batch_row_url_candidates(row):
                if not raw_url or self._is_alltrails_trail_url(raw_url):
                    continue
                candidate_url = self._normalize_restaurant_url(raw_url)
                if not candidate_url:
                    if self._is_google_maps_candidate_url(raw_url):
                        candidate_url = raw_url.split("#", 1)[0]
                    else:
                        continue
                cleaned = self._retain_discovered_url(
                    candidate_url,
                    item_name,
                    dest_name,
                    allow_alltrails=False,
                    kind="en-route stop",
                )
                if not cleaned:
                    continue
                scored = dict(row)
                scored["url"] = cleaned
                score = self._score_candidate_result(
                    scored,
                    item_name,
                    dest_name,
                    specific=True,
                    site_filter=None,
                )
                bucket = ranked_maps if self._is_google_maps_candidate_url(cleaned) else ranked_other
                bucket.append((score, cleaned))

        for ranked in (ranked_maps, ranked_other):
            if ranked:
                selected = sorted(ranked, key=lambda row: row[0], reverse=True)[0][1]
                self._log_decision(
                    kind="en_route_stop",
                    dest_name=dest_name,
                    item_name=item_name,
                    reason="direct_batch_selected",
                    message="en-route link (direct-link batch)",
                    url=selected,
                )
                return selected

        self._log_decision(
            kind="en_route_stop",
            dest_name=dest_name,
            item_name=item_name,
            reason="direct_batch_no_match",
            message="en-route direct-link batch had no matching stop",
        )
        return None

    def _search_alltrails_for_trail(
        self,
        item_name: str,
        dest_name: str,
        dates: str = "",
        *,
        allow_when_disabled: bool = False,
        force_search: bool = False,
    ) -> str | None:
        """Exhaust high-signal AllTrails queries before non-AllTrails fallback.

        `allow_when_disabled` is for the per-leg trail link, which the
        manifest asks for by naming a trail rather than by the --trails
        switch. See _discover_leg_trail_link.

        `force_search` skips the direct-link batch. That batch is a
        per-destination harvest of trails NEAR one stop, so a leg's trail
        cannot be in it by construction -- and in authoritative mode its
        no-match returns None before the search variants are ever tried,
        which is how the first PCT run asked for "Pacific Crest Trail
        Callahan's Lodge to Fish Lake Resort" and got nothing.
        """
        if bool(getattr(self, "_disable_trails", False)) and not allow_when_disabled:
            return None

        source_mode = (
            "search" if force_search
            else str(getattr(self, "_alltrails_source", "search") or "search")
        )
        if source_mode == "direct_link_batch":
            selected = self._search_alltrails_for_trail_from_direct_batch(item_name, dest_name, dates)
            if selected:
                selected = self._prefer_canonical_alltrails_url(selected, item_name)
                if self._passes_alltrails_post_search_filters(selected, item_name, dest_name):
                    return selected
                self._log_decision(
                    kind="attraction",
                    dest_name=dest_name,
                    item_name=item_name,
                    reason="post_search_constraints_rejected",
                    message="alltrails direct-link batch candidate rejected by post-search constraints",
                    url=selected or "",
                )
                if self._direct_batch_is_authoritative():
                    return None
                # Do not hard-stop here: if the direct-batch candidate is weak or
                # mismatched, broader search / NPS / maps fallbacks should still be
                # allowed. Direct-batch entries remain highest priority only when
                # they are actually viable.
            elif self._direct_batch_is_authoritative():
                self._log_decision(
                    kind="attraction",
                    dest_name=dest_name,
                    item_name=item_name,
                    reason="direct_batch_no_match",
                    message="alltrails direct-link batch had no item-matching trail in authoritative mode",
                )
                return None
        alltrails_variants: list[str] = _build_alltrails_query_variants(item_name, dest_name)

        # Preserve order while removing duplicates.
        seen: set[str] = set()
        deduped_variants: list[str] = []
        for q in alltrails_variants:
            key = (q or "").strip().lower()
            if not key or key in seen:
                continue
            seen.add(key)
            deduped_variants.append(q)

        if bool(getattr(self, "_enable_filtered_alltrails_selection", DEFAULT_ENABLE_FILTERED_ALLTRAILS_SELECTION)):
            # Use constrained selection first, then allow broader AllTrails
            # matching when snippets lack complete metadata.
            filtered = self._get_filtered_alltrails_selection(
                item_name=item_name,
                dest_name=dest_name,
                query_variants=deduped_variants,
            )
            if filtered:
                return filtered

        resolved = self._search_first(
            deduped_variants,
            site_filter="alltrails.com",
            item_name=item_name,
            dest_name=dest_name,
            max_attempts=min(len(deduped_variants), int(getattr(self, "_max_alltrails_query_attempts", 5) or 5)),
        )
        selected = self._prefer_canonical_alltrails_url(resolved, item_name)
        if self._passes_alltrails_post_search_filters(selected, item_name, dest_name):
            return selected
        self._log_decision(
            kind="attraction",
            dest_name=dest_name,
            item_name=item_name,
            reason="post_search_constraints_rejected",
            message="alltrails broad search candidate rejected by post-search constraints",
            url=selected or "",
        )
        return None

    def _search_alltrails_for_seed_relaxed(self, item_name: str, dest_name: str) -> str | None:
        """Seed-only fallback: keep strong canonical AllTrails candidates."""
        if bool(getattr(self, "_disable_trails", False)):
            return None

        query_variants = _build_alltrails_query_variants(item_name, dest_name)
        seen: set[str] = set()
        deduped_variants: list[str] = []
        for q in query_variants:
            key = (q or "").strip().lower()
            if not key or key in seen:
                continue
            seen.add(key)
            deduped_variants.append(q)

        resolved = self._search_first(
            deduped_variants,
            site_filter="alltrails.com",
            item_name=item_name,
            dest_name=dest_name,
            max_attempts=min(len(deduped_variants), int(getattr(self, "_max_alltrails_query_attempts", 5) or 5)),
        )
        selected = self._prefer_canonical_alltrails_url(resolved, item_name)
        if not selected or not self._alltrails_url_meets_seed_relaxed_standard(selected, item_name):
            return None
        return selected

    def _alltrails_url_meets_seed_relaxed_standard(self, url: str | None, item_name: str) -> bool:
        """Seed-appropriate AllTrails acceptance standard: liveness, no closure
        marker, and slug/entity match -- deliberately *not* the length/gain/
        difficulty confidence gate (_meets_alltrails_publish_confidence /
        _passes_alltrails_post_search_filters), which is tuned for the
        family-short-hike policy (max_trail_miles) and has no seed exemption.

        A seed is an item the trip owner explicitly asked to include, so the
        same length/difficulty rules that legitimately demote a *non-seed*
        AllTrails candidate (e.g. "The Narrows" in Zion, a long/strenuous
        through-hike) must not silently re-reject a seed that already passed
        this exact relaxed standard once (in _search_alltrails_for_seed_relaxed,
        at attach time). Reusing the same standard here -- in the later
        audit_discovered_urls() safety pass via _retain_discovered_url -- keeps
        attach-time and retain-time decisions consistent instead of accepting
        a seed's AllTrails link and then discarding it moments later.
        """
        if not url or not self._is_alltrails_trail_url(url):
            return False

        verified_ok, verified_status = self._verify_url_cached(url)
        if not verified_ok and self._is_definitively_dead_status(verified_status):
            return False

        ok, _status, text = self._fetch_page_text(url, timeout=8)
        if ok and text and self._has_alltrails_closure_marker(text):
            return False

        if not self._alltrails_slug_matches_item(url, item_name):
            return False

        return True

    def _passes_alltrails_post_search_filters(self, url: str | None, item_name: str, dest_name: str) -> bool:
        if not url or not self._is_alltrails_trail_url(url):
            return True

        # Dead slugs should fail closed regardless of filtered selection mode.
        verified_ok, verified_status = self._verify_url_cached(url)
        if not verified_ok and self._is_definitively_dead_status(verified_status):
            return False

        if not bool(getattr(self, "_enable_filtered_alltrails_selection", DEFAULT_ENABLE_FILTERED_ALLTRAILS_SELECTION)):
            return True

        ok, status, text = self._fetch_page_text(url, timeout=8)
        if not ok or not text:
            if self._is_definitively_dead_status(status):
                return False
            # Preserve fail-open behavior for bot-blocked/transient fetch failures.
            return True

        meta = self._extract_alltrails_candidate_metadata(
            {
                "url": url,
                "title": item_name,
                "snippet": text,
                "description": text,
            }
        )

        allowed_difficulties = {
            str(item or "").strip().lower()
            for item in getattr(
                self,
                "_alltrails_filter_allowed_difficulties",
                DEFAULT_ALLTRAILS_FILTER_ALLOWED_DIFFICULTIES,
            )
            if str(item or "").strip()
        }
        difficulty = str(meta.get("difficulty") or "").strip().lower()
        if difficulty and allowed_difficulties and difficulty not in allowed_difficulties:
            return False

        filter_max_miles = float(getattr(self, "_alltrails_filter_max_miles", DEFAULT_ALLTRAILS_FILTER_MAX_MILES) or 0)
        publish_max_miles = float(getattr(self, "_max_trail_miles", DEFAULT_MAX_TRAIL_MILES) or 0)
        max_miles_candidates = [v for v in (filter_max_miles, publish_max_miles) if v and v > 0]
        effective_max_miles = min(max_miles_candidates) if max_miles_candidates else 0.0
        miles = meta.get("miles")
        if miles is not None and effective_max_miles > 0 and float(miles) > effective_max_miles:
            return False

        max_gain = int(getattr(self, "_alltrails_filter_max_gain_feet", DEFAULT_ALLTRAILS_FILTER_MAX_GAIN_FEET) or 0)
        gain_feet = meta.get("gain_feet")
        if gain_feet is not None and max_gain >= 0 and int(gain_feet) > max_gain:
            return False

        min_reviews = int(getattr(self, "_alltrails_filter_min_reviews", DEFAULT_ALLTRAILS_FILTER_MIN_REVIEWS) or 0)
        reviews = meta.get("reviews")
        if reviews is not None and min_reviews > 0 and int(reviews) < min_reviews:
            return False

        return True

    def _get_filtered_alltrails_selection(
        self,
        *,
        item_name: str,
        dest_name: str,
        query_variants: list[str] | None = None,
    ) -> str | None:
        if not hasattr(self, "_alltrails_filtered_selection_cache"):
            self._alltrails_filtered_selection_cache = {}
        cache_key = ((item_name or "").strip().lower(), (dest_name or "").strip().lower())
        if cache_key in self._alltrails_filtered_selection_cache:
            return self._alltrails_filtered_selection_cache[cache_key]

        variants = query_variants if query_variants is not None else _build_alltrails_query_variants(item_name, dest_name)
        filtered = self._search_alltrails_for_trail_filtered(
            item_name=item_name,
            dest_name=dest_name,
            query_variants=variants,
        )
        self._alltrails_filtered_selection_cache[cache_key] = filtered
        return filtered

    @staticmethod
    def _same_alltrails_trail(url_a: str, url_b: str) -> bool:
        if not url_a or not url_b:
            return False
        a = urlparse(url_a)
        b = urlparse(url_b)
        if "alltrails.com" not in (a.netloc or "").lower() or "alltrails.com" not in (b.netloc or "").lower():
            return False
        return unquote((a.path or "").rstrip("/")).lower() == unquote((b.path or "").rstrip("/")).lower()

    def _search_alltrails_for_trail_filtered(
        self,
        *,
        item_name: str,
        dest_name: str,
        query_variants: list[str],
    ) -> str | None:
        # Honour the trails off-switch here too. Its two siblings --
        # _search_alltrails_for_trail and _search_alltrails_for_seed_relaxed --
        # have always checked _disable_trails; this one did not, and it is the
        # highest-volume of the three. Measured 2026-08-23 with trails
        # DISABLED: alltrails_trail_filtered still made 98 paid fallback calls,
        # so the switch was suppressing the links while still buying them.
        if bool(getattr(self, "_disable_trails", False)):
            return None

        max_attempts = min(
            len(query_variants),
            int(getattr(self, "_max_alltrails_query_attempts", 5) or 5),
        )
        allowed_difficulties = set(
            getattr(
                self,
                "_alltrails_filter_allowed_difficulties",
                DEFAULT_ALLTRAILS_FILTER_ALLOWED_DIFFICULTIES,
            )
        )
        max_miles = float(getattr(self, "_alltrails_filter_max_miles", DEFAULT_ALLTRAILS_FILTER_MAX_MILES) or 0)
        max_gain = int(getattr(self, "_alltrails_filter_max_gain_feet", DEFAULT_ALLTRAILS_FILTER_MAX_GAIN_FEET) or 0)
        min_reviews = int(getattr(self, "_alltrails_filter_min_reviews", DEFAULT_ALLTRAILS_FILTER_MIN_REVIEWS) or 0)

        best: tuple[float, int, float, int, str] | None = None

        for query in query_variants[:max_attempts]:
            full_query = f"site:alltrails.com {query}"
            self._note_fallback_call_site("alltrails_trail_filtered")
            candidates = self._search_cached(full_query, count=10)
            for candidate in candidates:
                url = str(candidate.get("url", "") or "")
                if not self._is_alltrails_trail_url(url):
                    continue
                if not self._alltrails_slug_matches_item(url, item_name):
                    continue
                if self._alltrails_slug_has_numbered_suffix(url):
                    continue

                meta = self._extract_alltrails_candidate_metadata(candidate)
                difficulty = str(meta.get("difficulty") or "").lower()
                if difficulty not in allowed_difficulties:
                    continue
                miles = meta.get("miles")
                if miles is None or (max_miles > 0 and float(miles) > max_miles):
                    continue
                gain_feet = meta.get("gain_feet")
                if gain_feet is None or (max_gain >= 0 and int(gain_feet) > max_gain):
                    continue
                reviews = meta.get("reviews")
                if reviews is None or int(reviews) < min_reviews:
                    continue
                rating = meta.get("rating")
                if rating is None:
                    continue

                # Highest rating first, then review volume, then shorter/easier trail.
                rank = (
                    float(rating),
                    int(reviews),
                    -float(miles),
                    -int(gain_feet),
                    url,
                )
                if best is None or rank > best:
                    best = rank

        if not best:
            return None

        return self._prefer_canonical_alltrails_url(best[-1], item_name)

    def _extract_alltrails_candidate_metadata(self, candidate: dict[str, Any] | None) -> dict[str, Any]:
        text = self._candidate_text_blob(candidate)
        rating, reviews = self._extract_rating_votes(text)
        difficulty = self._extract_alltrails_difficulty(text)
        miles = self._extract_trail_miles(text)
        gain_feet = self._extract_elevation_gain_feet(text)
        return {
            "rating": rating,
            "reviews": reviews,
            "difficulty": difficulty,
            "miles": miles,
            "gain_feet": gain_feet,
        }

    @staticmethod
    def _extract_alltrails_difficulty(text: str) -> str | None:
        t = str(text or "").lower()
        if "moderately challenging" in t:
            return "moderately challenging"
        if re.search(r"\bmoderate\b", t):
            return "moderate"
        if re.search(r"\beasy\b", t):
            return "easy"
        if re.search(r"\b(hard|difficult|challenging|strenuous)\b", t):
            return "hard"
        return None

    @staticmethod
    def _extract_elevation_gain_feet(text: str) -> int | None:
        t = str(text or "").lower()

        patterns = (
            r"(?:elevation\s*gain|gain)\s*[:\-]?\s*(\d{1,3}(?:,\d{3})?|\d+)\s*(?:ft|feet|foot)\b",
            r"(\d{1,3}(?:,\d{3})?|\d+)\s*(?:ft|feet|foot)\s*(?:elevation\s*gain|gain)\b",
        )
        for pattern in patterns:
            m = re.search(pattern, t, flags=re.IGNORECASE)
            if not m:
                continue
            try:
                return int(str(m.group(1)).replace(",", ""))
            except (TypeError, ValueError):
                continue
        return None

    @staticmethod
    def _extract_alltrails_geo_from_html(html: str) -> tuple[float, float] | None:
        """Extract a trail's real trailhead coordinate from its own AllTrails
        page's JSON-LD structured data.

        Verified live against a real, currently-served AllTrails trail page
        (Hickman Bridge Trail, Capitol Reef, fetched via a real browser
        session on 2026-08-18) -- AllTrails embeds a
        `<script type="application/ld+json">` block shaped like
        `{"@type": "LocalBusiness", "geo": {"@type": "GeoCoordinates",
        "latitude": "38.28876", "longitude": "-111.22765"}, ...}` alongside
        two other unrelated ld+json blocks (`WebPage`, `BreadcrumbList`) on
        the same page. Note AllTrails serializes latitude/longitude as JSON
        *strings*, not numbers -- this must float()-cast rather than assume a
        numeric type. Mirrors _extract_restaurant_meta_from_html's
        JSON-LD-block-scanning pattern (same file, iterate every ld+json
        block on the page and use whichever one actually has the field we
        want) but pulls the `geo` field instead of restaurant-specific ones.

        Returns None -- never a fabricated/estimated coordinate -- whenever
        no block contains a well-formed geo field; callers must leave the
        pre-existing maps_url behavior completely untouched in that case.
        """
        text = str(html or "")
        if not text:
            return None
        for ld_match in re.finditer(
            r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
            text,
            re.DOTALL | re.IGNORECASE,
        ):
            try:
                data = json.loads(ld_match.group(1))
            except (json.JSONDecodeError, TypeError):
                continue
            if not isinstance(data, dict):
                continue
            geo = data.get("geo")
            if not isinstance(geo, dict):
                continue
            try:
                lat = float(geo.get("latitude"))
                lng = float(geo.get("longitude"))
            except (TypeError, ValueError):
                continue
            # Defensive range/sanity check -- a malformed or truncated
            # JSON-LD blob could parse into garbage numbers without raising;
            # never trust an out-of-range or null-island value as real.
            if not (-90.0 <= lat <= 90.0 and -180.0 <= lng <= 180.0):
                continue
            if lat == 0.0 and lng == 0.0:
                continue
            return (lat, lng)
        return None

    def _alltrails_geo_maps_url(self, url: str) -> str | None:
        """Real-coordinate Google Maps link for an already-accepted AllTrails
        trail URL, built from that specific trail's own page JSON-LD `geo`
        field (see _extract_alltrails_geo_from_html above). This is what
        actually answers the project owner's ask ("a map link for each
        AllTrails trail that will take you to the trail") with a link a
        scraper can follow -- AllTrails' own "Get Directions" button is
        client-JS-driven with no static href to extract, so the page's own
        structured geo data is the reliable source, and it is the trail's
        exact trailhead coordinate rather than a fuzzy name-based geocode.

        Returns None on any fetch or parse failure. Callers must leave
        whatever maps_url the existing fallback logic already produced
        completely untouched in that case -- this codebase has a hard rule
        against inventing URLs/data (see the module docstring), so a failed
        extraction must never be papered over with a generic search-query
        link dressed up as a coordinate link.

        Routes through _fetch_page_text, which dispatches AllTrails URLs to
        _fetch_alltrails_text -- both cache per-URL in memory
        (_alltrails_fetch_cache) and persist successful fetches to the
        on-disk cache (_load_persistent_caches' alltrails_fetch_results
        section), so calling this on an already-accepted trail whose page
        was already fetched earlier in the same audit_discovered_urls pass
        (e.g. the trail-miles threshold check just above) or in an earlier
        run within the cache TTL is a cache hit, not a second live request.

        Falls back to _fetch_wayback_alltrails_text when the direct fetch
        fails -- in production this is the common case, not the rare one:
        AllTrails' DataDome bot-detection blocks this app's own direct
        fetches of trail pages essentially universally (confirmed via a
        real production run, dipstick71, where this feature fired zero
        times across 19 real AllTrails attractions despite passing all of
        its own unit tests). The direct fetch is still tried first and kept
        as the primary path -- it costs nothing extra when it works (e.g. a
        very new trail page not yet archived, or a lucky non-blocked
        window) -- but the Wayback Machine fallback is what actually makes
        this feature fire in practice. See _fetch_wayback_alltrails_text
        for the archive.org lookup/fetch details and its own caching.
        """
        if not url or not self._is_alltrails_trail_url(url):
            return None
        ok, _status, text = self._fetch_page_text(url, timeout=8)
        if not ok or not text:
            # A longer timeout than the direct-fetch path above: archive.org's
            # web.archive.org playback proxy is noticeably slower than a
            # normal site fetch for a ~1-1.2MB archived AllTrails page (live-
            # reproduced 2026-08-18 -- an 8s timeout genuinely wasn't enough
            # some of the time and turned a slow-but-real snapshot fetch into
            # a silent failure indistinguishable from "no snapshot"). 20s
            # still fails fast relative to how rare this path's own retry
            # already makes a *permanent* miss.
            ok, _status, text = self._fetch_wayback_alltrails_text(url, timeout=20)
            if not ok or not text:
                return None
        coords = self._extract_alltrails_geo_from_html(text)
        if not coords:
            return None
        lat, lng = coords
        return f"https://www.google.com/maps/search/?api=1&query={lat},{lng}"

    def _prefer_canonical_alltrails_url(self, url: str | None, item_name: str) -> str | None:
        """Prefer verified canonical AllTrails slug over broader '/via-' variants."""
        if not url or not self._is_alltrails_trail_url(url):
            return url

        url = self._strip_alltrails_tracking(url)

        parsed = urlparse(url)
        if not self._is_noisy_alltrails_slug(url, item_name):
            return url

        parent = parsed.path.rsplit("/", 1)[0]
        name_tokens = [t for t in re.findall(r"[a-z0-9]+", (item_name or "").lower()) if t]
        if not name_tokens:
            return url
        base_slug = "-".join(name_tokens)

        candidates: list[str] = []
        if not base_slug.endswith("-trail"):
            candidates.append(f"{parsed.scheme}://{parsed.netloc}{parent}/{base_slug}-trail")
        candidates.append(f"{parsed.scheme}://{parsed.netloc}{parent}/{base_slug}")

        item_tokens = self._significant_tokens(item_name)
        for candidate in candidates:
            if candidate == url:
                continue
            if not self._alltrails_slug_matches_item(candidate, item_name):
                continue
            ok, status, text = self._fetch_page_text(candidate, timeout=8)
            if not ok:
                # A blocked or inconclusive fetch is not verification that this
                # synthesized slug actually exists. Never promote a purely
                # templated guess as canonical on unverifiable evidence alone --
                # keep the original URL, which at least came from an actual
                # search/harvest hit rather than a name-token guess.
                continue
            lower_text = (text or "").lower()
            if any(marker in lower_text for marker in ALLTRAILS_404_MARKERS):
                continue
            if item_tokens and not self._text_matches_item_tokens(lower_text, item_tokens):
                continue
            return candidate

        return url

    @staticmethod
    def _strip_alltrails_tracking(url: str) -> str:
        parsed = urlparse(url)
        if "alltrails.com" not in (parsed.netloc or "").lower():
            return url
        cleaned = parsed._replace(query="", fragment="")
        return cleaned.geturl()

    # ── Restaurants — two-pass ───────────────────────────────────────────────

    def _discover_restaurants(self, ai: dict[str, Any], dest_name: str, dest_dates: str | None = None, dest: dict[str, Any] | None = None) -> None:
        # Whole-category off switch (restaurants.enabled). Placed FIRST, above
        # the group-deferral gate, because every restaurant purchase path runs
        # below this point -- the direct batch, its prioritisation pass, and
        # the per-item fallbacks. Gating here means nothing is bought, not
        # merely that nothing renders.
        #
        # That distinction is the lesson from the trails switch, which checked
        # two of three AllTrails entry points and left the highest-volume one
        # buying 98 calls per run while hiding their output.
        if getattr(self, "_disable_restaurants", False):
            ai["dinner_recommendations"] = []
            return
        # GH #68 multi-site grouping: restaurants are the default
        # base_owned category (config.yaml multi_site_grouping) -- a
        # grouped entry's dining is deferred to its group base entirely
        # rather than independently discovered. Additive skip-gate only;
        # nothing below this changes. Clearing dinner_recommendations (like
        # the scenic_drive and en_route_stop gates do) rather than leaving
        # AI-generated names with no urls lets html_assembler.py render a
        # clean "see base" pointer instead of a card full of dead links.
        if category_deferred_to_base(
            dest,
            "restaurant",
            getattr(self, "_multi_site_base_owned_categories", DEFAULT_BASE_OWNED_CATEGORIES),
        ):
            self._log_decision(
                kind="restaurant",
                dest_name=dest_name,
                item_name="",
                reason="base_owned_category_skipped",
                message="restaurant discovery skipped for entire destination — category deferred to group base",
            )
            ai["dinner_recommendations"] = []
            return
        restaurant_source_mode = str(
            getattr(self, "_restaurant_source", DEFAULT_RESTAURANT_SOURCE) or DEFAULT_RESTAURANT_SOURCE
        )
        lodging = dest.get("lodging") if isinstance(dest, dict) else None
        lodging_location = str(
            (lodging.get("location") or lodging.get("name") or "") if isinstance(lodging, dict) else ""
        ).strip()
        restaurants = ai.get("dinner_recommendations", [])
        if not isinstance(restaurants, list):
            restaurants = []

        if restaurant_source_mode == "direct_link_batch":
            restaurants = self._prioritize_direct_batch_restaurants(restaurants, dest_name, dest_dates, lodging_location=lodging_location)
            ai["dinner_recommendations"] = restaurants

        # One URL, one restaurant. The per-item fallback introduced 2026-08-27
        # published restaurantguru.com/9-Et-Voisins-Brussels under BOTH
        # "9 et Voisins" and "Brasserie Signature" -- a search for a name the
        # batch could not place will happily return a nearby restaurant's page,
        # and nothing downstream compared one item's URL against another's.
        #
        # This is the risk the authoritative-batch design was guarding against.
        # Re-opening the fallback was right, but it has to carry the guard the
        # batch was providing implicitly.
        # Claimed AS items are processed, never pre-seeded. Seeding from the
        # URLs already attached made each restaurant collide with its own link
        # the moment it was examined -- the guard rejected exactly the items it
        # was supposed to leave alone. First item to claim a URL keeps it;
        # later items asking for the same one are refused.
        claimed_restaurant_urls: set[str] = set()

        for rest in restaurants:
            rest_name = rest.get("name", "")
            restaurant_variants = _build_restaurant_query_variants(rest_name, dest_name)

            # Direct-batch rows are the primary source for restaurant links.
            # If a verified candidate is found here, keep it and do not override
            # with separately discovered alternatives.
            if restaurant_source_mode == "direct_link_batch":
                existing_url = str(rest.get("url", "") or "").strip()
                if existing_url:
                    cleaned_existing = self._retain_discovered_url(
                        existing_url,
                        rest_name,
                        dest_name,
                        allow_alltrails=False,
                        kind="restaurant",
                    )
                    preserved_existing = cleaned_existing
                    normalized_existing = self._normalize_restaurant_url(cleaned_existing)
                    if normalized_existing:
                        preserved_existing = normalized_existing
                    elif not self._is_google_maps_candidate_url(cleaned_existing):
                        preserved_existing = ""
                    if self._direct_batch_is_authoritative() and self._is_google_maps_candidate_url(preserved_existing):
                        preserved_existing = ""
                    if preserved_existing and self._url_already_claimed(
                        preserved_existing, claimed_restaurant_urls
                    ):
                        self._log_decision(
                            kind="restaurant",
                            dest_name=dest_name,
                            item_name=rest_name,
                            reason="url_collision_rejected",
                            message="pre-attached URL already published under another restaurant at this destination",
                            url=preserved_existing,
                        )
                        preserved_existing = ""
                        rest["url"] = ""
                    if preserved_existing:
                        rest["url"] = preserved_existing
                        rest.pop("maps_url", None)
                        claimed_restaurant_urls.add(self._collision_key(preserved_existing))
                        # This shortcut (URL already attached before this loop
                        # ran) otherwise skips the row-metadata merge the
                        # fresh-lookup branch below does, silently leaving
                        # rating/votes/cuisine/price empty even when the
                        # matched row had them (dipstick55 Theme D: badges
                        # and title decoration going missing inconsistently
                        # depending on which branch handled a given item).
                        rest.update(
                            self._direct_batch_row_quality_metadata_for_url(
                                self._get_restaurant_direct_batch_rows_for_destination(
                                    dest_name, str(dest_dates or ""), lodging_location=lodging_location
                                ),
                                preserved_existing,
                            )
                        )
                        self._log_decision(
                            kind="restaurant",
                            dest_name=dest_name,
                            item_name=rest_name,
                            reason="direct_batch_existing_url_preserved",
                            message="restaurant link preserved from direct-link batch row",
                            url=preserved_existing,
                        )
                        continue
                batch_url = self._search_restaurant_from_direct_batch(rest_name, dest_name, str(dest_dates or ""), lodging_location=lodging_location)
                if batch_url:
                    if self._url_already_claimed(batch_url, claimed_restaurant_urls):
                        # The batch matched this item to a row already used for a
                        # different restaurant. Brussels published
                        # restaurantguru.com/9-Et-Voisins-Brussels under both
                        # "9 et Voisins" and "Brasserie Signature" in every run
                        # since the first -- a reader clicking the second gets
                        # the first. The batch is authoritative about which URL
                        # is right, not about giving the same one to two items.
                        self._log_decision(
                            kind="restaurant",
                            dest_name=dest_name,
                            item_name=rest_name,
                            reason="url_collision_rejected",
                            message="direct-batch URL already published under another restaurant at this destination",
                            url=batch_url,
                        )
                        rest["url"] = ""
                        rest.pop("maps_url", None)
                        continue
                    rest["url"] = batch_url
                    rest.pop("maps_url", None)
                    claimed_restaurant_urls.add(self._collision_key(batch_url))
                    rest.update(
                        self._direct_batch_row_quality_metadata_for_url(
                            self._get_restaurant_direct_batch_rows_for_destination(
                                dest_name, str(dest_dates or ""), lodging_location=lodging_location
                            ),
                            batch_url,
                        )
                    )
                    self._log_decision(
                        kind="restaurant",
                        dest_name=dest_name,
                        item_name=rest_name,
                        reason="direct_batch_accepted",
                        message="restaurant link (direct-link batch)",
                        url=batch_url,
                    )
                    continue
                if self._direct_batch_is_authoritative():
                    # The batch being authoritative means it WINS where it has an
                    # answer -- not that its silence is an answer. Dropping the
                    # item here skipped the per-item search below entirely, and
                    # the 2026-08-27 Brussels run showed what that costs: Chicon
                    # Farsi, Thaiburi, Yummy Bowl, Pasta Divina and Rotisse were
                    # all removed for "no verified URL", and all five have
                    # official sites that a single Serper query finds
                    # (chiconfarsi.com, thaiburi.eu, eatyummybowl.com,
                    # pastadivina.be, rotisse.be). The batch had offered a
                    # generic TripAdvisor city landing page for each, which was
                    # correctly rejected -- and then nothing else was tried.
                    #
                    # 77% of that destination's dining was lost to a fallback
                    # that was never attempted, at ~$0.001 a query.
                    if not self._item_fallback_when_batch_silent_enabled():
                        rest["url"] = ""
                        rest.pop("maps_url", None)
                        self._log_decision(
                            kind="restaurant",
                            dest_name=dest_name,
                            item_name=rest_name,
                            reason="direct_batch_source_locked_no_match",
                            message="restaurant link omitted; authoritative direct-link batch had no usable result",
                        )
                        continue
                    rest["url"] = ""
                    rest.pop("maps_url", None)
                    self._log_decision(
                        kind="restaurant",
                        dest_name=dest_name,
                        item_name=rest_name,
                        reason="direct_batch_silent_falling_back_to_item_search",
                        message="authoritative direct-link batch had no usable result; trying per-item search",
                    )

            ai_candidate_url = self._resolve_ai_candidate_url(
                item=rest,
                item_name=rest_name,
                dest_name=dest_name,
                allow_alltrails=False,
                trail_like=False,
                kind="restaurant",
                normalize_restaurant=True,
            )
            if ai_candidate_url and self._url_already_claimed(
                ai_candidate_url, claimed_restaurant_urls
            ):
                self._log_decision(
                    kind="restaurant",
                    dest_name=dest_name,
                    item_name=rest_name,
                    reason="url_collision_rejected",
                    message="candidate already published under another restaurant at this destination",
                    url=ai_candidate_url,
                )
                ai_candidate_url = ""
            if ai_candidate_url:
                rest["url"] = ai_candidate_url
                rest.pop("maps_url", None)
                claimed_restaurant_urls.add(self._collision_key(ai_candidate_url))
                self._log_decision(
                    kind="restaurant",
                    dest_name=dest_name,
                    item_name=rest_name,
                    reason="ai_candidate_accepted",
                    message="restaurant link (ai-candidate)",
                    url=ai_candidate_url,
                )
                continue

            # Pass 1: Google Maps
            url = self._search_first(
                restaurant_variants,
                site_filter="google.com/maps",
                item_name=rest_name,
                dest_name=dest_name,
                max_attempts=int(getattr(self, "_max_restaurant_query_attempts", 3) or 3),
            )
            # Pass 2: TripAdvisor
            if not url:
                url = self._search_first(
                    restaurant_variants,
                    site_filter="tripadvisor.com",
                    item_name=rest_name,
                    dest_name=dest_name,
                    max_attempts=int(getattr(self, "_max_restaurant_query_attempts", 3) or 3),
                )
            url = self._normalize_restaurant_url(url)
            if url and self._url_already_claimed(url, claimed_restaurant_urls):
                self._log_decision(
                    kind="restaurant",
                    dest_name=dest_name,
                    item_name=rest_name,
                    reason="url_collision_rejected",
                    message="search result already published under another restaurant at this destination",
                    url=url,
                )
                url = ""
            rest["url"] = url or ""
            rest.pop("maps_url", None)
            if url:
                claimed_restaurant_urls.add(self._collision_key(url))
            if url:
                # Populate missing metadata from the search snippet before trying page fetch.
                winner = getattr(self, "_search_winner_snippets", {}).get(url, {})
                if winner:
                    snippet_text = " ".join(filter(None, [
                        str(winner.get("snippet", "") or ""),
                        str(winner.get("name", "") or ""),
                    ]))
                    self._populate_restaurant_from_snippet(rest, snippet_text)
            if not url:
                self._log_decision(
                    kind="restaurant",
                    dest_name=dest_name,
                    item_name=rest_name,
                    reason="maps_fallback_only",
                    message="restaurant link omitted; no canonical URL found",
                    url="",
                )
            else:
                self._log_decision(
                    kind="restaurant",
                    dest_name=dest_name,
                    item_name=rest_name,
                    reason="discovery_completed",
                    message="restaurant link",
                    url=url,
                )

        # Enrich any restaurant that has a valid URL but missing metadata.
        for rest in restaurants:
            self._maybe_upgrade_tripadvisor_restaurant_link(rest, dest_name)
            self._strip_place_name_cuisine(rest, dest_name)
            self._enrich_restaurant_metadata_from_url(rest)
            self._backfill_restaurant_metadata_from_available_text(rest)

        # Apply the budget cap AGAIN, now that prices are known. The first pass
        # runs before this enrichment, so it judged items whose price_range was
        # still empty and let them through as on-tier. That is how Frankfurt
        # kept The Legacy Bar & Grill at $$$$ on a "No fine dining" itinerary:
        # at cap time it had no price at all.
        #
        # Running it twice rather than moving it: the first pass still earns its
        # place by trimming the list before the expensive per-item URL work.
        priced = self._apply_budget_cap_to_restaurants(restaurants, dest_name)
        if len(priced) != len(restaurants):
            ai["dinner_recommendations"] = priced

    # Third-party pages that stand in for a restaurant's own site. TripAdvisor
    # was the only one checked until 2026-08-27, when re-opening the per-item
    # fallback started returning food blogs and AI travel sites instead:
    # champagne-tastes.com for Rotisse (rotisse.be exists), mindtrip.ai for
    # Thaiburi (thaiburi.eu exists), restaurantguru, wanderlog, inyourpocket.
    # All verified and resolvable, all worse than the restaurant's own page.
    _THIRD_PARTY_RESTAURANT_HOSTS = (
        "tripadvisor.", "yelp.", "restaurantguru.", "wanderlog.", "mindtrip.",
        "inyourpocket.", "thefork.", "opentable.", "zomato.", "foursquare.",
        "trip.com", "timeout.", "eater.",
    )

    @staticmethod
    def _domain_matches_item_name(url: str, item_name: str) -> bool:
        """Does the URL's domain look like it BELONGS to this item?

        The discriminator a host list cannot express. "rotisse.be" contains
        "rotisse"; "champagne-tastes.com" does not, and no amount of
        enumerating blog hosts would have told them apart -- the 2026-08-27
        upgrade accepted champagne-tastes.com as Rotisse's official site, and
        tipsfromawaitress.be as Yummy Bowl's, because both cleared a
        not-on-the-list test.

        Punctuation and spacing are collapsed on both sides, so "Chicon Farsi"
        matches chiconfarsi.com and "Yummy Bowl" matches eatyummybowl.com.
        Containment, not overlap: token intersection is what once matched
        *Zion Lodge* to *Stargazing in Zion*.

        Short names are refused rather than guessed at -- a three-character
        name would match almost any domain by accident.
        """
        host = str(url or "").strip().lower()
        for prefix in ("https://", "http://"):
            if host.startswith(prefix):
                host = host[len(prefix):]
                break
        host = host.split("/", 1)[0]
        if host.startswith("www."):
            host = host[4:]
        host_key = re.sub(r"[^a-z0-9]", "", host)
        if not host_key:
            return False

        raw_name = str(item_name or "").lower()
        name_key = re.sub(r"[^a-z0-9]", "", raw_name)
        if len(name_key) < 5:
            return False
        if name_key in host_key:
            return True

        # Real domains abbreviate. "Benja Thai & Sushi" registers
        # benjathaistgeorge.com -- it drops a word and adds the town, so whole
        # name containment rejects a genuine official site. Fall back to the
        # first distinctive token, which is what a restaurant actually builds
        # its domain around.
        tokens = [tok for tok in re.split(r"[^a-z0-9]+", raw_name) if len(tok) >= 4]
        if not tokens:
            return False
        return tokens[0] in host_key

    @classmethod
    def _is_third_party_restaurant_page(cls, url: str) -> bool:
        """True for a page ABOUT the restaurant rather than the restaurant's own.

        Deliberately a host list rather than a heuristic. "Looks like a blog" is
        not decidable from a URL, and guessing wrong would discard a legitimate
        official site -- the expensive direction of the error, since the whole
        point is to end up with better links, not fewer.
        """
        lower = str(url or "").strip().lower()
        if not lower:
            return False
        return any(marker in lower for marker in cls._THIRD_PARTY_RESTAURANT_HOSTS)

    def _maybe_upgrade_tripadvisor_restaurant_link(self, rest: dict[str, Any], dest_name: str) -> None:
        """TripAdvisor should be the exception, not the default, for a named
        restaurant's primary link -- but TripAdvisor blocks automated fetches
        (403), so its page content can't be inspected for an official-site
        link the way a normal page can. Run one independent supplementary
        search for the restaurant's own site instead, and only swap the link
        when a genuinely different, non-aggregator domain is found and clears
        the normal retention checks; otherwise TripAdvisor is kept as-is.
        """
        if not bool(
            getattr(
                self,
                "_restaurant_prefer_official_site_over_tripadvisor",
                DEFAULT_RESTAURANT_PREFER_OFFICIAL_SITE_OVER_TRIPADVISOR,
            )
        ):
            return
        url = str(rest.get("url", "") or "").strip()
        if not self._is_third_party_restaurant_page(url):
            return
        rest_name = str(rest.get("name", "") or "").strip()
        if not rest_name:
            return

        variants = [
            f'"{rest_name}" {dest_name} official website',
            f'"{rest_name}" {dest_name} restaurant menu',
        ]
        candidate = self._search_first(
            variants,
            item_name=rest_name,
            dest_name=dest_name,
            allow_alltrails=False,
        )
        if not candidate:
            return
        lower_candidate = candidate.lower()
        aggregator_markers = self._THIRD_PARTY_RESTAURANT_HOSTS + (
            "facebook.",
            "instagram.",
            "google.com/maps",
            "google.com/search",
        )
        if any(marker in lower_candidate for marker in aggregator_markers):
            return
        # An "upgrade" must actually be the restaurant's own site. Without this
        # the search's first non-listed result wins, which is how a food blog
        # replaced a TripAdvisor page and was logged as an upgrade. Keeping the
        # existing link is the better failure: it is at least ABOUT the right
        # restaurant.
        if not self._domain_matches_item_name(candidate, rest_name):
            self._log_decision(
                kind="restaurant",
                dest_name=dest_name,
                item_name=rest_name,
                reason="official_site_upgrade_rejected_name_mismatch",
                message="candidate domain does not correspond to the restaurant name; keeping existing link",
                url=candidate,
            )
            return
        cleaned = self._retain_discovered_url(
            candidate,
            rest_name,
            dest_name,
            allow_alltrails=False,
            kind="restaurant",
        )
        if not cleaned:
            return
        rest["url"] = cleaned
        rest.pop("maps_url", None)
        self._log_decision(
            kind="restaurant",
            dest_name=dest_name,
            item_name=rest_name,
            reason="tripadvisor_upgraded_to_official_site",
            message="restaurant link upgraded from TripAdvisor to official site",
            url=cleaned,
        )

    def _enrich_restaurant_metadata_from_url(self, rest: dict[str, Any]) -> None:
        url = str(rest.get("url", "") or "").strip()
        if not url or any(url.lower().startswith(p) for p in SAFE_FALLBACK_URL_PREFIXES):
            return
        needs_desc = not str(rest.get("description", "") or "").strip()
        needs_cuisine = not str(rest.get("cuisine", "") or "").strip()
        needs_price = not str(rest.get("price_range", "") or rest.get("price", "") or "").strip()
        if not (needs_desc or needs_cuisine or needs_price):
            return
        ok, _status, html = self._fetch_page_text(url, timeout=8)
        if not ok or not html:
            return
        meta = self._extract_restaurant_meta_from_html(html)
        if needs_desc and meta.get("description"):
            rest["description"] = meta["description"]
            logger.info("  Enriched description for '%s' from page", rest.get("name", ""))
        if needs_cuisine and meta.get("cuisine"):
            rest["cuisine"] = meta["cuisine"]
            logger.info("  Enriched cuisine for '%s' from page", rest.get("name", ""))
        if needs_price and meta.get("price_range"):
            rest["price_range"] = meta["price_range"]
            logger.info("  Enriched price_range for '%s' from page", rest.get("name", ""))
        still_needs_cuisine = not str(rest.get("cuisine", "") or "").strip()
        still_needs_price = not str(rest.get("price_range", "") or rest.get("price", "") or "").strip()
        if still_needs_cuisine or still_needs_price:
            title_match = re.search(r"<title[^>]*>(.*?)</title>", html, flags=re.IGNORECASE | re.DOTALL)
            title_text = re.sub(r"\s+", " ", str(title_match.group(1) if title_match else "")).strip()
            text_blob = " ".join(
                part
                for part in (
                    title_text,
                    str(meta.get("description", "") or "").strip(),
                    str(rest.get("description", "") or "").strip(),
                    str(rest.get("name", "") or "").strip(),
                )
                if part
            )
            inferred = self._infer_restaurant_metadata_from_text_and_url(text_blob, url)
            if still_needs_cuisine and inferred.get("cuisine"):
                rest["cuisine"] = str(inferred.get("cuisine") or "").strip()
            if still_needs_price and inferred.get("price_range"):
                rest["price_range"] = str(inferred.get("price_range") or "").strip()

    def _populate_restaurant_from_snippet(self, rest: dict[str, Any], snippet: str) -> None:
        """Populate missing restaurant fields from a search-result snippet (plain text)."""
        text = str(snippet or "").strip()
        if not text:
            return
        name = str(rest.get("name", "") or "").strip()
        if not str(rest.get("price_range", "") or "").strip():
            # Match standalone $-strings surrounded by spaces, dots, or bullets
            m = re.search(r'(?<![^\s·,])(\${1,4})(?![^\s·,\d])', text)
            if m:
                rest["price_range"] = m.group(1)
        if not str(rest.get("cuisine", "") or "").strip():
            # Cuisine words often appear between · separators in search result titles/snippets
            # e.g. "Painted Pony · $$$ · Southwestern · American"
            parts = [p.strip() for p in re.split(r"·|,", text)]
            for part in parts:
                # Skip parts that are prices, ratings, or empty
                if not part or re.fullmatch(r'[\$\d\.\s★*]+', part):
                    continue
                if len(part.split()) <= 3 and part[0].isupper() and "review" not in part.lower():
                    rest["cuisine"] = part
                    break
        if not str(rest.get("description", "") or "").strip():
            # Use the snippet itself as description if it's meaningful
            if len(text) > 25 and name.lower() not in text[:25].lower():
                rest["description"] = text[:300]

    @classmethod
    def _backfill_restaurant_metadata_from_available_text(cls, rest: dict[str, Any]) -> None:
        """Infer missing cuisine/price fields from available restaurant text and URLs."""
        if not isinstance(rest, dict):
            return

        needs_cuisine = not str(rest.get("cuisine", "") or "").strip()
        needs_price = not str(rest.get("price_range", "") or rest.get("price", "") or "").strip()
        if not (needs_cuisine or needs_price):
            return

        text_blob = " ".join(
            part
            for part in (
                str(rest.get("name", "") or "").strip(),
                str(rest.get("description", "") or "").strip(),
                str(rest.get("practical_note", "") or "").strip(),
            )
            if part
        )
        candidate_url = str(rest.get("url", "") or rest.get("maps_url", "") or "").strip()
        inferred = cls._infer_restaurant_metadata_from_text_and_url(text_blob, candidate_url)

        if needs_cuisine and inferred.get("cuisine"):
            rest["cuisine"] = str(inferred.get("cuisine") or "").strip()
        if needs_price and inferred.get("price_range"):
            rest["price_range"] = str(inferred.get("price_range") or "").strip()

    #: Cuisine values that are really a place, not a food style. The harvest
    #: returned cuisine="Frankfurt" for THE ROOF and African Queen, which reads
    #: as a cuisine badge saying the name of the city the reader is already in.
    #: Place-type nouns. "Wenceslas Square" cleared every other check -- two
    #: alphabetic words, no digits, no street suffix -- so a landmark name still
    #: reached the badge. "market" is deliberately absent: a market hall is a
    #: real dining category on a low-cost brief.
    _CUISINE_PLACE_STOPWORDS = (
        "district", "quarter", "centre", "center", "old town", "square",
        "bridge", "castle", "cathedral", "station", "tower", "palace",
    )

    #: Street-type words in the languages this generator has produced output for.
    #: "Pflugstrasse 11" and "Mehringdamm 32" both reached a cuisine badge.
    _CUISINE_STREET_WORDS = (
        "strasse", "straße", "str.", "damm", "platz", "gasse", "allee", "weg",
        "street", "road", "avenue", "lane", "boulevard", "plein", "straat",
        "rue ", "namesti", "náměstí",
    )

    #: Fragments that mean a scrape leaked into the field rather than a cuisine.
    _CUISINE_SCRAPE_MARKERS = ("photo", "review", "menu", "price", "…", "...", "|", "http")

    @classmethod
    def _is_plausible_cuisine(cls, value: str, dest_name: str = "") -> bool:
        """Does this read as a FOOD STYLE, rather than whatever text was nearby?

        Inverted from the earlier version, which blocklisted place names and so
        cleared cuisine="Frankfurt" while passing "Pflugstrasse 11",
        "Mehringdamm 32" and "Photos & ..." straight to the badge. Screening
        against a list of things a cuisine is not requires knowing them all in
        advance; this asks what a cuisine looks like instead.

        A cuisine is a short alphabetic phrase: "Thai", "Modern European",
        "Vietnamese". It carries no digits, no street-type word, no punctuation
        from a scraped page, and is not the name of the place the reader is in.
        """
        text = str(value or "").strip()
        if not text or len(text) > 28:
            return False
        lowered = text.lower()

        if any(ch.isdigit() for ch in text):
            return False                      # addresses, "4.5/5", "Top 10"
        if any(marker in lowered for marker in cls._CUISINE_SCRAPE_MARKERS):
            return False
        if any(word in lowered for word in cls._CUISINE_STREET_WORDS):
            return False
        if len(text.split()) > 3:
            return False                      # a phrase this long is prose
        if not re.fullmatch(r"[A-Za-zÀ-ÿ\s&'/-]+", text):
            return False

        dest_tokens = {
            tok for tok in re.split(r"[^a-z]+", str(dest_name or "").lower()) if len(tok) > 3
        }
        if lowered in dest_tokens:
            return False                      # cuisine="Frankfurt" in Frankfurt
        if any(word in lowered for word in cls._CUISINE_PLACE_STOPWORDS):
            return False
        return True

    @classmethod
    def _strip_place_name_cuisine(cls, rest: dict[str, Any], dest_name: str) -> None:
        """Blank a cuisine field that is not plausibly a cuisine.

        Cleared rather than corrected: an empty badge is honest, whereas
        guessing a cuisine from the name would invent a fact about the
        restaurant. _backfill_restaurant_metadata_from_available_text may still
        infer one legitimately from the page text afterwards.
        """
        if not isinstance(rest, dict):
            return
        cuisine = str(rest.get("cuisine", "") or "").strip()
        if cuisine and not cls._is_plausible_cuisine(cuisine, dest_name):
            logger.info("Cleared implausible cuisine %r for %r", cuisine[:40], rest.get("name", ""))
            rest["cuisine"] = ""

    @staticmethod
    def _extract_restaurant_meta_from_html(html: str) -> dict[str, str]:
        """Best-effort extraction of description, cuisine, and price_range from raw HTML."""
        result: dict[str, str] = {}
        text = str(html or "")
        if not text:
            return result

        # JSON-LD structured data (most reliable source)
        for ld_match in re.finditer(
            r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
            text,
            re.DOTALL | re.IGNORECASE,
        ):
            try:
                data = json.loads(ld_match.group(1))
                if not isinstance(data, dict):
                    continue
                if not result.get("cuisine"):
                    raw = data.get("servesCuisine", "")
                    if isinstance(raw, list):
                        raw = ", ".join(str(c) for c in raw[:2])
                    cuisine = str(raw or "").strip()
                    if cuisine:
                        result["cuisine"] = cuisine
                if not result.get("price_range"):
                    pr = str(data.get("priceRange", "") or "").strip()
                    if pr:
                        result["price_range"] = pr
                if not result.get("description"):
                    desc = str(data.get("description", "") or "").strip()
                    if len(desc) > 20:
                        result["description"] = desc[:300]
            except (json.JSONDecodeError, KeyError, TypeError):
                pass
            if result.get("cuisine") and result.get("price_range") and result.get("description"):
                break

        # OG / meta description fallback
        if not result.get("description"):
            for pattern in (
                r'<meta[^>]+(?:name|property)=["\'](?:description|og:description)["\'][^>]+content=["\']([^"\']{20,})["\']',
                r'<meta[^>]+content=["\']([^"\']{20,})["\'][^>]+(?:name|property)=["\'](?:description|og:description)["\']',
            ):
                m = re.search(pattern, text, re.IGNORECASE)
                if m:
                    result["description"] = m.group(1).strip()[:300]
                    break

        # Price range pattern in raw text as last resort
        if not result.get("price_range"):
            m = re.search(r'(?:price\s*range|price)[^\$\n]{0,20}(\${1,4})(?!\d)', text, re.IGNORECASE)
            if m:
                result["price_range"] = m.group(1)

        return result

    @staticmethod
    def _is_google_maps_domain(netloc: str) -> bool:
        host = (netloc or "").strip().lower()
        if not host:
            return False
        host = host.split(":", 1)[0]
        return (
            host == "maps.app.goo.gl"
            or host == "maps.google.com"
            or host.endswith(".google.com")
            or host.endswith(".google.co")
        )

    @classmethod
    def _is_google_maps_candidate_url(cls, url: str | None) -> bool:
        parsed = urlparse(str(url or "").strip())
        if not cls._is_google_maps_domain(parsed.netloc):
            return False
        host_l = (parsed.netloc or "").lower()
        path_l = (parsed.path or "").lower()
        return (
            "maps" in path_l
            or path_l.startswith("/place/")
            or ("maps.google.com" in host_l and bool(parsed.query))
            or "maps.app.goo.gl" in (parsed.netloc or "").lower()
        )

    def _resolve_google_maps_final_url(self, url: str) -> str:
        candidate = str(url or "").strip()
        if not candidate:
            return ""

        cache = getattr(self, "_maps_url_resolution_cache", None)
        if isinstance(cache, dict) and candidate in cache:
            return cache[candidate]

        fetch_cache = getattr(self, "_fetch_final_url_cache", None)
        if isinstance(fetch_cache, dict):
            cached_final = str(fetch_cache.get(candidate, "") or "").strip()
            # Deliberately still keyed on a DIFFERING target rather than on
            # `link_redirect_state`. A recorded non-redirect is now legible
            # here, and short-circuiting on it would be defensible -- but it
            # would change how many requests this resolver makes, and this
            # change is about representing the third state, not about spending
            # or saving requests. Left exactly as it behaved before.
            if cached_final and cached_final != candidate:
                if isinstance(cache, dict):
                    cache[candidate] = cached_final
                return cached_final

        try:
            resp = self._url_validator.session.get(candidate, timeout=8)
            final_url = str(getattr(resp, "url", None) or candidate).strip()
            # Its own response, so this is an observation either way.
            self._record_link_redirect(candidate, final_url)
        except Exception:
            # No response, so nothing was observed: leave the ledger alone
            # rather than record `candidate` as its own destination. The
            # returned value still falls back to the candidate, as before.
            final_url = candidate

        if isinstance(cache, dict):
            cache[candidate] = final_url
        return final_url

    def _normalize_restaurant_url(self, url: str | None) -> str:
        if not url:
            return ""
        normalized = str(url).strip()
        lower = normalized.lower()
        if "google.com/maps/dir/" in lower or "maps.google.com/maps/dir/" in lower:
            return ""
        parsed = urlparse(normalized)

        if self._is_google_maps_candidate_url(normalized):
            resolved = self._resolve_google_maps_final_url(normalized)
            parsed = urlparse(resolved)
            path_l = (parsed.path or "").lower()
            query_l = (parsed.query or "").lower()

            # Query-style map/search pages remain ambiguous and should not be canonical.
            if path_l.startswith("/maps/search") or path_l.startswith("/maps/dir"):
                return ""
            if path_l.startswith("/maps/@"):
                return ""

            # Deterministic place-target variants are acceptable.
            if path_l.startswith("/maps/place/"):
                if "/data=" in path_l or "cid=" in query_l or "ftid=" in query_l:
                    cleaned = resolved.split("#", 1)[0]
                    if self._looks_synthetic_google_maps_place_url(cleaned):
                        return ""
                    return cleaned
                return ""
            if path_l.startswith("/place/"):
                cleaned = resolved.split("#", 1)[0]
                if self._looks_synthetic_google_maps_place_url(cleaned):
                    return ""
                return cleaned
            if path_l.rstrip("/") == "/maps":
                if "cid=" in query_l or "ftid=" in query_l or "place_id:" in query_l:
                    cleaned = resolved.split("#", 1)[0]
                    if self._looks_synthetic_google_maps_place_url(cleaned):
                        return ""
                    return cleaned
                return ""

            # Bare maps domains or generic query-style endpoints are ambiguous.
            if "q=" in query_l and "place_id:" not in query_l:
                return ""
            if path_l in {"", "/"}:
                return ""

            return resolved.split("#", 1)[0]

        if "maps.google.com" in parsed.netloc.lower() and parsed.query:
            query_l = parsed.query.lower()
            if "q=" in query_l:
                return ""
        return normalized

    @staticmethod
    def _looks_synthetic_google_maps_place_url(url: str) -> bool:
        lower = str(url or "").lower()
        if not lower:
            return False
        placeholder_tokens = (
            "0x1234567890abcdef",
            "0xabcdef",
            "1td_abcde",
            "1tc_xyz",
            "e0e0e0e0e0e0e0e0",
            "8e8e8e8e8e8e8e8e",
            "5e5e5e5e5e5e5e5e",
        )
        if any(token in lower for token in placeholder_tokens):
            return True
        if "/g/1tc_" in lower or "/g/1td_" in lower:
            return True
        if re.search(r"0x(?:([0-9a-f]{2})\\1{5,}|([0-9a-f]{4})\\2{2,})", lower):
            return True
        if ":0x0" in lower:
            return True
        if re.search(r"1s0x[0-9a-f]{8,}:0x0\b", lower):
            return True
        parsed = urlparse(str(url or ""))
        cid = ""
        try:
            cid = parse_qs(parsed.query).get("cid", [""])[0].strip()
        except Exception:
            cid = ""
        # Real Google CID values are long decimal identifiers; short numeric
        # placeholders (for example 1234567890) are synthetic and unreliable.
        if cid:
            if not cid.isdigit():
                return True
            if len(cid) < 16 or len(cid) > 20:
                return True
        return False

    @staticmethod
    def _has_unescaped_whitespace(url: str | None) -> bool:
        return any(ch.isspace() for ch in str(url or ""))

    @classmethod
    def _is_deterministic_google_maps_place_url(cls, url: str | None) -> bool:
        parsed = urlparse(str(url or "").strip())
        if not cls._is_google_maps_domain(parsed.netloc):
            return False
        path_l = (parsed.path or "").lower()
        query_l = (parsed.query or "").lower()

        if path_l.startswith("/maps/place/"):
            return ("/data=" in path_l) or ("cid=" in query_l) or ("ftid=" in query_l)
        if path_l.startswith("/place/"):
            return True
        if path_l.rstrip("/") == "/maps":
            return ("cid=" in query_l) or ("ftid=" in query_l) or ("place_id:" in query_l)
        return False

    def _matches_site_filter(self, candidate_url: str, site_filter: str | None) -> bool:
        if not site_filter:
            return True
        if site_filter == "google.com/maps":
            return self._is_google_maps_candidate_url(candidate_url)
        return site_filter in candidate_url

    @classmethod
    def _restaurant_maps_query_text(cls, rest_name: str, dest_name: str) -> str:
        name = str(rest_name or "").strip()
        dest = str(dest_name or "").strip()
        query = cls._maps_fallback_query_text(name, dest)

        # Keep destination context for restaurant lookups unless the candidate
        # name is already location-qualified (e.g., "Tropic Junction").
        if name and dest and dest.lower() not in query.lower() and not cls._looks_location_qualified(name):
            query = f"{name} {dest}".strip()

        lowered = query.lower()
        if not any(term in lowered for term in ("restaurant", "cafe", "diner", "bistro", "grill", "saloon")):
            query = f"{query} restaurant".strip()

        return query

    def _resolve_ai_candidate_url(
        self,
        *,
        item: dict[str, Any],
        item_name: str,
        dest_name: str,
        allow_alltrails: bool,
        trail_like: bool,
        kind: str,
        normalize_restaurant: bool = False,
    ) -> str | None:
        candidates = item.get("url_candidates", []) if isinstance(item, dict) else []
        if not isinstance(candidates, list):
            return None
        parsed_candidates = [str(c or "").strip() for c in candidates if str(c or "").strip()]
        if not parsed_candidates:
            return None

        # For trail-like attractions, evaluate AllTrails candidates first.
        if trail_like:
            parsed_candidates.sort(key=lambda u: 0 if self._is_alltrails_trail_url(u) else 1)

        for candidate in parsed_candidates:
            candidate_url = candidate
            lower = candidate_url.lower()
            if lower.startswith("www."):
                candidate_url = "https://" + candidate_url
            if not candidate_url.lower().startswith(("http://", "https://")):
                continue
            if self._is_alltrails_trail_url(candidate_url):
                candidate_url = self._prefer_canonical_alltrails_url(candidate_url, item_name) or ""
                if trail_like and bool(getattr(self, "_enable_filtered_alltrails_selection", DEFAULT_ENABLE_FILTERED_ALLTRAILS_SELECTION)):
                    filtered_selected = self._get_filtered_alltrails_selection(item_name=item_name, dest_name=dest_name)
                    if filtered_selected and not self._same_alltrails_trail(candidate_url, filtered_selected):
                        self._log_decision(
                            kind=kind,
                            dest_name=dest_name,
                            item_name=item_name,
                            reason="alltrails_mismatched_filtered_selection",
                            message=f"{kind} alltrails candidate mismatched filtered selection",
                            url=candidate_url,
                            level=logging.WARNING,
                        )
                        continue
            if normalize_restaurant:
                candidate_url = self._normalize_restaurant_url(candidate_url)
            cleaned = self._retain_discovered_url(
                candidate_url,
                item_name,
                dest_name,
                allow_alltrails=allow_alltrails,
                kind=kind,
            )
            if cleaned and trail_like and self._is_alltrails_trail_url(cleaned):
                if not self._meets_alltrails_publish_confidence(cleaned, item_name, dest_name):
                    self._log_decision(
                        kind=kind,
                        dest_name=dest_name,
                        item_name=item_name,
                        reason="ai_candidate_downgraded_confidence_gate",
                        message=f"{kind} ai-candidate downgraded by confidence gate",
                        url=cleaned,
                    )
                    cleaned = ""
            if cleaned:
                self._record_url_recommendation_source(cleaned, "ai_candidate")
                return cleaned

            self._log_decision(
                kind=kind,
                dest_name=dest_name,
                item_name=item_name,
                reason="ai_candidate_rejected",
                message=f"{kind} ai-candidate rejected",
                url=candidate_url,
            )

        return None

    @staticmethod
    def _direct_batch_row_name(row: dict[str, Any] | None) -> str:
        if not row:
            return ""
        return str(row.get("name", "") or row.get("title", "") or "").strip()

    @staticmethod
    def _primary_list_target_count(current_count: int, fallback_count: int, available_count: int) -> int:
        # The configured item count is a floor, not a cap that gets discarded the
        # moment the AI-generated list happens to be non-empty. Locking the target
        # to whatever (often sparse) count the AI produced silently discarded
        # richer, higher-quality batch candidates even when the config explicitly
        # asked for more (e.g. restaurant_direct_batch_item_count: 8).
        if fallback_count > 0:
            configured_target = min(max(1, fallback_count), max(1, available_count))
            return max(current_count, configured_target)
        if current_count > 0:
            return current_count
        return max(0, available_count)

    def _build_primary_items_from_direct_batch(
        self,
        *,
        rows: list[dict[str, Any]],
        existing_items: list[dict[str, Any]],
        target_count: int,
        fallback_description: str,
        default_type: str | None = None,
        dest_name: str = "",
    ) -> list[dict[str, Any]]:
        if target_count <= 0:
            return [dict(item) for item in existing_items if isinstance(item, dict)]

        normalized_existing = [dict(item) for item in existing_items if isinstance(item, dict)]
        used_existing: set[int] = set()
        merged: list[dict[str, Any]] = []
        seen_names: set[str] = set()

        for row in rows:
            row_name = self._direct_batch_row_name(row)
            if not row_name:
                continue
            key = row_name.lower()
            if key in seen_names:
                continue
            row_url = str(row.get("url", "") or "").strip()
            if self._is_generic_listing_title(row_name) or (
                row_url and self._is_obviously_generic_url(row_url.lower())
            ):
                # A listing/search-result row (title and/or URL identify a
                # generic page like a TripAdvisor "10 Best Restaurants" list,
                # not a specific place) must never be used to synthesize a new
                # item, nor overwrite an existing item's name/url below.
                continue

            matched_idx: int | None = None
            for idx, item in enumerate(normalized_existing):
                if idx in used_existing:
                    continue
                item_name = str(item.get("name", "") or item.get("title", "") or "").strip()
                if item_name and self._direct_batch_row_matches_item(row, item_name, dest_name):
                    matched_idx = idx
                    break

            if matched_idx is not None:
                merged_item = dict(normalized_existing[matched_idx])
                used_existing.add(matched_idx)
            else:
                merged_item = {}

            merged_item["name"] = row_name
            if row_url and not str(merged_item.get("url", "") or "").strip():
                merged_item["url"] = row_url
            for key in (
                "detour_distance_miles",
                "detour_time_minutes",
                "practical_note",
                "cuisine",
                "price_range",
                "price",
                "reserve_recommended",
                "rating",
                "raw_rating",
                "votes",
            ):
                if key in row and row.get(key) not in (None, ""):
                    merged_item[key] = row.get(key)
            if default_type and not str(merged_item.get("type", "") or "").strip():
                merged_item["type"] = default_type
            row_desc = self._sanitize_direct_batch_description_text(
                str(row.get("description", "") or row.get("practical_note", "") or row.get("snippet", "") or "")
            )
            # The "snippet" fallback above exists for rows whose description/
            # practical_note were never populated but whose raw snippet still
            # carries real prose. When description/practical_note are empty
            # because _direct_batch_rows_from_html already determined there was
            # nothing but the item's own name plus rating/price/cuisine
            # metadata (_is_metadata_only_residual_text), falling back to the
            # snippet re-introduces that exact metadata as a fake "description"
            # (e.g. row_desc="Book Club Bistro 4.9/5 $$ Bistro" for a row whose
            # name is "Book Club Bistro" -- dipstick58: after downstream
            # rating/price stripping this rendered as "Book Club Bistro
            # Bistro", the cuisine word glued onto the end of the name). Apply
            # the same metadata-only guard here so a name-only snippet never
            # masquerades as a real description.
            if row_desc and self._is_metadata_only_residual_text(row_desc, name=row_name):
                row_desc = ""
            existing_desc = str(merged_item.get("description", "") or "").strip()
            if row_desc and row_desc.lower() != row_name.lower():
                # The row we just matched is the traveler's actual verified,
                # source-linked harvest data for this item -- every other field
                # merged above (rating, votes, price_range, cuisine,
                # detour_distance_miles/detour_time_minutes, and practical_note,
                # which is populated from this exact same underlying text) is
                # already trusted unconditionally from the row. Description must
                # not be the one field where a stale, unverified pre-harvest AI
                # guess is allowed to keep overriding it: real dipstick62 bug --
                # "Little Wild Horse Canyon Trailhead" (a slot-canyon trailhead)
                # rendered with a pre-existing hallucinated description about
                # "sweeping views of the Colorado River," and "Wedge Overlook
                # (San Rafael Swell)" rendered describing "Castleton Tower" (a
                # real but ~100-mile-distant, unrelated Moab-area landmark) --
                # while the correct harvested text ("slot canyon hiking access",
                # "dramatic canyon rim views") silently landed in practical_note
                # instead, since only practical_note was ever unconditionally
                # trusted from the row. Prefer the harvested description here too.
                merged_item["description"] = row_desc
            elif not existing_desc:
                merged_item["description"] = fallback_description

            merged.append(merged_item)
            seen_names.add(key)
            if len(merged) >= target_count:
                break

        for idx, item in enumerate(normalized_existing):
            if len(merged) >= target_count:
                break
            if idx in used_existing:
                continue
            item_name = str(item.get("name", "") or item.get("title", "") or "").strip()
            if item_name and item_name.lower() in seen_names:
                continue
            merged.append(dict(item))

        return merged

    def _prioritize_direct_batch_restaurants(
        self,
        restaurants: list[dict[str, Any]],
        dest_name: str,
        dest_dates: str | None,
        lodging_location: str = "",
    ) -> list[dict[str, Any]]:
        try:
            rows = self._get_restaurant_direct_batch_rows_for_destination(dest_name, str(dest_dates or ""), lodging_location=lodging_location)
        except AttributeError:
            return restaurants
        if not rows:
            return restaurants
        target_count = self._primary_list_target_count(
            len(restaurants),
            int(getattr(self, "_restaurant_direct_batch_item_count", DEFAULT_RESTAURANT_DIRECT_BATCH_ITEM_COUNT) or DEFAULT_RESTAURANT_DIRECT_BATCH_ITEM_COUNT),
            len(rows),
        )
        merged = self._build_primary_items_from_direct_batch(
            rows=rows,
            existing_items=restaurants,
            target_count=target_count,
            fallback_description="Locally surfaced dinner option.",
            dest_name=dest_name,
        )
        merged = self._backfill_restaurant_metadata_from_existing(merged, restaurants)
        return self._apply_budget_cap_to_restaurants(merged, dest_name)

    #: Below this a destination's dining section stops being useful. The cap
    #: will admit off-tier options to reach it rather than publish a near-empty
    #: list -- correctly-priced and absent is not better than present and one
    #: tier high.
    _MIN_RESTAURANTS_PER_DESTINATION = 5

    #: On an explicit low-cost brief, off-tier options are admitted only to keep
    #: a destination from rendering almost nothing. Two is the point at which a
    #: dining section stops looking broken; anything above that should be filled
    #: with places that actually match the brief, or not at all.
    _LOW_BUDGET_BACKFILL_FLOOR = 2

    def _apply_budget_cap_to_restaurants(
        self, restaurants: list[dict[str, Any]], dest_name: str
    ) -> list[dict[str, Any]]:
        """Re-apply the trip's budget preference after the batch merge.

        Mirrors AIContentGenerator._normalize_restaurants: at most one splurge
        on a low-budget trip, at most one casual on a high-budget one. Applied
        again here because the batch supplies items that never passed through
        the first filter.
        """
        budget_text = re.sub(r"[-_]+", " ", str(getattr(self, "_trip_budget", "") or "").lower())
        if not budget_text.strip():
            return restaurants
        low = any(
            k in budget_text
            for k in ("budget", "cheap", "economy", "economical", "value", "frugal",
                      "low cost", "lowcost", "inexpensive", "affordable", "modest",
                      "shoestring", "backpack")
        )
        high = any(
            k in budget_text
            for k in ("luxury", "premium", "high end", "splurge", "upscale", "fine dining")
        )
        if "no fine dining" in budget_text or "not fine dining" in budget_text:
            high, low = False, True
        if not (low or high):
            return restaurants

        off_tier = {"$$$", "$$$$"} if low else {"$", "$$"}
        # Keeping the FIRST off-tier item and dropping the rest produced the
        # worst of both outcomes on 2026-08-27: Amsterdam ended with two
        # restaurants, one of them $$$$, and Frankfurt with a single $$$$.
        # The list arrives expensive-first, so "first" meant "most expensive",
        # and dropping six of eight left almost nothing on the page.
        #
        # Instead: keep every on-tier item, then backfill from the off-tier
        # ones CHEAPEST-first until the destination has a usable number. A
        # thin section of correctly-priced places is not better than a
        # reasonable section that leans the right way.
        # Consult Places where our own price is missing or looks wrong for the
        # brief. Used as a FILTER only -- the verdict decides whether an item
        # survives, and no Places field is published. See
        # docs/design/places-for-restaurants.md for why that boundary matters.
        #
        # Spent narrowly: one call per ambiguous item, not per restaurant. The
        # destination-wide sweep is too coarse to help -- a 40-place window
        # missed Horvath, Comme Chez Soi and Madami alike -- but a targeted
        # lookup answered 11 of 12 correctly, rejecting every Michelin entry.
        places = getattr(self, "_places_filter", None)
        if low and places is not None and places.available:
            rescued, rejected = [], []
            for item in restaurants:
                tier = str((item or {}).get("price_range", "") or (item or {}).get("price", "") or "").strip()
                if tier in ("$", "$$"):
                    continue                      # already on-brief; do not spend a call
                name = str((item or {}).get("name", "") or "").strip()
                if not name:
                    continue
                verdict = places.verdict_precise(name, dest_name)
                if verdict == "too_expensive":
                    item["_places_reject"] = True
                    rejected.append(name)
                elif verdict == "confirmed_affordable":
                    # Keep it even though our own price said otherwise, or said
                    # nothing. Places is the better authority on price.
                    item["_places_affordable"] = True
                    rescued.append(name)
            if rejected:
                logger.info(
                    "Places rejected %d too-expensive restaurant(s) at %s: %s",
                    len(rejected), dest_name, ", ".join(r[:26] for r in rejected[:6]),
                )
            if rescued:
                logger.info(
                    "Places confirmed %d affordable restaurant(s) at %s: %s",
                    len(rescued), dest_name, ", ".join(r[:26] for r in rescued[:6]),
                )
            restaurants = [i for i in restaurants if not (i or {}).get("_places_reject")]

        rank = {"$": 0, "$$": 1, "$$$": 2, "$$$$": 3}
        def _tier(item: Any) -> str:
            return str((item or {}).get("price_range", "") or (item or {}).get("price", "") or "").strip()

        def _on_brief(item: Any) -> bool:
            # A Places-confirmed item is on-brief whatever our own price label
            # says, which is the point of asking: our label was frequently
            # absent, and absent items were bypassing this cap entirely.
            return bool((item or {}).get("_places_affordable")) or _tier(item) not in off_tier

        on_tier = [i for i in restaurants if _on_brief(i)]
        off = [i for i in restaurants if not _on_brief(i)]
        off.sort(key=lambda i: rank.get(_tier(i), 99), reverse=not low)
        # The top tier is never a valid backfill for a low-cost brief. Filling
        # a shortfall admitted Comme Chez Soi (2 Michelin stars) to Brussels and
        # De Silveren Spiegel to Amsterdam on a manifest that says "No fine
        # dining". A shorter section is the correct answer; $$$ can still fill.
        if low:
            off = [i for i in off if _tier(i) != "$$$$"]

        # Backfill ONLY to cover a shortfall. An earlier version always kept one
        # off-tier item, mirroring the "span two price tiers" rule -- but that
        # rule exists for variety on an unstated budget, and a brief saying "No
        # fine dining" has stated one. With enough correctly-priced options,
        # nothing off-tier is admitted.
        # Backfill to a MINIMUM, not to a comfortable count. Filling toward five
        # published three Michelin restaurants for Berlin -- Horvath, Nobelhart &
        # Schmutzig, Cookies Cream -- on a "No fine dining" brief, because $$$
        # was allowed to make up the numbers.
        #
        # The instruction to exclude fine dining is in the batch prompt and is
        # not reliably honoured, so the cap is the only enforceable control. It
        # now admits off-tier options only to avoid a near-empty section, which
        # is a genuinely worse outcome than a short one.
        floor = self._MIN_RESTAURANTS_PER_DESTINATION if not low else self._LOW_BUDGET_BACKFILL_FLOOR
        shortfall = max(0, floor - len(on_tier))
        keep_off = off[:shortfall] if off else []
        dropped = [str((i or {}).get("name", "") or "") for i in off[len(keep_off):]]

        kept = [i for i in restaurants if i in on_tier or i in keep_off]
        if dropped:
            logger.info(
                "Budget cap dropped %d off-tier restaurant(s) at %s: %s",
                len(dropped), dest_name, ", ".join(d[:28] for d in dropped[:6]),
            )
        return kept

    @staticmethod
    def _infer_destination_day_count(dates: str) -> int:
        """Days at a destination, uncapped, for batch sizing.

        Delegates to date_span.day_count. See that module for why the regex
        this replaced returned 1 for a stay spanning a month boundary.
        """
        from generator.date_span import day_count

        return day_count(dates)
    def _prioritize_direct_batch_attractions(
        self,
        attractions: list[dict[str, Any]],
        dest_name: str,
        dest_dates: str | None,
        seed_names: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        try:
            rows = self._get_attraction_direct_batch_rows_for_destination(
                dest_name, str(dest_dates or ""), seed_names=seed_names
            )
        except AttributeError:
            return attractions
        if not rows:
            return attractions

        # Existing AI-generated attractions (including seeds) are never evicted --
        # unlike restaurants/en-route stops, attractions carry user-requested seed
        # anchors that must survive regardless of batch richness. This function
        # only ever adds new, distinct candidates on top.
        existing = [dict(item) for item in attractions if isinstance(item, dict)]
        existing_keys = {
            re.sub(r"[^a-z0-9]+", " ", str(item.get("name", "") or "").lower()).strip()
            for item in existing
        }

        day_count = self._infer_destination_day_count(dest_dates or "")
        items_per_day = int(
            getattr(self, "_attraction_direct_batch_items_per_day", DEFAULT_ATTRACTION_DIRECT_BATCH_ITEMS_PER_DAY)
            or DEFAULT_ATTRACTION_DIRECT_BATCH_ITEMS_PER_DAY
        )
        target_total = max(len(existing), items_per_day * day_count)
        additional_slots = max(0, target_total - len(existing))
        if additional_slots <= 0:
            return attractions

        candidates: list[tuple[float, int, str, dict[str, Any]]] = []
        for order, row in enumerate(rows):
            row_name = self._direct_batch_row_name(row)
            if not row_name:
                continue
            key = re.sub(r"[^a-z0-9]+", " ", row_name.lower()).strip()
            if not key or key in existing_keys:
                continue
            if self._candidate_mentions_conflicting_destination(row, dest_name):
                continue
            # Defensive: the harvest prompt excludes hikes, but guard against a
            # trail-like row slipping through so it doesn't duplicate the
            # separately-sourced AllTrails batch.
            if self._is_trail_like_attraction(row_name, "attraction", str(row.get("description", "") or "")):
                continue
            rating = row.get("rating")
            try:
                rating_value = float(rating) if rating is not None else -1.0
            except (TypeError, ValueError):
                rating_value = -1.0
            candidates.append((rating_value, order, key, row))

        # Highest rating first; missing ratings (-1.0) and ties keep harvest order.
        candidates.sort(key=lambda entry: (-entry[0], entry[1]))

        added: list[dict[str, Any]] = []
        seen_new_keys: set[str] = set()
        for _rating, _order, key, row in candidates:
            if len(added) >= additional_slots:
                break
            if key in seen_new_keys:
                continue
            row_name = self._direct_batch_row_name(row)
            row_url = str(row.get("url", "") or "").strip()
            new_item: dict[str, Any] = {"name": row_name, "type": "attraction"}
            if row_url:
                new_item["url"] = row_url
            row_desc = self._sanitize_direct_batch_description_text(
                str(row.get("description", "") or row.get("practical_note", "") or "")
            )
            if row_desc and not self._is_metadata_only_residual_text(row_desc, name=row_name):
                new_item["description"] = row_desc
            added.append(new_item)
            seen_new_keys.add(key)

        if not added:
            return attractions
        return existing + added

    def _prioritize_direct_batch_trails(
        self,
        attractions: list[dict[str, Any]],
        dest_name: str,
        dest_dates: str | None,
        seed_names: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Trail-specific counterpart to _prioritize_direct_batch_attractions.

        Trails need their own injection pass because _discover_attractions only
        ever looks up a URL for a trail name the AI already generated -- with no
        mechanism to pull additional hikes in from the AllTrails direct-batch
        harvest, a rich batch of candidates was going entirely unused whenever
        the AI happened to generate few (or zero) trail-type attractions.
        """
        try:
            rows = self._get_alltrails_direct_batch_rows_for_destination(
                dest_name, str(dest_dates or ""), seed_names=seed_names
            )
        except AttributeError:
            return attractions
        if not rows:
            return attractions

        existing = [dict(item) for item in attractions if isinstance(item, dict)]
        existing_keys = {
            re.sub(r"[^a-z0-9]+", " ", str(item.get("name", "") or "").lower()).strip()
            for item in existing
        }
        existing_trail_count = sum(
            1
            for item in existing
            if self._is_trail_like_attraction(
                str(item.get("name", "") or ""),
                str(item.get("type", "attraction") or "attraction").lower(),
                self._attraction_trail_context(item),
            )
        )

        day_count = self._infer_destination_day_count(dest_dates or "")
        items_per_day = int(
            getattr(self, "_trail_direct_batch_items_per_day", DEFAULT_TRAIL_DIRECT_BATCH_ITEMS_PER_DAY)
            or DEFAULT_TRAIL_DIRECT_BATCH_ITEMS_PER_DAY
        )
        target_total = max(existing_trail_count, items_per_day * day_count)
        additional_slots = max(0, target_total - existing_trail_count)
        if additional_slots <= 0:
            return attractions

        max_miles = float(getattr(self, "_max_trail_miles", DEFAULT_MAX_TRAIL_MILES) or 0)
        slug_denylist = getattr(self, "_alltrails_slug_denylist", frozenset())

        candidates: list[tuple[float, int, str, dict[str, Any], str]] = []
        for order, row in enumerate(rows):
            row_name = self._direct_batch_row_name(row)
            if not row_name:
                continue
            key = re.sub(r"[^a-z0-9]+", " ", row_name.lower()).strip()
            if not key or key in existing_keys:
                continue
            raw_url = str(row.get("url", "") or "").strip()
            if not raw_url or not self._is_alltrails_trail_url(raw_url):
                continue
            if self._candidate_mentions_conflicting_destination(row, dest_name):
                continue
            normalized_url = self._strip_alltrails_tracking(raw_url)
            if self._alltrails_slug_has_numbered_suffix(normalized_url):
                continue
            if not self._alltrails_slug_matches_item(normalized_url, row_name):
                continue
            slug = urlparse(normalized_url).path.rsplit("/", 1)[-1].lower()
            if slug in slug_denylist:
                continue
            if self._has_alltrails_closure_marker(self._candidate_text_blob(row)):
                continue
            meta = self._extract_alltrails_candidate_metadata(row)
            miles = meta.get("miles")
            if miles is not None and max_miles > 0 and float(miles) > max_miles + 0.15:
                continue
            rating = row.get("rating") if row.get("rating") is not None else meta.get("rating")
            try:
                rating_value = float(rating) if rating is not None else -1.0
            except (TypeError, ValueError):
                rating_value = -1.0
            candidates.append((rating_value, order, key, row, normalized_url))

        # Highest rating first; missing ratings (-1.0) and ties keep harvest order.
        candidates.sort(key=lambda entry: (-entry[0], entry[1]))

        added: list[dict[str, Any]] = []
        seen_new_keys: set[str] = set()
        for _rating, _order, key, row, normalized_url in candidates:
            if len(added) >= additional_slots:
                break
            if key in seen_new_keys:
                continue
            row_name = self._direct_batch_row_name(row)
            new_item: dict[str, Any] = {"name": row_name, "type": "hike", "url": normalized_url}
            # Found 2026-08-16 (dipstick58): unlike
            # _prioritize_direct_batch_attractions just above, this trail path
            # never copied the harvested row's description/practical_note into
            # the new item -- every trail injected here rendered with a
            # permanently empty teaser regardless of whether the direct-batch
            # HTML actually contained a descriptive note (it usually did; see
            # the row parsing in _direct_batch_rows_from_html). That alone
            # accounted for 10/10 of the empty attraction/trail teasers in a
            # real run (0 attractions were affected, only trails), so it's
            # copied here the same way the attraction path already does.
            row_desc = self._sanitize_direct_batch_description_text(
                str(row.get("description", "") or row.get("practical_note", "") or "")
            )
            if row_desc and not self._is_metadata_only_residual_text(row_desc, name=row_name):
                new_item["description"] = row_desc
            added.append(new_item)
            seen_new_keys.add(key)

        if not added:
            return attractions
        return existing + added

    @staticmethod
    def _normalized_name_tokens_for_restaurant_match(name: str) -> set[str]:
        raw_tokens = re.findall(r"[a-z0-9]+", str(name or "").lower())
        stop = {
            "restaurant", "restaurante", "cafe", "grill", "kitchen", "bar", "eatery",
            "the", "and", "co", "company", "llc",
        }
        out: set[str] = set()
        for token in raw_tokens:
            if token in stop or len(token) <= 2:
                continue
            out.add(token)
        return out

    @staticmethod
    def _restaurant_metadata_missing(item: dict[str, Any]) -> bool:
        if not isinstance(item, dict):
            return True
        return not any(
            str(item.get(key, "") or "").strip()
            for key in ("cuisine", "price_range", "price")
        ) and not bool(item.get("reserve_recommended", False))

    def _backfill_restaurant_metadata_from_existing(
        self,
        merged_items: list[dict[str, Any]],
        existing_items: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        if not merged_items or not existing_items:
            return merged_items

        existing_norm: list[dict[str, Any]] = [dict(item) for item in existing_items if isinstance(item, dict)]
        for merged in merged_items:
            if not isinstance(merged, dict) or not self._restaurant_metadata_missing(merged):
                continue
            target_name = str(merged.get("name", "") or "").strip()
            if not target_name:
                continue
            target_tokens = self._normalized_name_tokens_for_restaurant_match(target_name)
            if not target_tokens:
                continue

            best_match: dict[str, Any] | None = None
            best_score = 0.0
            for source in existing_norm:
                source_name = str(source.get("name", "") or source.get("title", "") or "").strip()
                if not source_name:
                    continue
                source_tokens = self._normalized_name_tokens_for_restaurant_match(source_name)
                if not source_tokens:
                    continue
                overlap = len(target_tokens & source_tokens)
                if overlap <= 0:
                    continue
                union = len(target_tokens | source_tokens)
                score = overlap / union if union else 0.0
                if score > best_score:
                    best_score = score
                    best_match = source

            # Avoid overly loose joins; require decent token overlap.
            if not best_match or best_score < 0.45:
                continue

            for key in ("cuisine", "price_range", "price", "reserve_recommended"):
                if key in merged and merged.get(key) not in (None, "", False):
                    continue
                source_val = best_match.get(key)
                if source_val in (None, ""):
                    continue
                merged[key] = source_val

        return merged_items

    def _prioritize_direct_batch_en_route_stops(
        self,
        stops: list[dict[str, Any]],
        dest_name: str,
        dest_dates: str | None,
        origin_name: str,
        seed_names: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        try:
            rows = self._get_en_route_direct_batch_rows_for_destination(
                dest_name, str(dest_dates or ""), origin_name, seed_names=seed_names
            )
        except AttributeError:
            return stops
        if not rows:
            return stops
        # Preserve legacy behavior when AI yields no en-route ideas:
        # keep a compact shortlist instead of surfacing the entire batch.
        if not stops:
            target_count = min(4, len(rows))
        else:
            target_count = self._primary_list_target_count(
                len(stops),
                int(getattr(self, "_en_route_direct_batch_item_count", DEFAULT_EN_ROUTE_DIRECT_BATCH_ITEM_COUNT) or DEFAULT_EN_ROUTE_DIRECT_BATCH_ITEM_COUNT),
                len(rows),
            )
        merged = self._build_primary_items_from_direct_batch(
            rows=rows,
            existing_items=stops,
            target_count=target_count,
            fallback_description="Optional stop for the inbound transfer leg.",
            dest_name=dest_name,
        )
        return self._dedupe_en_route_stops_by_specificity(merged)

    def _normalize_url_for_item_dedupe(self, url: str) -> str:
        candidate = str(url or "").strip()
        if not candidate:
            return ""
        normalized = self._normalize_direct_batch_authoritative_url(candidate)
        if not normalized:
            return ""
        if self._is_google_maps_candidate_url(normalized):
            parsed = urlparse(normalized)
            params = parse_qs(parsed.query or "")
            query = (
                (params.get("query", [""])[0] or "")
                or (params.get("q", [""])[0] or "")
            ).strip()
            if query:
                return f"maps-query:{query.lower()}"
        return normalized.lower()

    @staticmethod
    @staticmethod
    def _is_generic_en_route_stop_title(name: str) -> bool:
        text = str(name or "").strip().lower()
        if not text:
            return True
        if len(text) < 6:
            return True
        generic_patterns = (
            r"\b(top|best)\b.*\b(stops?|places?|stopovers?)\b",
            r"\bstopovers?\b",
            r"\bthings\s+to\s+do\b",
            r"\bitinerary\b",
            r"\broad\s*trip\b",
            r"\bstops?\s+along\b",
            r"\bscenic\s+drive\s+from\b.*\bto\b",
            r"\bscenic\s+route\b",
            r"\broute\s+option\b",
            r"\bscenic\s+stops?\b",
            r"\bstops?\s+on\s+the\s+(?:drive|route|road|way)\b",
            r"\bdriving?\s+from\b.*\bto\b",
        )
        return any(re.search(pattern, text) for pattern in generic_patterns)

    @classmethod
    def _en_route_stop_name_duplicates_destination(cls, stop_name: str, dest_name: str) -> bool:
        """True when an en-route stop's own name is (or reduces to) the
        arrival destination itself -- e.g. a candidate literally titled
        "Capitol Reef National Park" surfacing as an en-route stop on the
        Bryce -> Capitol Reef leg. Comparing full significant-token sets
        (not substring/overlap) avoids false positives from a single shared
        word (see the "Canyon Overlook Trail" / "Bryce Canyon National Park"
        false-qualification failure mode already documented on
        _build_route_gmaps_url) while still catching exact-name and
        trivial-suffix duplicates ("Capitol Reef", "Capitol Reef NP").

        This is a real symptom the project owner's own Google Maps
        screenshot of the Bryce -> Capitol Reef leg surfaced: "Capitol Reef
        National Park" appearing as its own waypoint entry immediately
        before the actual destination. An en-route stop whose resolved name
        IS the destination isn't a real detour -- it belongs nowhere in the
        waypoint list (or the "can't-miss enroute" card), since recommending
        a detour to the destination itself, right before arriving, is
        never a sensible suggestion.
        """
        stop_tokens = frozenset(cls._significant_tokens(stop_name))
        dest_tokens = frozenset(cls._significant_tokens(dest_name))
        if not stop_tokens or not dest_tokens:
            return False
        return stop_tokens == dest_tokens

    @classmethod
    def _en_route_stop_duplicates_destination_own_list(
        cls, stop_name: str, dest: dict[str, Any] | None
    ) -> str | None:
        """True (returning the matching entry's own name) when an en-route
        stop is the same real place as something the destination already
        lists among its own scenic drives or top attractions.

        Real dipstick63 example: Zion's "Getting Here" en-route stops
        included "Kolob Canyons Scenic Drive" while Zion's own scenic-drives
        list (generated independently by ai_content.py's destination-content
        call, which always runs before URL discovery -- see
        generate_destination_content / _discover_scenic_drives's own
        multi-site-grouping comment for the same ordering fact) separately
        included "Kolob Canyons Road" for the exact same real place. Two
        subsystems (en-route-stop discovery here vs. destination
        scenic-drive/attraction content generation in ai_content.py) proposed
        the same real place under two different names and never cross-checked
        each other.

        The destination's own entry wins on a match: it's the fuller,
        more-authoritative treatment (a real duration/description tailored to
        being explored, not just passed by), so the en-route-stop duplicate
        is dropped in favor of it. Uses the same full-significant-token-set
        equality as _en_route_stop_name_duplicates_destination just above
        (not substring/overlap) so a single shared generic word can't cause a
        false-positive match -- and _significant_tokens already strips
        generic route/drive suffix words ("drive", "road", "scenic", "byway",
        "trail", "point", ...), which is exactly what lets "Kolob Canyons
        Scenic Drive" and "Kolob Canyons Road" reduce to the same token set.
        """
        stop_tokens = frozenset(cls._significant_tokens(stop_name))
        if not stop_tokens or not isinstance(dest, dict):
            return None

        candidate_names: list[str] = []
        scenic_drives = dest.get("scenic_drives", [])
        if isinstance(scenic_drives, list):
            for drive in scenic_drives:
                if isinstance(drive, dict):
                    candidate_names.append(str(drive.get("title", "") or drive.get("name", "") or ""))
        ai_content = dest.get("ai_content", {})
        if isinstance(ai_content, dict):
            top_attractions = ai_content.get("top_attractions", [])
            if isinstance(top_attractions, list):
                for attraction in top_attractions:
                    if isinstance(attraction, dict):
                        candidate_names.append(str(attraction.get("name", "") or ""))

        for candidate_name in candidate_names:
            candidate_name = candidate_name.strip()
            if not candidate_name:
                continue
            candidate_tokens = frozenset(cls._significant_tokens(candidate_name))
            if candidate_tokens and candidate_tokens == stop_tokens:
                return candidate_name
        return None

    @staticmethod
    def _extract_named_stops_from_description(description: str) -> list[str]:
        """Extract specific named place candidates from a list-style en-route description."""
        text = str(description or "").strip()
        if not text or len(text) < 8:
            return []
        if ": " in text:
            text = text.split(": ", 1)[1]
        # Remove parenthetical annotations before splitting so decimal points
        # inside parens (e.g. "4.6 stars") don't break sentence detection.
        text = re.sub(r"\([^)]*\)", "", text)
        # Normalise common abbreviations so "Mt. Carmel" isn't split mid-name.
        text = re.sub(r"\b(Mt|St|Dr|Hwy|Rte|Rd|Blvd|Ave|Ft|Pt)\.\s+", r"\1 ", text)
        skip_starts = {
            "avoids", "no ", "note:", "skip", "google", "all ", "each ",
            "these", "this ", "also ", "and ", "the ", "a ", "an ",
            "includes", "including", "see ", "visit ", "check", "with ",
            "from ", "detour", "quick",
        }
        names: list[str] = []
        for raw in re.split(r"\.\s+|;\s*", text):
            frag = raw.strip()
            if not frag or len(frag) < 5:
                continue
            clean = frag.split(",")[0].strip()
            if len(clean) < 5 or len(clean.split()) < 2:
                continue
            lower_clean = clean.lower()
            if any(lower_clean.startswith(w) for w in skip_starts):
                continue
            if not clean[0].isupper():
                continue
            if not any(n.lower() == lower_clean for n in names):
                names.append(clean)
        return names

    def _dedupe_en_route_stops_by_specificity(self, stops: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if not stops:
            return stops

        def _score(name: str) -> tuple[int, int, int]:
            generic_penalty = 0 if not self._is_generic_en_route_stop_title(name) else -100
            token_count = len(re.findall(r"[a-z0-9]+", (name or "").lower()))
            char_count = len((name or "").strip())
            return (generic_penalty, token_count, char_count)

        best_by_url: dict[str, dict[str, Any]] = {}
        ordered_without_url: list[dict[str, Any]] = []
        seen_names_without_url: set[str] = set()

        for raw in stops:
            if not isinstance(raw, dict):
                continue
            item = dict(raw)
            name = str(item.get("name", "") or item.get("title", "") or "").strip()
            if not name:
                continue
            item["name"] = name
            url_key = self._normalize_url_for_item_dedupe(str(item.get("url", "") or ""))

            if url_key:
                existing = best_by_url.get(url_key)
                if existing is None:
                    best_by_url[url_key] = item
                    continue
                existing_name = str(existing.get("name", "") or existing.get("title", "") or "").strip()
                if _score(name) > _score(existing_name):
                    best_by_url[url_key] = item
                continue

            lowered_name = name.lower()
            if lowered_name in seen_names_without_url:
                continue
            if self._is_generic_en_route_stop_title(name):
                continue
            seen_names_without_url.add(lowered_name)
            ordered_without_url.append(item)

        result: list[dict[str, Any]] = []
        emitted_name_keys: set[str] = set()
        for raw in stops:
            if not isinstance(raw, dict):
                continue
            url_key = self._normalize_url_for_item_dedupe(str(raw.get("url", "") or ""))
            if url_key:
                chosen = best_by_url.get(url_key)
                if not chosen:
                    continue
                chosen_name_key = str(chosen.get("name", "") or "").strip().lower()
                if chosen_name_key and chosen_name_key not in emitted_name_keys:
                    result.append(chosen)
                    emitted_name_keys.add(chosen_name_key)
                best_by_url.pop(url_key, None)

        for item in ordered_without_url:
            name_key = str(item.get("name", "") or "").strip().lower()
            if name_key and name_key not in emitted_name_keys:
                result.append(item)
                emitted_name_keys.add(name_key)

        return result

    @staticmethod
    def _en_route_stop_name_is_bare_street_address(name: str) -> bool:
        """True when a stop's whole label is essentially just a street
        address (e.g. "118 E Center St, Moab, UT 84532") rather than a real
        place name -- a harvested-from-Maps-pin row with no venue name
        attached. The distinguishing signal is simple and robust: a real
        place name doesn't start with a number, a street address does."""
        return bool(re.match(r"^\d", str(name or "").strip()))

    @staticmethod
    def _en_route_stop_address_key(name: str) -> str:
        """Extract a normalized '<street number> <street words>' key from a
        stop's name/label, for matching two en-route-stop candidates that
        name the same real place by address rather than by identical text.

        Real example (Moab -> Arches leg, from a Google Maps waypoint
        screenshot): the harvested list included both a bare address
        ("118 E Center St, Moab, UT 84532") and a named entry for the exact
        same address ("Moab Museum, 118 E Center St, Moab, UT") as two
        separate candidates -- same real place, one captured as a pin
        address, the other as a venue name plus its address. Stripping
        directional words, street-type suffixes, and everything after the
        street segment (city/state/zip, or a leading venue-name prefix
        before the address starts) reduces both forms to the same
        "118 center" key.
        """
        text = str(name or "").lower()
        # A street number followed by street-name words, up to the next
        # comma (or end of string) -- text before the match (a venue-name
        # prefix like "moab museum, ") isn't part of the address itself.
        m = re.search(r"(\d{1,6})\s+([a-z][a-z0-9\s]*?)(?:,|$)", text)
        if not m:
            return ""
        number = m.group(1)
        street = m.group(2)
        street = re.sub(
            r"\b(n|s|e|w|ne|nw|se|sw|north|south|east|west)\b", " ", street
        )
        street = re.sub(
            r"\b(st|street|ave|avenue|rd|road|blvd|boulevard|dr|drive|ln|lane|"
            r"way|hwy|highway|ct|court|pl|place)\b",
            " ",
            street,
        )
        street = re.sub(r"\s+", " ", street).strip()
        if not street:
            return ""
        return f"{number} {street}"

    @classmethod
    def _en_route_stop_place_identity_score(cls, item: dict[str, Any]) -> tuple[int, int, int, int]:
        """Preference score for picking which of two same-place en-route-stop
        entries to keep: a real named entry always beats a bare-address one,
        then prefer whichever has real descriptive content, then fall back to
        the existing specificity tiebreakers (more tokens, longer text)."""
        name = str(item.get("name", "") or "")
        not_bare_address = 0 if cls._en_route_stop_name_is_bare_street_address(name) else 1
        has_description = 1 if str(item.get("description", "") or item.get("practical_note", "") or "").strip() else 0
        token_count = len(re.findall(r"[a-z0-9]+", name.lower()))
        char_count = len(name.strip())
        return (not_bare_address, has_description, token_count, char_count)

    def _dedupe_en_route_stops_same_leg_by_shared_address(
        self, stops: list[dict[str, Any]], dest_name: str
    ) -> list[dict[str, Any]]:
        """Collapse two-or-more entries in the SAME leg's en_route_stops list
        that name the same real place by street address, keeping the more
        informative (named) entry over a bare-address one -- see
        _en_route_stop_address_key's docstring for the real Moab Museum
        example this fixes. This is a same-leg duplicate, distinct from
        _en_route_stop_duplicates_destination_own_list above (which catches
        the same real place under two different names across TWO different
        lists -- en_route_stops vs. the destination's own scenic-drives/
        attractions -- for the Kolob Canyons case)."""
        if not stops:
            return stops

        groups: dict[str, list[dict[str, Any]]] = {}
        ungrouped: list[dict[str, Any]] = []
        for stop in stops:
            if not isinstance(stop, dict):
                ungrouped.append(stop)
                continue
            key = self._en_route_stop_address_key(str(stop.get("name", "") or ""))
            if not key:
                ungrouped.append(stop)
                continue
            groups.setdefault(key, []).append(stop)

        result: list[dict[str, Any]] = []
        seen_ids: set[int] = set()
        for stop in stops:
            if not isinstance(stop, dict) or id(stop) in seen_ids:
                continue
            key = self._en_route_stop_address_key(str(stop.get("name", "") or ""))
            group = groups.get(key, [stop]) if key else [stop]
            if len(group) < 2:
                result.append(stop)
                seen_ids.add(id(stop))
                continue
            best = max(group, key=self._en_route_stop_place_identity_score)
            for candidate in group:
                seen_ids.add(id(candidate))
            result.append(best)
            for candidate in group:
                if candidate is best:
                    continue
                self._log_decision(
                    kind="en_route_stop",
                    dest_name=dest_name,
                    item_name=str(candidate.get("name", "") or ""),
                    reason="en_route_duplicate_same_place_in_leg",
                    message=(
                        "en-route stop removed: shares a street address with another "
                        f"entry on the same leg ('{best.get('name', '')}'), same real place"
                    ),
                )
        return result

    def _dedupe_en_route_stops_same_leg_by_geocode_proximity(
        self, stops: list[dict[str, Any]], dest_name: str
    ) -> list[dict[str, Any]]:
        """Same-leg same-place collapse as
        _dedupe_en_route_stops_same_leg_by_shared_address above, but for
        stops that don't share a parseable street address yet DO already
        carry a verified geocode (set by _prune_en_route_stops_by_geometry,
        which runs before this) landing within a couple hundred feet of each
        other -- close enough that they can only be the same real point of
        interest, not two distinct nearby places."""
        if not stops or len(stops) < 2:
            return stops

        same_place_radius_miles = 0.05  # ~260 feet
        drop_ids: set[int] = set()
        n = len(stops)
        for i in range(n):
            stop_a = stops[i]
            if not isinstance(stop_a, dict) or id(stop_a) in drop_ids:
                continue
            coords_a = self._parse_lat_lng(stop_a.get("geocode_lat"), stop_a.get("geocode_lng"))
            if coords_a is None:
                continue
            for j in range(i + 1, n):
                stop_b = stops[j]
                if not isinstance(stop_b, dict) or id(stop_b) in drop_ids:
                    continue
                coords_b = self._parse_lat_lng(stop_b.get("geocode_lat"), stop_b.get("geocode_lng"))
                if coords_b is None:
                    continue
                if self._haversine_miles(coords_a, coords_b) > same_place_radius_miles:
                    continue
                keep, drop = (
                    (stop_a, stop_b)
                    if self._en_route_stop_place_identity_score(stop_a)
                    >= self._en_route_stop_place_identity_score(stop_b)
                    else (stop_b, stop_a)
                )
                drop_ids.add(id(drop))
                self._log_decision(
                    kind="en_route_stop",
                    dest_name=dest_name,
                    item_name=str(drop.get("name", "") or ""),
                    reason="en_route_duplicate_same_place_in_leg",
                    message=(
                        "en-route stop removed: geocodes within "
                        f"{same_place_radius_miles} mi of another entry on the same leg "
                        f"('{keep.get('name', '')}'), same real place"
                    ),
                )
                if drop is stop_a:
                    break

        if not drop_ids:
            return stops
        return [stop for stop in stops if not (isinstance(stop, dict) and id(stop) in drop_ids)]

    # NOTE on detour semantics (investigated for dipstick59 Bug 2 -- "are
    # distances for detour one way or two way or loop that connects
    # downstream?"): detour_distance_miles/detour_time_minutes (both here and
    # in the sibling _extract_en_route_detour_miles_from_text /
    # _en_route_stop_detour_metrics / _en_route_stop_within_threshold below)
    # are taken verbatim from whatever free-text the AI generated for an
    # en-route stop -- prompts/destination_content.txt only asks for "numeric
    # miles off main route" / "numeric extra drive minutes", which does not
    # specify one-way, round-trip (there-and-back), or a loop that rejoins
    # the route further downstream without backtracking. There is no
    # normalization anywhere in this pipeline reconciling those three cases.
    # This is a real, open gap, not an oversight fixed here: every consumer
    # of these two fields (_en_route_stop_within_threshold's minutes/miles
    # cap below, and the "(X mi detour | Y min)" display text in
    # html_assembler.py) treats the number as an opaque scalar and does not
    # itself assume a specific interpretation, so there is no single wrong
    # assumption to correct in code. Resolving the ambiguity for real would
    # mean either standardizing what the prompt asks the AI to report, or
    # having a downstream consumer (e.g. real route/schedule time-budgeting)
    # that needs one specific interpretation and can drive the decision --
    # both are product/design decisions, not a bug fix.
    @staticmethod
    def _extract_en_route_detour_minutes_from_text(text: str) -> int | None:
        t = str(text or "").lower()
        if not t:
            return None
        hour_match = re.search(r"(\d+)\s*(?:hr|hrs|hour|hours)", t)
        min_match = re.search(r"(\d+)\s*(?:m|min|mins|minute|minutes)\b", t)
        if not hour_match and not min_match:
            return None
        total = 0
        if hour_match:
            total += int(hour_match.group(1)) * 60
        if min_match:
            total += int(min_match.group(1))
        return total if total > 0 else None

    @staticmethod
    def _extract_en_route_detour_miles_from_text(text: str) -> float | None:
        t = str(text or "").lower()
        if not t:
            return None
        m = re.search(r"(\d+(?:\.\d+)?)\s*(?:mi|mile|miles)\b", t)
        if not m:
            return None
        try:
            miles = float(m.group(1))
        except (TypeError, ValueError):
            return None
        return miles if miles > 0 else None

    def _en_route_stop_detour_metrics(self, stop: dict[str, Any]) -> tuple[float | None, int | None]:
        miles: float | None = None
        minutes: int | None = None

        raw_miles = stop.get("detour_distance_miles") if isinstance(stop, dict) else None
        raw_minutes = stop.get("detour_time_minutes") if isinstance(stop, dict) else None

        try:
            parsed_miles = float(raw_miles)
            if parsed_miles > 0:
                miles = parsed_miles
        except (TypeError, ValueError):
            miles = None

        try:
            parsed_minutes = int(raw_minutes)
            if parsed_minutes > 0:
                minutes = parsed_minutes
        except (TypeError, ValueError):
            minutes = None

        blob = " ".join(
            [
                str(stop.get("name", "") or ""),
                str(stop.get("description", "") or ""),
                str(stop.get("practical_note", "") or ""),
            ]
        )
        if minutes is None:
            minutes = self._extract_en_route_detour_minutes_from_text(blob)
        if miles is None:
            miles = self._extract_en_route_detour_miles_from_text(blob)
        return miles, minutes

    # Real-coordinate-grounded detour distance/time -- corrects the
    # free-text-mined numbers above when the stop has verified geocoded
    # coordinates (dipstick63: "Kolob Canyons Scenic Drive" rendered "(10 mi
    # detour | 15 min)" on the real St. George -> Springdale leg for a
    # detour that actually runs ~18-20 mi one-way off I-15 near exit 40 --
    # the AI-generated prose that number was mined from was simply wrong,
    # and nothing cross-checked it against the stop's own coordinates, which
    # ARE available here: _prune_en_route_stops_by_geometry (which runs
    # immediately before this in _discover_en_route_stops's call order)
    # already geocodes every surviving stop and persists
    # geocode_lat/geocode_lng onto it for the maps_url it builds.
    #
    # This is a narrower, more tractable problem than
    # _prune_en_route_stops_by_geometry's straight-line lateral-distance
    # check (see that method's long comment on why a hard distance cutoff
    # for stop *inclusion* was tried and reverted -- "Swasey's Beach"
    # legitimately sits 35.4 mi off the straight route line via real,
    # winding Utah highways, so straight-line distance alone can't safely
    # decide whether a stop belongs on the list at all). Correcting the
    # *displayed number* for a stop whose inclusion has already been decided
    # is different: a round-trip detour (leave the route, drive to the
    # point, drive back to roughly the same spot on the route) can never be
    # shorter than 2x the point's straight-line perpendicular offset from
    # the route, because a real road can never be shorter than the
    # straight-line distance between two points. That is a hard geometric
    # floor, not merely a competing estimate -- so a text-mined value below
    # it is provably wrong, not just "different from our guess", and is
    # always safe to override regardless of how the ambiguous
    # one-way/round-trip/loop semantics documented above
    # _extract_en_route_detour_minutes_from_text end up being interpreted
    # elsewhere.
    @staticmethod
    def _en_route_stop_geometry_grounded_detour_floor(perpendicular_miles: float) -> tuple[float, int]:
        """Hard lower bound on a round-trip detour's distance/time implied by
        a stop's straight-line perpendicular offset from the route. See the
        block comment above for why this is a provable floor, not an
        estimate."""
        min_round_trip_miles = max(0.0, perpendicular_miles) * 2.0
        min_round_trip_minutes = (
            (min_round_trip_miles / MAX_PLAUSIBLE_EN_ROUTE_DETOUR_MPH) * 60.0 if min_round_trip_miles else 0.0
        )
        return min_round_trip_miles, int(ceil(min_round_trip_minutes))

    @staticmethod
    def _en_route_stop_geometry_grounded_detour_estimate(perpendicular_miles: float) -> tuple[float, int]:
        """Best-effort round-trip distance/time estimate (not just the hard
        floor above) for filling in a stop that has no text-mined/AI-provided
        detour figures at all. Uses the same real-road inflation factor and
        average speed _estimate_route_from_haversine already uses elsewhere
        in this file for the main leg distance/time, applied to the
        round-trip (2x perpendicular) distance rather than a one-way leg."""
        estimate_miles = max(0.0, perpendicular_miles) * 2.0 * ROAD_DISTANCE_FACTOR
        estimate_minutes = drive_minutes(estimate_miles) if estimate_miles else 0.0
        return round(estimate_miles, 1), int(round(estimate_minutes))

    def _resolve_en_route_stop_detour_metrics_against_geometry(
        self,
        stop: dict[str, Any],
        *,
        origin: tuple[float, float] | None,
        dest: tuple[float, float] | None,
    ) -> tuple[float | None, int | None, bool]:
        """Return (miles, minutes, was_overridden): the text-mined/AI-provided
        detour figures for `stop`, corrected against its own verified geocode
        when one is available and the correction is geometrically forced. See
        the block comment above _en_route_stop_geometry_grounded_detour_floor
        for the reasoning."""
        text_miles, text_minutes = self._en_route_stop_detour_metrics(stop)
        geocode_lat = stop.get("geocode_lat")
        geocode_lng = stop.get("geocode_lng")
        has_geocode = isinstance(geocode_lat, (int, float)) and isinstance(geocode_lng, (int, float))
        if not has_geocode or origin is None or dest is None:
            return text_miles, text_minutes, False

        perpendicular_miles = self._route_perpendicular_distance_miles(
            origin=origin, dest=dest, point=(float(geocode_lat), float(geocode_lng))
        )
        if perpendicular_miles is None:
            return text_miles, text_minutes, False

        min_miles, min_minutes = self._en_route_stop_geometry_grounded_detour_floor(perpendicular_miles)
        estimate_miles, estimate_minutes = self._en_route_stop_geometry_grounded_detour_estimate(perpendicular_miles)

        final_miles, final_minutes = text_miles, text_minutes
        overridden = False
        if text_miles is None or text_miles < min_miles:
            final_miles = estimate_miles
            overridden = True
        if text_minutes is None or text_minutes < min_minutes:
            final_minutes = estimate_minutes
            overridden = True
        # The two checks above are independent: a stop only slightly off the
        # route has a small floor in *both* dimensions, so a text-mined pair
        # can clear each one separately while still being mutually
        # implausible together (e.g. "22 mi in 15 min" = 88 mph -- real
        # example, Kodachrome Basin State Park). Check the resulting pair's
        # implied speed as a final pass; if it's still unrealistic, neither
        # individual number can be trusted, so replace both with the
        # geometry-grounded estimate rather than patching just one.
        # Both checks above only ever RAISE a value: they floor an under-stated
        # detour and never question an over-stated one. So a stop sitting on
        # the route keeps whatever the model wrote about it, and the card reads
        # "46.0 mi detour" for somewhere you drive straight past. Reported on
        # the Capitol Reef -> Moab leg (San Rafael Swell, John Wesley Powell
        # Museum -- both on I-70) and again for Mancos State Park Entrance.
        #
        # The geometry gives an upper bound as well as a lower one. A detour
        # cannot sensibly cost several times the round trip its own offset
        # implies, so a text figure far above that is not a better estimate
        # than the geometry -- it is a worse one. Tolerance is deliberately
        # loose (3x plus a 5-mile allowance) because real roads bend, and the
        # aim is to catch the absurd rather than to second-guess the plausible.
        if final_miles is not None and estimate_miles is not None:
            ceiling_miles = max(estimate_miles * 3.0, estimate_miles + 5.0)
            if final_miles > ceiling_miles:
                final_miles, final_minutes = estimate_miles, estimate_minutes
                overridden = True
        if final_miles and final_minutes:
            implied_mph = final_miles / (final_minutes / 60.0)
            if implied_mph > MAX_PLAUSIBLE_EN_ROUTE_DETOUR_MPH:
                final_miles, final_minutes = estimate_miles, estimate_minutes
                overridden = True
        return final_miles, final_minutes, overridden

    def _en_route_stop_within_threshold(self, stop: dict[str, Any]) -> tuple[bool, str]:
        max_minutes = int(getattr(self, "_en_route_detour_max_minutes", DEFAULT_EN_ROUTE_DETOUR_MAX_MINUTES) or 0)
        max_miles = float(getattr(self, "_en_route_detour_max_miles", DEFAULT_EN_ROUTE_DETOUR_MAX_MILES) or 0)
        require_metadata = bool(getattr(self, "_en_route_require_detour_metadata", DEFAULT_EN_ROUTE_REQUIRE_DETOUR_METADATA))
        is_seed = bool(isinstance(stop, dict) and stop.get("is_seed"))

        miles, minutes = self._en_route_stop_detour_metrics(stop)
        if require_metadata and miles is None and minutes is None:
            if is_seed:
                # A manifest en_route_seeds candidate is the traveler's own
                # explicit pick, not an AI/harvest guess that merely lacks
                # detour metadata by chance -- it shouldn't need pre-existing
                # detour distance/time text to survive, mirroring how a
                # seeded top_attraction already bypasses the max_trail_miles
                # demotion above (see "seed_threshold_override" there). This
                # does not skip real verification: the seed still has to
                # clear _prune_en_route_stops_by_geometry's actual geocoding
                # and route-proximity checks that run right after this
                # filter, same as any other candidate.
                return True, "seed_threshold_override"
            return False, "missing_detour_metadata"
        if max_minutes > 0 and minutes is not None and minutes > max_minutes:
            return False, "detour_minutes_exceeded"
        if max_miles > 0 and miles is not None and miles > max_miles:
            return False, "detour_miles_exceeded"
        return True, "ok"

    # ── En-Route Stops ───────────────────────────────────────────────────────

    def _ensure_en_route_seed_candidates(
        self,
        stops: list[dict[str, Any]],
        dest: dict[str, Any] | None,
        dest_name: str,
    ) -> list[dict[str, Any]]:
        """Guarantee manifest `en_route_seeds` (see manifest_parser.py) are present
        as en-route-stop candidates for the leg arriving at this destination.

        This is the en-route-stop counterpart to how `seeds` get folded into
        `top_attractions` for destination-attraction discovery: a seed name not
        already among the proposed stops is added as a bare-name candidate so it
        gets a search-priority boost (guaranteed candidacy) rather than depending
        on the AI or the direct-link-batch harvest happening to propose it.

        Crucially this does NOT bypass verification -- an injected seed still
        flows through every check that runs immediately after this call inside
        _discover_en_route_stops (threshold filtering via
        _en_route_stop_within_threshold, generic-title filtering, and real
        geocoding/route-proximity pruning via _prune_en_route_stops_by_geometry),
        exactly like any AI-proposed or batch-harvested stop. A seed that isn't a
        real, verifiably on-route place is filtered out same as anything else.
        """
        seed_names = [
            str(seed or "").strip()
            for seed in ((dest or {}).get("en_route_seeds", []) or [])
            if str(seed or "").strip()
        ]
        if not seed_names:
            return stops

        existing_keys = {
            re.sub(r"[^a-z0-9]+", " ", str((stop or {}).get("name", "") or "").lower()).strip()
            for stop in stops
            if isinstance(stop, dict)
        }
        result = list(stops)
        for seed in seed_names:
            key = re.sub(r"[^a-z0-9]+", " ", seed.lower()).strip()
            if not key or key in existing_keys:
                continue
            result.append({"name": seed, "is_seed": True})
            existing_keys.add(key)
            self._log_decision(
                kind="en_route_stop",
                dest_name=dest_name,
                item_name=seed,
                reason="en_route_seed_injected",
                message="en-route seed added as a discovery candidate for the incoming leg (subject to normal verification)",
            )
        return result

    #: Kept in step with AIContentGenerator._NON_DRIVING_ARRIVAL_MODES.
    #: Duplicated rather than imported: url_discovery importing ai_content
    #: would be circular, and a shared constants module for one frozenset is
    #: not worth the indirection.
    _NON_DRIVING_ARRIVAL_MODES = NON_DRIVING_BOOKED_TYPES

    @classmethod
    def _arrival_is_not_self_driven(cls, dest: dict[str, Any] | None) -> bool:
        """Delegates. This module and ai_content each had their own copy, and
        the fix for en-route stops on rail had to be made in both -- "two
        sources, one rule", as the comment at the call site puts it. There is
        one source now."""
        return not leg_mode(dest).has_roadside

    def _discover_leg_trail_link(
        self, ai: dict[str, Any], dest: dict[str, Any] | None, origin_name: str, dest_name: str
    ) -> None:
        """Attach the AllTrails page for THIS leg's section of a named trail.

        Only where the manifest has said enough to ask: a `bike`/`hike` leg
        and a `trip.trail_name`. Without the name there is no query worth
        making -- "Cascade Locks to Trout Lake" alone matches whatever
        AllTrails has near either end, which on a thru-hike is a day loop.

        Goes through _search_alltrails_for_trail rather than around it, so the
        publish-confidence gate, the slug denylist and the post-search filters
        all apply. Those exist because AllTrails matching fails in specific
        documented ways, and a leg link is no more trustworthy than an
        attraction link.
        """
        from generator.transit_routing import TRAIL_NAME_KEY

        trail_name = str((dest or {}).get(TRAIL_NAME_KEY, "") or "").strip()
        if not trail_name or not dest_name:
            return
        # origin_name is only needed to COMPOSE a section name. The first
        # destination has none here even when trip.departure gives it a real
        # leg, so requiring it dropped the first leg's link on a manifest that
        # named its section outright -- 14 of 15 rendered.
        authored = (dest or {}).get("trail_url") or (dest or {}).get("trail_section")
        if not origin_name and not authored:
            return
        getting_here = ai.get("getting_here")
        if not isinstance(getting_here, dict):
            return

        # ONLY an authored section name is searched for. Composing
        # "<trail> <stop> to <stop>" was tried and is now removed, because it
        # failed in both directions:
        #
        #   On the PCT it matched nothing -- AllTrails names those pages by
        #   guidebook section, so the strict matcher rejected every composed
        #   candidate across a full 15-leg run.
        #
        #   On the East Coast Greenway it matched something WRONG. "East Coast
        #   Greenway Newburyport, Massachusetts to Boston, Massachusetts"
        #   resolved to the East Boston Greenway, a three-mile neighbourhood
        #   path, on the strength of the shared words. It then rendered under
        #   a label naming the 43-mile leg. A rider following that link gets
        #   the wrong path, which is worse than getting none.
        #
        # A name the generator invented is not evidence about a catalogue's
        # contents. Without one the human supplied, there is no query worth
        # making.
        section = str((dest or {}).get("trail_section", "") or "").strip()
        if not section:
            return
        authored_section = section

        # An authored URL ends the guessing. It costs no search, cannot be
        # mismatched, and is the same footing planning_links has always stood
        # on: design.md 1.4 bars the MODEL from producing a URL, not the
        # human. Worth having because a catalogue's titles and slugs
        # disagree -- AllTrails' own title for Section B names Callahan's and
        # Ashland while its slug names Highway 5 and Highway 140, so strict
        # matching rejects the page under either phrasing.
        authored_url = str((dest or {}).get("trail_url", "") or "").strip()
        # (Reached only with a section name in hand -- see above.)
        if authored_url:
            getting_here["trail_url"] = authored_url
            getting_here["trail_label"] = authored_section or (
                f"{trail_name}: {origin_name} to {dest_name}"
            )
            self._log_decision(
                kind="leg_trail",
                dest_name=dest_name,
                item_name=section,
                reason="manifest_supplied",
                message="leg trail link taken from the manifest",
                url=authored_url,
            )
            return
        # Deliberately past the --trails switch. That switch governs trail
        # links for ATTRACTIONS AT a destination -- a priced enrichment, off
        # by default, and on a thru-hike mostly day loops near the resupply
        # town. This is the opposite thing: the trail the traveler is
        # actually on, between two stops, asked for by name in the manifest.
        # Tying them together meant turning the wanted one on dragged the
        # unwanted one with it.
        url = self._search_alltrails_for_trail(
            section, dest_name, allow_when_disabled=True, force_search=True
        )
        if not url:
            self._log_decision(
                kind="leg_trail",
                dest_name=dest_name,
                item_name=section,
                reason="no_trail_link_found",
                message="no AllTrails page matched this leg's section",
            )
            return
        getting_here["trail_url"] = url
        getting_here["trail_label"] = authored_section or (
            f"{trail_name}: {origin_name} to {dest_name}"
        )
        self._log_decision(
            kind="leg_trail",
            dest_name=dest_name,
            item_name=section,
            reason="discovery_completed",
            message="leg trail link",
            url=url,
        )

    def _discover_en_route_stops(
        self,
        ai: dict[str, Any],
        dest_name: str,
        dest_dates: str | None = None,
        origin_name: str = "",
        origin_lat: Any = None,
        origin_lng: Any = None,
        dest_lat: Any = None,
        dest_lng: Any = None,
        dest: dict[str, Any] | None = None,
    ) -> None:
        getting_here = ai.get("getting_here", {}) if isinstance(ai.get("getting_here", {}), dict) else {}

        # GH #68 multi-site grouping: a grouped entry can defer en-route-stop
        # discovery to its group base (configurable; not part of the default
        # base_owned_categories). Additive skip-gate only -- distance/time
        # computation and getting_there route-option discovery below are a
        # different category and are never gated by this check.
        # Whole-category off switch (en_route_stops.enabled). Distinct from
        # the group-level deferral below: this suppresses en-route stops for
        # every destination, so nothing is discovered AND nothing renders.
        # En-route stops were 253 of 301 batch candidate rejections before
        # they moved to Maps links, and remain a priced enrichment rather
        # than part of the core itinerary.
        if getattr(self, "_disable_en_route", False):
            getting_here["en_route_stops"] = []
            ai["getting_here"] = getting_here
            return

        # A booked train, ferry or flight has no roadside to stop at.
        # ai_content clears stops for the same reason, but THIS path harvests
        # its own independently, so clearing there alone left Brussels with a
        # 25km detour to Mechelen on a rail itinerary. Two sources, one rule --
        # the same mistake the trails switch took five attempts to close.
        if self._arrival_is_not_self_driven(dest):
            if getting_here.get("en_route_stops"):
                logger.info(
                    "En-route discovery skipped for '%s': arrival is not by road", dest_name
                )
            getting_here["en_route_stops"] = []
            ai["getting_here"] = getting_here
            return

        en_route_stop_deferred = category_deferred_to_base(
            dest,
            "en_route_stop",
            getattr(self, "_multi_site_base_owned_categories", DEFAULT_BASE_OWNED_CATEGORIES),
        )
        if en_route_stop_deferred:
            self._log_decision(
                kind="en_route_stop",
                dest_name=dest_name,
                item_name="",
                reason="base_owned_category_skipped",
                message="en-route stop discovery skipped for entire destination — category deferred to group base",
            )
            getting_here["en_route_stops"] = []
            ai["getting_here"] = getting_here

        source_mode = str(getattr(self, "_en_route_source", DEFAULT_EN_ROUTE_SOURCE) or DEFAULT_EN_ROUTE_SOURCE)
        stops = (
            []
            if en_route_stop_deferred
            else (getting_here.get("en_route_stops", []) if isinstance(getting_here.get("en_route_stops", []), list) else [])
        )

        # "maps" mode changes URL RESOLUTION only. It must still run the
        # harvest, because this batch does two jobs: it supplies candidate
        # en-route stops (see "Preserve legacy behavior when AI yields no
        # en-route ideas" below) as well as their URLs.
        #
        # The first version of maps mode conflated the two and skipped the
        # batch entirely. The 2026-08-22 run measured the result: en-route
        # stop cards fell from 50 to 12, a 76% content loss, and the
        # geometry pass that assigns route-verified geocodes collapsed with
        # them (35 -> 9 corrections). The apparent "-83% batch candidate
        # rejections" that made the mode look successful was mostly stops
        # ceasing to exist, not resolution improving.
        if source_mode in {"direct_link_batch", "maps"} and not en_route_stop_deferred:
            en_route_seed_names = [
                str(seed or "").strip()
                for seed in ((dest or {}).get("en_route_seeds", []) or [])
                if str(seed or "").strip()
            ]
            stops = self._prioritize_direct_batch_en_route_stops(
                stops, dest_name, dest_dates, origin_name, seed_names=en_route_seed_names
            )
            getting_here["en_route_stops"] = stops
            ai["getting_here"] = getting_here

        if not en_route_stop_deferred:
            seeded_stops = self._ensure_en_route_seed_candidates(stops, dest, dest_name)
            if seeded_stops is not stops:
                stops = seeded_stops
                getting_here["en_route_stops"] = stops
                ai["getting_here"] = getting_here

        if stops:
            address_deduped_stops = self._dedupe_en_route_stops_same_leg_by_shared_address(stops, dest_name)
            if len(address_deduped_stops) != len(stops):
                stops = address_deduped_stops
                getting_here["en_route_stops"] = stops
                ai["getting_here"] = getting_here

        if stops:
            non_duplicate_stops: list[dict[str, Any]] = []
            for stop in stops:
                stop_name = str((stop or {}).get("name", "") or "").strip()
                if stop_name and self._en_route_stop_name_duplicates_destination(stop_name, dest_name):
                    self._log_decision(
                        kind="en_route_stop",
                        dest_name=dest_name,
                        item_name=stop_name,
                        reason="en_route_duplicate_of_destination",
                        message="en-route stop removed: resolved name duplicates the arrival destination itself",
                    )
                    continue
                matched_own_entry = (
                    self._en_route_stop_duplicates_destination_own_list(stop_name, dest) if stop_name else None
                )
                if matched_own_entry:
                    self._log_decision(
                        kind="en_route_stop",
                        dest_name=dest_name,
                        item_name=stop_name,
                        reason="en_route_duplicate_of_destination_own_list",
                        message=(
                            "en-route stop removed: duplicates the destination's own "
                            f"scenic-drive/attraction entry '{matched_own_entry}'"
                        ),
                    )
                    continue
                non_duplicate_stops.append(stop)
            if len(non_duplicate_stops) != len(stops):
                stops = non_duplicate_stops
                getting_here["en_route_stops"] = stops
                ai["getting_here"] = getting_here

        if stops and source_mode in {"direct_link_batch", "maps"}:
            filtered_stops: list[dict[str, Any]] = []
            for stop in stops:
                keep, reason = self._en_route_stop_within_threshold(stop if isinstance(stop, dict) else {})
                stop_name = str((stop or {}).get("name", "") or "")
                if keep:
                    filtered_stops.append(stop)
                    if reason == "seed_threshold_override":
                        self._log_decision(
                            kind="en_route_stop",
                            dest_name=dest_name,
                            item_name=stop_name,
                            reason="seed_threshold_override",
                            message="seeded en-route stop missing detour metadata but retention allowed",
                        )
                    continue
                self._log_decision(
                    kind="en_route_stop",
                    dest_name=dest_name,
                    item_name=stop_name,
                    reason="en_route_threshold_filtered",
                    message=f"en-route stop filtered by threshold ({reason})",
                )
            if len(filtered_stops) != len(stops):
                stops = filtered_stops
                getting_here["en_route_stops"] = stops
                ai["getting_here"] = getting_here

        if stops:
            filtered_generic_stops: list[dict[str, Any]] = []
            for stop in stops:
                stop_name = str((stop or {}).get("name", "") or "").strip()
                stop_url = str((stop or {}).get("url", "") or "").strip()
                has_concrete_url = bool(
                    stop_url
                    and not self._is_google_maps_candidate_url(stop_url)
                    and not self._is_generic_directions_url(stop_url)
                )
                if stop_name and self._is_generic_en_route_stop_title(stop_name) and not has_concrete_url:
                    desc = str((stop or {}).get("description", "") or "").strip()
                    mined = self._extract_named_stops_from_description(desc)
                    if mined:
                        for mined_name in mined:
                            mined_stop = dict(stop)
                            mined_stop["name"] = mined_name
                            mined_stop.pop("url", None)
                            if not str(mined_stop.get("description", "") or "").strip():
                                mined_stop["description"] = desc
                            if not str(mined_stop.get("practical_note", "") or "").strip():
                                mined_stop["practical_note"] = desc
                            if str(mined_stop.get("detour_distance_miles", "") or "").strip() == "":
                                mined_stop["detour_distance_miles"] = self._extract_en_route_detour_miles_from_text(desc)
                            if str(mined_stop.get("detour_time_minutes", "") or "").strip() == "":
                                mined_stop["detour_time_minutes"] = self._extract_en_route_detour_minutes_from_text(desc)
                            logger.info(
                                "  En-route mining: extracted '%s' from generic item '%s'",
                                mined_name, stop_name,
                            )
                            filtered_generic_stops.append(mined_stop)
                        self._log_decision(
                            kind="en_route_stop",
                            dest_name=dest_name,
                            item_name=stop_name,
                            reason="en_route_generic_title_filtered",
                            message=(
                                f"en-route stop filtered (generic heading); {len(mined)} named stops mined"
                            ),
                        )
                        continue
                    filtered_generic_stops.append(stop)
                    continue
                filtered_generic_stops.append(stop)
            if len(filtered_generic_stops) != len(stops):
                stops = filtered_generic_stops
                getting_here["en_route_stops"] = stops
                ai["getting_here"] = getting_here

        if stops:
            route_pruned_stops = self._prune_en_route_stops_by_geometry(
                stops=stops,
                origin_name=origin_name,
                dest_name=dest_name,
                origin_lat=origin_lat,
                origin_lng=origin_lng,
                dest_lat=dest_lat,
                dest_lng=dest_lng,
            )
            if len(route_pruned_stops) != len(stops):
                stops = route_pruned_stops
                getting_here["en_route_stops"] = stops
                ai["getting_here"] = getting_here

        if stops:
            # A second same-leg same-place pass, now that every surviving
            # stop that resolved has a verified geocode_lat/geocode_lng
            # (just persisted above by _prune_en_route_stops_by_geometry) --
            # catches same-place duplicates the earlier address-key pass
            # missed because neither name contained a parseable street
            # address (e.g. two different free-text names that happen to
            # geocode to the same point).
            geocode_deduped_stops = self._dedupe_en_route_stops_same_leg_by_geocode_proximity(stops, dest_name)
            if len(geocode_deduped_stops) != len(stops):
                stops = geocode_deduped_stops
                getting_here["en_route_stops"] = stops
                ai["getting_here"] = getting_here

        if stops:
            # Correct free-text-mined/AI-provided detour distance/time against
            # each stop's own verified geocode (just persisted onto it, above,
            # by _prune_en_route_stops_by_geometry) -- see the block comment
            # above _en_route_stop_geometry_grounded_detour_floor for why this
            # is a safe, geometrically-forced correction to a number, distinct
            # from that method's deliberately-not-auto-rejecting inclusion
            # check.
            origin_point = self._parse_lat_lng(origin_lat, origin_lng)
            dest_point = self._parse_lat_lng(dest_lat, dest_lng)
            if origin_point is not None and dest_point is not None:
                for stop in stops:
                    if not isinstance(stop, dict):
                        continue
                    stop_name = str(stop.get("name", "") or "").strip()
                    prior_miles = stop.get("detour_distance_miles")
                    prior_minutes = stop.get("detour_time_minutes")
                    final_miles, final_minutes, overridden = (
                        self._resolve_en_route_stop_detour_metrics_against_geometry(
                            stop, origin=origin_point, dest=dest_point
                        )
                    )
                    if not overridden:
                        continue
                    stop["detour_distance_miles"] = final_miles
                    stop["detour_time_minutes"] = final_minutes
                    self._log_decision(
                        kind="en_route_stop",
                        dest_name=dest_name,
                        item_name=stop_name,
                        reason="en_route_detour_metrics_geometry_corrected",
                        message=(
                            "en-route detour distance/time replaced with a geometry-grounded "
                            f"value from the stop's own verified coordinates (was "
                            f"miles={prior_miles!r} minutes={prior_minutes!r}, now "
                            f"miles={final_miles!r} minutes={final_minutes!r})"
                        ),
                    )

        resolved_stops: list[dict[str, Any]] = []
        # Two en-route stops must not publish the same link. Each assignment
        # path validates its own choice and none of them looked at what the
        # others had already claimed, so a plausible page could stand in for
        # several different places at once: five Asheville-leg stops shared
        # https://www.knoxvilletn.gov -- a city homepage for a waterfall and a
        # national park -- and Lebanon and Franklin each had a pair sharing one
        # URL across the direct_batch_existing_preserved and discovery_selected
        # paths. Nothing was wrong with either claim alone; only the pair was.
        # Restaurants have had this guard (claimed_restaurant_urls) all along.
        claimed_en_route_urls: set[str] = set()
        for stop in stops:
            stop_name = stop.get("name", "")
            geocoded_lat = stop.get("geocode_lat")
            geocoded_lng = stop.get("geocode_lng")
            has_precise_geocode = isinstance(geocoded_lat, (int, float)) and isinstance(geocoded_lng, (int, float))
            if has_precise_geocode:
                # A coordinate query always resolves to exactly one point, unlike
                # a free-text name/address search, which can fail to resolve
                # precisely even for a real, correctly in-region place
                # (dipstick55 Theme E: "Swasey's Beach doesn't resolve to a
                # single point on Google Maps"). The geocode was already
                # verified as plausibly on-route by
                # _prune_en_route_stops_by_geometry just above, at no extra
                # cost -- prefer it as the fallback everywhere a free-text
                # query would otherwise be used below (maps_url, url, and the
                # final safety pass all route through `fallback_url`).
                fallback_url = f"https://www.google.com/maps/search/?api=1&query={geocoded_lat},{geocoded_lng}"
            else:
                q = self._en_route_maps_fallback_query_text(stop_name, origin_name, dest_name)
                fallback_url = f"https://www.google.com/maps/search/?api=1&query={quote(q)}" if str(q or "").strip() else ""
            existing_maps_url = str(stop.get("maps_url", "") or "").strip()
            if has_precise_geocode:
                stop["maps_url"] = fallback_url
            elif existing_maps_url:
                stop["maps_url"] = existing_maps_url
            elif fallback_url:
                stop["maps_url"] = fallback_url
            else:
                stop.pop("maps_url", None)
            url = None
            if source_mode in {"direct_link_batch", "maps"}:
                existing_url = str(stop.get("url", "") or "").strip()
                if existing_url:
                    cleaned_existing = self._retain_discovered_url(
                        existing_url,
                        stop_name,
                        dest_name,
                        allow_alltrails=False,
                        kind="en_route_stop",
                        # See item2 fix note (docs/design/url-discovery-and-
                        # audit.md, "En-Route Stop Maps-Link Specificity"):
                        # a direct-batch row's own `url` field is sometimes
                        # itself an AI-authored Google Maps search link with
                        # whatever raw query text the model wrote (e.g. just
                        # the bare item name, no destination). Without this,
                        # such a link is flatly rejected here (a real stop
                        # loses its only link) instead of being rebuilt with
                        # this module's own controlled, always-destination-
                        # qualified query text -- the same leniency
                        # `_search_en_route_stop_from_direct_batch`'s own
                        # per-candidate check already grants a few hundred
                        # lines below, now applied uniformly here too.
                        allow_google_maps_search=True,
                    )
                    if cleaned_existing and self._url_already_claimed(
                        cleaned_existing, claimed_en_route_urls
                    ):
                        self._log_decision(
                            kind="en_route_stop",
                            dest_name=dest_name,
                            item_name=stop_name,
                            reason="en_route_url_collision_rejected",
                            message="another en-route stop already claimed this link",
                            url=cleaned_existing,
                        )
                        cleaned_existing = ""
                    if cleaned_existing:
                        claimed_en_route_urls.add(self._collision_key(cleaned_existing))
                        stop["url"] = cleaned_existing
                        stop["_url_assigned_by"] = "direct_batch_existing_preserved"
                        self._log_decision(
                            kind="en_route_stop",
                            dest_name=dest_name,
                            item_name=stop_name,
                            reason="direct_batch_existing_url_preserved",
                            message="en-route link preserved from direct-link batch row",
                            url=cleaned_existing,
                        )
                        self._log_decision(
                            kind="en_route_stop",
                            dest_name=dest_name,
                            item_name=stop_name,
                            reason="discovery_completed",
                            message="en-route link",
                            url=cleaned_existing,
                        )
                        resolved_stops.append(stop)
                        continue
                url = self._search_en_route_stop_from_direct_batch(
                    stop_name,
                    dest_name,
                    str(dest_dates or ""),
                    origin_name,
                )
                if not url and self._direct_batch_is_authoritative():
                    if fallback_url:
                        stop["url"] = fallback_url
                        stop["_url_assigned_by"] = "fallback_after_existing_rejected"
                        self._log_decision(
                            kind="en_route_stop",
                            dest_name=dest_name,
                            item_name=stop_name,
                            reason="direct_batch_source_locked_no_match",
                            message="en-route canonical URL omitted; direct-link batch authoritative had no match",
                        )
                        self._log_decision(
                            kind="en_route_stop",
                            dest_name=dest_name,
                            item_name=stop_name,
                            reason="maps_fallback_assigned",
                            message="en-route fallback maps URL assigned",
                            url=fallback_url,
                        )
                        self._log_decision(
                            kind="en_route_stop",
                            dest_name=dest_name,
                            item_name=stop_name,
                            reason="discovery_completed",
                            message="en-route link",
                            url=fallback_url,
                        )
                        resolved_stops.append(stop)
                    else:
                        stop.pop("url", None)
                        self._log_decision(
                            kind="en_route_stop",
                            dest_name=dest_name,
                            item_name=stop_name,
                            reason="en_route_removed_no_fallback",
                            message="en-route stop removed because no canonical or fallback URL was available",
                        )
                    continue
            if not url:
                url = self._search_first(
                    _build_query_variants(stop_name, dest_name, "attraction stop"),
                    item_name=stop_name,
                    dest_name=dest_name,
                    allow_alltrails=False,
                )
            if url and self._url_already_claimed(url, claimed_en_route_urls):
                self._log_decision(
                    kind="en_route_stop",
                    dest_name=dest_name,
                    item_name=stop_name,
                    reason="en_route_url_collision_rejected",
                    message="another en-route stop already claimed this link",
                    url=url,
                )
                url = None
            if url:
                claimed_en_route_urls.add(self._collision_key(url))
                stop["url"] = url
                stop["_url_assigned_by"] = "discovery_selected"
                resolved_stops.append(stop)
            else:
                if fallback_url:
                    stop["url"] = fallback_url
                    stop["_url_assigned_by"] = "fallback_no_url_found"
                    self._log_decision(
                        kind="en_route_stop",
                        dest_name=dest_name,
                        item_name=stop_name,
                        reason="maps_fallback_assigned",
                        message="en-route fallback maps URL assigned",
                        url=fallback_url,
                    )
                    resolved_stops.append(stop)
                else:
                    stop.pop("url", None)
                    self._log_decision(
                        kind="en_route_stop",
                        dest_name=dest_name,
                        item_name=stop_name,
                        reason="en_route_removed_no_fallback",
                        message="en-route stop removed because no canonical or fallback URL was available",
                    )
            self._log_decision(
                kind="en_route_stop",
                dest_name=dest_name,
                item_name=stop_name,
                reason="discovery_completed",
                message="en-route link",
                url=str(stop.get("url", "") or ""),
            )

        if len(resolved_stops) != len(stops):
            getting_here["en_route_stops"] = resolved_stops
            ai["getting_here"] = getting_here

        # Safety pass: guarantee every surviving stop has a url regardless of which
        # discovery branch handled it (avoids silent no-link on any code path).
        for stop in getting_here.get("en_route_stops", []) or []:
            if not str(stop.get("url", "") or "").strip():
                sn = str(stop.get("name", "") or "").strip()
                if sn:
                    geocoded_lat = stop.get("geocode_lat")
                    geocoded_lng = stop.get("geocode_lng")
                    if isinstance(geocoded_lat, (int, float)) and isinstance(geocoded_lng, (int, float)):
                        stop["url"] = f"https://www.google.com/maps/search/?api=1&query={geocoded_lat},{geocoded_lng}"
                        stop["_url_assigned_by"] = "geocode_coordinate"
                    else:
                        q = self._en_route_maps_fallback_query_text(sn, origin_name, dest_name)
                        if q:
                            stop["url"] = f"https://www.google.com/maps/search/?api=1&query={quote(q)}"
                            stop["_url_assigned_by"] = "maps_query_rebuilt"
                        logger.warning("  En-route safety fallback assigned url for '%s'", sn)

        # Derive distance/time from the actual route rather than AI-generated estimates.
        self._update_route_distance_and_time(
            ai=ai,
            getting_here=getting_here,
            origin_name=origin_name,
            dest_name=dest_name,
            origin_lat=origin_lat,
            origin_lng=origin_lng,
            dest_lat=dest_lat,
            dest_lng=dest_lng,
            dest=dest,
        )

        getting_there = ai.get("getting_there", {}) if isinstance(ai.get("getting_there", {}), dict) else {}
        for option in getting_there.get("route_options", []) or []:
            option_name = str(option.get("title", "") or option.get("name", "") or "").strip()
            if not option_name:
                continue
            url = self._search_first(
                _build_query_variants(option_name, dest_name, "scenic drive byway route"),
                item_name=option_name,
                dest_name=dest_name,
                allow_alltrails=False,
            )
            if url:
                option["url"] = url
            else:
                option.pop("url", None)
            self._log_decision(
                kind="getting_there_route_option",
                dest_name=dest_name,
                item_name=option_name,
                reason="discovery_completed" if url else "no_canonical_url",
                message="departure route option link",
                url=(url or ""),
            )

    @staticmethod
    def _parse_lat_lng(lat: Any, lng: Any) -> tuple[float, float] | None:
        try:
            lat_f = float(lat)
            lng_f = float(lng)
        except (TypeError, ValueError):
            return None
        if not (-90.0 <= lat_f <= 90.0 and -180.0 <= lng_f <= 180.0):
            return None
        return lat_f, lng_f

    @staticmethod
    def _haversine_miles(a: tuple[float, float], b: tuple[float, float]) -> float:
        lat1, lon1 = a
        lat2, lon2 = b
        r = 3958.8
        dlat = radians(lat2 - lat1)
        dlon = radians(lon2 - lon1)
        s1 = radians(lat1)
        s2 = radians(lat2)
        h = (0.5 - cos(dlat) / 2.0) + cos(s1) * cos(s2) * (0.5 - cos(dlon) / 2.0)
        h = min(1.0, max(0.0, h))
        return 2.0 * r * asin(sqrt(h))

    @staticmethod
    def _route_progress_ratio(
        *,
        origin: tuple[float, float],
        dest: tuple[float, float],
        point: tuple[float, float],
    ) -> float | None:
        mid_lat = radians((origin[0] + dest[0]) / 2.0)
        scale = cos(mid_lat)
        ox, oy = origin[1] * scale, origin[0]
        dx, dy = dest[1] * scale, dest[0]
        px, py = point[1] * scale, point[0]
        abx = dx - ox
        aby = dy - oy
        denom = (abx * abx) + (aby * aby)
        if denom <= 1e-12:
            return None
        apx = px - ox
        apy = py - oy
        return ((apx * abx) + (apy * aby)) / denom

    @staticmethod
    def _route_perpendicular_distance_miles(
        *,
        origin: tuple[float, float],
        dest: tuple[float, float],
        point: tuple[float, float],
    ) -> float | None:
        """How far `point` sits off to the side of the straight origin->dest
        line, in miles -- the check _route_progress_ratio's callers were
        missing entirely. Progress ratio alone only says "this point's
        projection falls between the two endpoints along the route's
        general direction" -- it says nothing about lateral distance, so a
        point 50+ miles off the actual corridor (e.g. Lake Powell/Hite
        Crossing sitting well south of a Torrey->Moab leg, while Sego Canyon
        sits well northeast near the Colorado line) can still pass with a
        "reasonable" progress value despite being geographically
        incompatible with any single sane route through the other stops.
        Same equirectangular-ish scaling as _route_progress_ratio (consistent
        distortion for both, cancels out at the latitudes this app operates
        at) so a mile figure stays roughly proportionate; converted to real
        miles via a local degrees-latitude-to-miles constant (69.0) applied
        after the perpendicular offset is computed in scaled-degree space.
        """
        mid_lat = radians((origin[0] + dest[0]) / 2.0)
        scale = cos(mid_lat)
        ox, oy = origin[1] * scale, origin[0]
        dx, dy = dest[1] * scale, dest[0]
        px, py = point[1] * scale, point[0]
        abx = dx - ox
        aby = dy - oy
        ab_len = sqrt((abx * abx) + (aby * aby))
        if ab_len <= 1e-9:
            return None
        apx = px - ox
        apy = py - oy
        # Magnitude of the cross product / |AB| = perpendicular distance from
        # point to the infinite line through A and B, in the same scaled
        # degree units _route_progress_ratio's dot product uses.
        cross = (apx * aby) - (apy * abx)
        perpendicular_degrees = abs(cross) / ab_len
        return perpendicular_degrees * 69.0

    # Generic place-designation words that commonly co-occur with an
    # unrelated place's real name in Nominatim/OSM data (e.g. two entirely
    # different towns can each have their own "Historic District"). These
    # must not count as evidence of a name match on their own -- only the
    # non-generic "anchor" words carry real identity, mirroring the
    # generic_trail_tokens carve-out in _alltrails_slug_matches_item.
    _GEOCODE_GENERIC_DESIGNATION_TOKENS = frozenset({
        "historic", "historical", "district", "downtown", "village",
        "town", "area", "neighbourhood", "neighborhood",
    })

    # A "waterway" (river/stream/canal) Nominatim result whose own bounding
    # box spans more than this many miles diagonally is a major, multi-county
    # river system, not a short local creek -- see
    # _geocode_result_is_oversized_waterway's docstring for the real,
    # live-measured numbers that set this threshold (Virgin River ~114 mi vs.
    # Willis Creek ~1-11 mi).
    _GEOCODE_OVERSIZED_WATERWAY_SPAN_MILES = 25.0

    @classmethod
    def _geocode_result_is_oversized_waterway(cls, result: dict[str, Any] | None) -> bool:
        """True when `result` is a Nominatim "waterway" (river/stream/canal)
        feature whose own bounding box is too large to plausibly stand in
        for a single point-of-interest.

        Real case (dipstick75, St. George -> Zion leg): en-route stop
        "Virgin River Petroglyph Site" has no Nominatim entry under its full
        name (verified live), so _geocode_en_route_stop_for_route's
        progressive-truncation fallback (see _en_route_stop_name_truncations)
        retried with "Virgin River Petroglyph" (still no match) and then
        "Virgin River" -- which DOES resolve, to OSM relation 10605393, the
        Virgin River itself: `{"class": "waterway", "type": "river", "name":
        "Virgin River", "lat": "36.9670932", "lon": "-113.7249242",
        "boundingbox": ["36.1457610", "37.2935220", "-114.4173887",
        "-112.9404080"]}` (live Nominatim response, 2026-08-18) -- a point
        that happens to fall in Mohave County, Arizona, ~12 mi from St.
        George and nowhere near the real BLM petroglyph site the stop
        actually refers to. This slipped past `_geocode_result_name_plausible`
        because the anchor-overlap check compares against the ORIGINAL,
        untruncated query name -- "Virgin River" is, by construction of the
        truncation itself, always a literal token subset of "Virgin River
        Petroglyph Site", so the overlap check ("virgin"/"river" shared)
        trivially passes no matter how much of the query's real identity
        ("Petroglyph Site") was dropped to reach that match. This is a
        genuinely different gap from the Rockville/Grafton bug the anchor-
        overlap check was built for: that case had NO shared tokens at all
        between two distinctly-named places; this case has near-total token
        overlap because the "wrong" result's name is textually contained in
        the query, and no name-overlap heuristic can ever catch that.

        A bare "reject waterway-class matches" rule was tried and rejected:
        the module's own documented, intentional truncation-recovery case
        for "Willis Creek Slot Canyon Trailhead" -> "Willis Creek" *also*
        resolves to a `class: waterway` result (live-verified, both real
        candidate streams near Bryce Canyon: bounding boxes ~10.8 mi and
        ~1.1 mi diagonal) -- rejecting on class alone would silently
        reintroduce the exact zigzag-waypoint bug that truncation recovery
        was built to fix (see test_geocode_en_route_stop_recovers_real_
        landmark_behind_descriptive_suffix). The real, measurable difference
        between the two cases is *scale*: Willis Creek is a short local
        stream (bounding box a few miles across at most); the Virgin River
        relation Nominatim returns spans the entire river system (~114 mi
        diagonal, live-measured) -- while a bare river/stream *name* can't
        distinguish "the specific creek this trailhead sits on" from "an
        entire regional river", its own bounding-box size can: a "waterway"
        match whose own extent already exceeds any plausible en-route-stop
        detour is positive evidence it's an aggregate/coarse feature, not
        the specific site queried for.

        Fails open (returns False, i.e. "not oversized") whenever
        `boundingbox` is missing or malformed -- this is an additional
        rejection signal layered on top of the anchor-overlap check, not a
        replacement for it, and every existing geocode mock/test predates
        this field, so absence must never manufacture a rejection.
        """
        if not isinstance(result, dict):
            return False
        if str(result.get("class") or "").strip().lower() != "waterway":
            return False
        bbox = result.get("boundingbox")
        if not isinstance(bbox, (list, tuple)) or len(bbox) != 4:
            return False
        try:
            min_lat, max_lat, min_lon, max_lon = (float(v) for v in bbox)
        except (TypeError, ValueError):
            return False
        corner_a = cls._parse_lat_lng(min_lat, min_lon)
        corner_b = cls._parse_lat_lng(max_lat, max_lon)
        if corner_a is None or corner_b is None:
            return False
        return cls._haversine_miles(corner_a, corner_b) > cls._GEOCODE_OVERSIZED_WATERWAY_SPAN_MILES

    @classmethod
    def _geocode_result_name_plausible(cls, query_name: str, result: dict[str, Any] | None) -> bool:
        """Reject a Nominatim result whose own place name shares no
        distinguishing token with the geocoded query -- catches free-text
        search fuzzy-matching onto a completely different, unrelated place.

        Real case (dipstick69): en-route stop "Rockville Historic District"
        (a real Rockville, UT designation with no distinctly-tagged OSM
        entry) was geocoded via a route-viewbox-restricted Nominatim search
        to (37.166804, -113.0864502) -- which is actually the neighbouring
        "Grafton Historic DIstrict" entry (a real, separate en-route stop
        on the same leg, ~3 road miles away). The existing distance-from-
        route-midpoint sanity check doesn't catch this because Grafton is
        well within the route viewbox/radius; only a name check does.

        A pure "shares no token" check is too weak here: "Rockville
        Historic District" and "Grafton Historic DIstrict" already share
        "historic"/"district", so the check must discount those generic
        designation words and require overlap on each side's real
        identifying ("anchor") word(s) instead -- "rockville" vs "grafton"
        share nothing. Conversely a legitimate case like "Sunrise Point"
        resolving to a result named "Sunrise Point Overlook" must still
        pass: only "point" is generic there (already excluded by the
        shared _significant_tokens stop-word list), so "sunrise" anchors
        both sides.

        Also rejects an oversized "waterway" match -- see
        _geocode_result_is_oversized_waterway's docstring (dipstick75:
        "Virgin River Petroglyph Site" truncation-recovered onto the Virgin
        River itself, a different, distinct failure mode from Rockville/
        Grafton that pure name-token overlap structurally cannot catch,
        since a truncated query's successful match is -- by construction --
        always a token subset of the original name).
        """
        query_tokens = set(cls._significant_tokens(query_name))
        if not query_tokens:
            return True
        if not isinstance(result, dict):
            return True

        result_name = str(result.get("name") or "").strip()
        if not result_name:
            display_name = str(result.get("display_name") or "").strip()
            # Only the result's own place name (the first component of
            # display_name), never the full address hierarchy -- the
            # containing town/county/state in display_name can coincidentally
            # contain the query's own place-name token (Grafton's entry sits
            # inside "Rockville, Washington County, Utah", so checking the
            # full string would let "rockville" match through the address
            # rather than the actual (different) place being returned).
            result_name = display_name.split(",", 1)[0]
        result_tokens = set(cls._significant_tokens(result_name))
        if not result_tokens:
            return True

        generic = cls._GEOCODE_GENERIC_DESIGNATION_TOKENS
        query_anchors = query_tokens - generic
        if not query_anchors:
            # Query itself is only generic designation words -- nothing to
            # anchor on, so fall back to plain overlap rather than rejecting.
            anchor_overlap = bool(query_tokens & result_tokens)
        else:
            anchor_overlap = bool(query_anchors & result_tokens)
        if not anchor_overlap:
            return False
        return not cls._geocode_result_is_oversized_waterway(result)

    def _geocode_en_route_stop_for_route(
        self,
        stop_name: str,
        *,
        origin_name: str,
        dest_name: str,
        origin: tuple[float, float] | None = None,
        dest: tuple[float, float] | None = None,
    ) -> tuple[float, float] | None:
        name = str(stop_name or "").strip()
        if not name:
            return None
        cache = getattr(self, "_en_route_stop_geocode_cache", None)
        if not isinstance(cache, dict):
            cache = {}
            self._en_route_stop_geocode_cache = cache
        key = f"{name.lower()}|{str(origin_name or '').lower()}|{str(dest_name or '').lower()}"
        if key in cache:
            return cache[key]

        session = getattr(getattr(self, "_url_validator", None), "session", None)
        if session is None:
            cache[key] = None
            return None

        # Nominatim's free-text `q` param does not understand "near X" phrasing
        # (it always returns zero results for it) and a bare "Name, USA" query
        # resolves common place names (e.g. "Red Canyon") to whichever match
        # ranks highest by Nominatim's own global "importance" score -- which is
        # frequently a same-named place hundreds of miles from the actual route.
        # A viewbox biased to the route's own origin/destination disambiguates
        # correctly without needing exact query phrasing.
        viewbox_params: dict[str, Any] = {}
        if origin is not None and dest is not None:
            lat_pad = max(0.5, abs(origin[0] - dest[0]) * 0.25)
            lng_pad = max(0.5, abs(origin[1] - dest[1]) * 0.25)
            min_lat = min(origin[0], dest[0]) - lat_pad
            max_lat = max(origin[0], dest[0]) + lat_pad
            min_lng = min(origin[1], dest[1]) - lng_pad
            max_lng = max(origin[1], dest[1]) + lng_pad
            viewbox_params = {
                "viewbox": f"{min_lng},{max_lat},{max_lng},{min_lat}",
                "bounded": 1,
            }

        # An unrestricted (non-viewbox) fallback query can still match a
        # same-named place clear across the country (e.g. a "Glendale Town
        # Park" hundreds of miles away) when nothing exists in-region under
        # that exact query text. Sanity-bound any such match against the
        # route itself so a wrong-region hit is rejected rather than accepted.
        sanity_radius_miles: float | None = None
        route_midpoint: tuple[float, float] | None = None
        if origin is not None and dest is not None:
            leg_miles = self._haversine_miles(origin, dest)
            sanity_radius_miles = max(150.0, leg_miles * 1.5)
            route_midpoint = ((origin[0] + dest[0]) / 2.0, (origin[1] + dest[1]) / 2.0)

        queries = [f"{name}, {dest_name}", name, f"{name}, USA"]
        # Each (query, viewbox-mode) combination is a separate throttled
        # request (1.1s apart). The full cross product (3 queries x 2 viewbox
        # modes = up to 6) was rarely needed in practice -- the viewbox-biased
        # search on the first two query variants, plus one unrestricted
        # fallback, covers the same ground the existing tests exercise at
        # roughly half the worst-case cost per unresolved stop.
        if viewbox_params:
            attempts: list[tuple[str, bool]] = [(queries[0], True)]
            if len(queries) > 1:
                attempts.append((queries[1], True))
            attempts.append((queries[0], False))
        else:
            attempts = [(q, False) for q in queries]

        # AI-generated en-route stop names frequently tack a descriptive
        # feature word onto a real, well-known landmark -- e.g. "Cedar
        # Breaks National Monument Rim View", "Coral Pink Sand Dunes State
        # Park Boardwalk", "Willis Creek Slot Canyon Trailhead" (all real
        # dipstick59 stops on the Zion -> Bryce leg). Nominatim's free-text
        # search requires (approximately) every significant word to match
        # something in its index, so these exact strings return zero
        # results even though the landmark itself ("Cedar Breaks National
        # Monument", "Coral Pink Sand Dunes State Park", "Willis Creek")
        # geocodes cleanly. When that happens the stop silently gets no
        # route_progress_ratio and sorts to the very end of the waypoint
        # list (see the "unknown ratio sorts last" fix), which scrambles
        # the real visiting order into a zigzag between geographic
        # clusters instead of merely misplacing one stop. Progressively
        # dropping the last word and retrying recovers the real
        # coordinates without a hardcoded list of "known bad suffix
        # words" that would need constant upkeep.
        for truncated in self._en_route_stop_name_truncations(name):
            attempts.append((truncated, bool(viewbox_params)))

        for query, use_viewbox in attempts:
            q = str(query or "").strip()
            if not q:
                continue
            self._respect_nominatim_rate_limit()
            try:
                params = {
                    "q": q,
                    "format": "json",
                    "limit": 1,
                    "countrycodes": "us",
                }
                if use_viewbox:
                    params.update(viewbox_params)
                response = session.get(
                    "https://nominatim.openstreetmap.org/search",
                    params=params,
                    headers={"User-Agent": "RoadTripGenerator-URLDiscovery/1.0"},
                    timeout=8,
                )
                if getattr(response, "status_code", None) == 429:
                    # Back off the shared clock and give up for this stop
                    # rather than burning through the remaining query/
                    # viewbox combinations while rate-limited -- each
                    # destination pair can call this once per stop, so
                    # retrying every combination here risks compounding
                    # the lockout across the whole run.
                    self._nominatim_last_request_ts = time.monotonic() + 5.0
                    logger.info("Nominatim rate-limited (429) geocoding en-route stop '%s'", name)
                    cache[key] = None
                    return None
                response.raise_for_status()
                rows = response.json()
                if not isinstance(rows, list) or not rows:
                    continue
                first = rows[0] if isinstance(rows[0], dict) else {}
                parsed = self._parse_lat_lng(first.get("lat"), first.get("lon"))
                if parsed is None:
                    continue
                if not self._geocode_result_name_plausible(name, first):
                    # A real, well-tagged place, just not the one queried for
                    # (e.g. free-text search fuzzy-matched onto a completely
                    # different named place sharing only generic designation
                    # words). Keep trying the remaining query/viewbox
                    # combinations rather than giving up on this stop
                    # entirely -- same pattern as the out-of-region rejection
                    # below.
                    continue
                if (
                    not use_viewbox
                    and route_midpoint is not None
                    and sanity_radius_miles is not None
                    and self._haversine_miles(route_midpoint, parsed) > sanity_radius_miles
                ):
                    # This is a real, named-place hit -- not an absence of data --
                    # that just happens to sit far outside any plausible reading
                    # of this route (e.g. a same-named trail/park hundreds of
                    # miles away in another state). That is positive evidence the
                    # stop is a wrong-geography hallucination, not merely
                    # "unconfirmed" -- record it distinctly from the plain-miss
                    # case below so _prune_en_route_stops_by_geometry can drop
                    # the stop outright instead of just deprioritizing it for
                    # waypoint ordering (see dipstick55 Theme A: "Stan's Overlook
                    # Trail, Snoqualmie, WA" / "Looking Glass Rock, Brevard, NC"
                    # appearing as en-route stops for unrelated UT/CO routes).
                    self._mark_en_route_stop_geocode_rejected_out_of_region(key)
                    continue
                cache[key] = parsed
                self._mark_persistent_cache_dirty()
                return parsed
            except Exception:
                continue

        cache[key] = None
        return None

    @staticmethod
    def _en_route_stop_name_truncations(name: str, *, min_words: int = 2, max_variants: int = 3) -> list[str]:
        """Progressively drop the last word of a multi-word place name.

        Used as a last-resort geocoding fallback: see the call site in
        _geocode_en_route_stop_for_route for why this recovers real
        landmarks whose AI-generated name has a descriptive suffix
        Nominatim's free-text search can't match ("... Rim View", "...
        Boardwalk", "... Trailhead"). Capped at a handful of variants and a
        floor of min_words so this can't degrade into a single, overly
        generic word (e.g. just "Park") that would risk a wrong-region
        false match.
        """
        words = str(name or "").split()
        variants: list[str] = []
        while len(words) > min_words and len(variants) < max_variants:
            words = words[:-1]
            candidate = " ".join(words).strip()
            if candidate:
                variants.append(candidate)
        return variants

    def _mark_en_route_stop_geocode_rejected_out_of_region(self, cache_key: str) -> None:
        if not hasattr(self, "_en_route_stop_geocode_rejected_out_of_region"):
            self._en_route_stop_geocode_rejected_out_of_region = set()
        self._en_route_stop_geocode_rejected_out_of_region.add(cache_key)

    def _en_route_stop_geocode_was_rejected_out_of_region(self, stop_name: str, *, origin_name: str, dest_name: str) -> bool:
        """True only when a prior geocode attempt for this exact stop found a
        real named-place match that was rejected specifically for being
        implausibly far from the route -- not merely "no data available"."""
        rejected = getattr(self, "_en_route_stop_geocode_rejected_out_of_region", None)
        if not rejected:
            return False
        key = f"{str(stop_name or '').strip().lower()}|{str(origin_name or '').lower()}|{str(dest_name or '').lower()}"
        return key in rejected

    def _respect_nominatim_rate_limit(self) -> None:
        """Nominatim's usage policy caps requests at 1/sec. This function can
        issue several requests per stop (query-variant x viewbox-mode
        combinations) across many stops per destination pair, so it needs its
        own throttle rather than relying on the caller to pace calls.

        Destination discovery runs on a multi-thread pool (see
        discover_all's ThreadPoolExecutor), so multiple threads can reach
        this method concurrently. Without a lock, each one independently
        reads the same last-request timestamp, decides it's safe, and fires
        at the same time -- the check-sleep-write sequence must be atomic or
        the 1 req/sec policy isn't actually enforced under concurrency, it
        just looks like it is from any single thread's perspective.
        """
        if not hasattr(self, "_nominatim_rate_limit_lock"):
            self._nominatim_rate_limit_lock = Lock()
        with self._nominatim_rate_limit_lock:
            last_ts = getattr(self, "_nominatim_last_request_ts", 0.0)
            elapsed = time.monotonic() - last_ts
            min_interval = 1.1
            if elapsed < min_interval:
                time.sleep(min_interval - elapsed)
            self._nominatim_last_request_ts = time.monotonic()

    def _prune_en_route_stops_by_geometry(
        self,
        *,
        stops: list[dict[str, Any]],
        origin_name: str,
        dest_name: str,
        origin_lat: Any,
        origin_lng: Any,
        dest_lat: Any,
        dest_lng: Any,
    ) -> list[dict[str, Any]]:
        origin = self._parse_lat_lng(origin_lat, origin_lng)
        dest = self._parse_lat_lng(dest_lat, dest_lng)
        if origin is None or dest is None:
            for stop in stops:
                if isinstance(stop, dict):
                    stop["route_waypoint_eligible"] = False
            return stops

        leg_miles = self._haversine_miles(origin, dest)
        if leg_miles <= 1.0:
            for stop in stops:
                if isinstance(stop, dict):
                    stop["route_waypoint_eligible"] = False
            return stops

        kept: list[dict[str, Any]] = []
        for stop in stops:
            stop_name = str((stop or {}).get("name", "") or "").strip()
            if not stop_name:
                if isinstance(stop, dict):
                    stop["route_waypoint_eligible"] = False
                kept.append(stop)
                continue

            stop_coords = self._geocode_en_route_stop_for_route(
                stop_name,
                origin_name=origin_name,
                dest_name=dest_name,
                origin=origin,
                dest=dest,
            )
            if stop_coords is None:
                # Distinguish "we have no data either way" (lenient: fall back to
                # the detour-metadata heuristic, keep the stop) from "geocoding
                # found a real named-place match for this exact name, but it was
                # implausibly far from the route" (positive evidence of a
                # wrong-geography hallucination -- drop the stop outright rather
                # than merely excluding it from waypoint ordering).
                if self._en_route_stop_geocode_was_rejected_out_of_region(
                    stop_name, origin_name=origin_name, dest_name=dest_name
                ):
                    self._log_decision(
                        kind="en_route_stop",
                        dest_name=dest_name,
                        item_name=stop_name,
                        reason="en_route_geometry_filtered_wrong_region",
                        message="en-route stop removed: only resolvable to a place far outside the route",
                    )
                    continue
                if isinstance(stop, dict):
                    keep_waypoint = False
                    if not self._is_generic_en_route_stop_title(stop_name):
                        within_threshold, _reason = self._en_route_stop_within_threshold(stop)
                        keep_waypoint = bool(within_threshold)
                    stop["route_waypoint_eligible"] = keep_waypoint
                kept.append(stop)
                continue

            progress = self._route_progress_ratio(origin=origin, dest=dest, point=stop_coords)
            if progress is None:
                if isinstance(stop, dict):
                    keep_waypoint = False
                    if not self._is_generic_en_route_stop_title(stop_name):
                        within_threshold, _reason = self._en_route_stop_within_threshold(stop)
                        keep_waypoint = bool(within_threshold)
                    stop["route_waypoint_eligible"] = keep_waypoint
                kept.append(stop)
                continue

            origin_to_stop = self._haversine_miles(origin, stop_coords)
            beyond_buffer = max(10.0, leg_miles * 0.12)
            # Remove stops that are at or past the destination: these belong as
            # destination attractions, not as route waypoints.
            at_destination = progress >= 0.93 and origin_to_stop >= leg_miles * 0.88
            # Defense-in-depth against the progress-ratio test above missing a
            # stop that geocodes essentially on top of the destination itself
            # (e.g. a same-named feature inside the destination park, or a
            # geocoder match that snaps to the destination when the specific
            # POI isn't in its index): a stop within a couple of miles of the
            # destination's own coordinates is never a genuine en-route
            # detour, regardless of where the ratio math places it along the
            # origin->destination line. See the Bryce -> Capitol Reef "Capitol
            # Reef National Park" appearing as its own waypoint entry right
            # before the real destination pin.
            dest_to_stop = self._haversine_miles(dest, stop_coords)
            at_destination = at_destination or dest_to_stop <= 2.0
            if at_destination or (progress > 1.06 and origin_to_stop > leg_miles + beyond_buffer):
                self._log_decision(
                    kind="en_route_stop",
                    dest_name=dest_name,
                    item_name=stop_name,
                    reason="en_route_geometry_filtered",
                    message="en-route stop filtered as beyond destination leg",
                )
                continue

            # Lateral (perpendicular-to-the-route) distance -- diagnostic only,
            # NOT an auto-reject filter. Progress ratio alone only says the
            # stop's projection falls between the two endpoints along the
            # route's general direction; it says nothing about how far off
            # to the side the stop actually is, which is how a real
            # dipstick62 case slipped through: on a single Torrey->Moab leg,
            # "Lake Powell / Hite Crossing" (37.9 mi off the straight line)
            # and "Sego Canyon" (progress ~0.98, essentially at Moab itself
            # -- already caught by the at-destination check above once real
            # coordinates resolve, a separate mechanism from this one) both
            # rendered as waypoints on the same leg, and Google Maps'
            # resulting directions came out to 706 miles / 13h50m for what
            # should be roughly 140 miles / 2.5-3 hours.
            #
            # A hard distance cutoff was tried and reverted: the real,
            # commonly-used I-70 route from this same Capitol Reef/Torrey
            # area through Green River to Moab -- an established-legitimate
            # stop covered by test_discover_en_route_stops_uses_geocoded_
            # coordinates_for_maps_url ("Swasey's Beach", dipstick55 Theme E
            # precedent) -- sits 35.4 mi off the same straight line, only
            # 2.5 mi closer than Lake Powell. Real Utah highways bend that
            # far around terrain; no straight-line distance threshold can
            # separate "genuinely off-corridor" from "the real road just
            # isn't straight" without actual road-network routing data,
            # which this codebase does not have (Nominatim gives point
            # geocodes, not routes). Logging the figure here (not acting on
            # it) so real distribution data can accumulate across runs
            # before anyone tries to pick a safe threshold again.
            perpendicular_miles = self._route_perpendicular_distance_miles(
                origin=origin, dest=dest, point=stop_coords
            )
            if perpendicular_miles is not None and perpendicular_miles > 25.0:
                self._log_decision(
                    kind="en_route_stop",
                    dest_name=dest_name,
                    item_name=stop_name,
                    reason="en_route_far_off_straight_line_kept",
                    message=(
                        f"en-route stop is {perpendicular_miles:.0f} mi off the straight "
                        "origin->destination line -- kept (diagnostic only, see code comment "
                        "on why this isn't auto-rejected)"
                    ),
                )

            if isinstance(stop, dict):
                stop["route_waypoint_eligible"] = True
                stop["route_progress_ratio"] = progress
                # Persist the verified coordinates so the maps_url built later in
                # _discover_en_route_stops can point at this exact point instead
                # of a free-text search -- see dipstick55 Theme E ("Swasey's
                # Beach doesn't resolve to a single point on Google Maps"): a
                # loosely-defined BLM beach/campsite is real and correctly
                # in-region, but a free-text query for it doesn't reliably
                # resolve to one place in Google's own index. A coordinate
                # query (from the same Nominatim lookup already done for route
                # pruning, at no extra cost) always resolves to exactly one
                # point.
                stop["geocode_lat"] = stop_coords[0]
                stop["geocode_lng"] = stop_coords[1]
            kept.append(stop)

        return kept

    # ── Scenic Drives ────────────────────────────────────────────────────────

    _GENERIC_SCENIC_DRIVE_NAME_TOKENS = frozenset({
        "scenic", "drive", "road", "route", "byway", "day", "trip", "loop", "tour", "highway",
    })

    @classmethod
    def _nps_deterministic_scenic_drive_page_matches(cls, drive_name: str, page_text: str | None) -> bool:
        distinctive_tokens = [
            t for t in cls._significant_tokens(drive_name)
            if t not in cls._GENERIC_SCENIC_DRIVE_NAME_TOKENS
        ]
        if not distinctive_tokens:
            # A generically-named drive ("Scenic Drive", "Park Loop Road") has
            # nothing to distinguish it from the park's own page -- trust it.
            return True
        lower_text = str(page_text or "").lower()
        if not lower_text:
            return False
        return any(token in lower_text for token in distinctive_tokens)

    @staticmethod
    def _scenic_drive_search_name(drive_name: str) -> str:
        """Strip a trailing AI-added activity-type descriptor ('Day Trip') that
        is not part of the road's actual name, so search queries target the
        real name ('Notom-Bullfrog Road') instead of a quoted phrase no real
        source uses verbatim ('Notom-Bullfrog Road Day Trip'), which wastes
        the exact-phrase query attempts before falling through to a looser
        variant."""
        cleaned = re.sub(r"\s+day\s+trip\s*$", "", str(drive_name or ""), flags=re.IGNORECASE).strip()
        return cleaned or str(drive_name or "").strip()

    def _discover_scenic_drives(self, dest: dict[str, Any], dest_name: str, nps_code: str | None = None) -> None:
        # GH #68 multi-site grouping: a grouped entry can defer scenic-drive
        # discovery to its group base. Per the design doc's open question
        # #4 ("gate both" lean), this also clears any AI-generated
        # scenic_drives content already attached to `dest` by
        # ai_content.py.generate_destination_content (which always runs
        # before discover_all in main.py's pipeline) -- a scenic-drive text
        # block with no linked URL reads worse than no block at all. No
        # separate ai_content.py change needed: this is the one place both
        # layers converge, since dest["scenic_drives"] is the same list
        # object either way.
        if category_deferred_to_base(
            dest,
            "scenic_drive",
            getattr(self, "_multi_site_base_owned_categories", DEFAULT_BASE_OWNED_CATEGORIES),
        ):
            if dest.get("scenic_drives"):
                self._log_decision(
                    kind="scenic_drive",
                    dest_name=dest_name,
                    item_name="",
                    reason="base_owned_category_skipped",
                    message="scenic drive discovery skipped for entire destination — category deferred to group base",
                )
            dest["scenic_drives"] = []
            return
        for drive in dest.get("scenic_drives", []):
            drive_name = drive.get("title", "")

            # For NPS parks, the *park's own* scenic-drive page follows a
            # deterministic pattern -- but a park can have several distinctly
            # named drives (e.g. Capitol Reef's paved "Scenic Drive" vs. the
            # separate "Notom-Bullfrog Road" backcountry route), and every one
            # of them would get this same generic URL if we only checked that
            # it returns HTTP 200. Verify the page text is actually about
            # *this* drive (or the drive's name is itself generic enough that
            # there's nothing to distinguish) before accepting the shortcut.
            nps_drive_url: str | None = None
            if nps_code:
                candidate = f"https://www.nps.gov/{nps_code}/planyourvisit/scenic-drive.htm"
                ok, _status, page_text = self._fetch_page_text(candidate, timeout=6)
                if ok and self._nps_deterministic_scenic_drive_page_matches(drive_name, page_text):
                    nps_drive_url = candidate
                    self._log_decision(
                        kind="scenic_drive",
                        dest_name=dest_name,
                        item_name=drive_name,
                        reason="nps_deterministic_accepted",
                        message="scenic drive link (NPS deterministic)",
                        url=nps_drive_url,
                    )

            url = nps_drive_url or self._search_first(
                _build_query_variants(self._scenic_drive_search_name(drive_name), dest_name, "scenic drive viewpoint"),
                item_name=drive_name,
                dest_name=dest_name,
                allow_alltrails=False,
            )
            # Keep scenic-drive/day-trip popup links optional: only include when discovery
            # produces a verified relevant URL.
            drive["url"] = url or ""
            if not nps_drive_url:
                self._log_decision(
                    kind="scenic_drive",
                    dest_name=dest_name,
                    item_name=drive_name,
                    reason="discovery_completed" if url else "no_match",
                    message="scenic drive link",
                    url=(url or ""),
                )

    # ── Search helpers ───────────────────────────────────────────────────────

    def _search_first(
        self,
        query_variants: list[str],
        site_filter: str | None = None,
        site_hint: str | None = None,
        item_name: str = "",
        dest_name: str = "",
        allow_alltrails: bool = True,
        max_attempts: int = MAX_FALLBACK_ATTEMPTS,
    ) -> str | None:
        # Check cache first
        cache_key = (item_name, dest_name, site_filter or "", "alltrails" if allow_alltrails else "no-alltrails")
        if cache_key in _url_cache:
            self._log_decision(
                kind="search",
                dest_name=dest_name,
                item_name=item_name,
                reason="search_cache_hit",
                message=f"cache hit ({site_filter or 'any'})",
                url=(_url_cache[cache_key] or ""),
            )
            return _url_cache[cache_key]

        # The paid per-item fallback. On the 2026-08-22 cold-start run this
        # path was 218 calls, 1.6M tokens and 306 billed web_search
        # invocations -- $3.86 of a $5.85 run, 66% of it. It fires only for
        # items the direct batch already failed to resolve, so it is the most
        # expensive discovery we do and the least likely to succeed.
        #
        # "geocode_maps" mode declines to make that call. The caller's item
        # instead receives a coordinate Maps link from a free geocode (see
        # _geocode_maps_url_for_item), which satisfies the verified-link
        # policy under the 2026-08-22 owner decision recorded in
        # _item_has_verified_url. Cache hits above are still served -- they
        # cost nothing and are strictly better than a coordinate.
        if str(getattr(self, "_fallback_mode", DEFAULT_FALLBACK_MODE) or "") == "geocode_maps":
            self._log_decision(
                kind="search",
                dest_name=dest_name,
                item_name=item_name,
                reason="paid_fallback_skipped",
                message="fallback_mode=geocode_maps: not buying a per-item search",
            )
            _url_cache[cache_key] = None
            return None

        # Search and cache result
        result = self._search_first_strict(
            query_variants=query_variants,
            site_filter=site_filter,
            site_hint=site_hint,
            item_name=item_name,
            dest_name=dest_name,
            allow_alltrails=allow_alltrails,
            max_attempts=max_attempts,
        )
        _url_cache[cache_key] = result
        if result:
            self._log_decision(
                kind="search",
                dest_name=dest_name,
                item_name=item_name,
                reason="search_resolved",
                message=f"resolved ({site_filter or 'any'})",
                url=result,
            )
        else:
            self._log_decision(
                kind="search",
                dest_name=dest_name,
                item_name=item_name,
                reason="search_no_match",
                message=f"rejected/no-match ({site_filter or 'any'})",
            )
        return result

    def _search_first_strict(
        self,
        *,
        query_variants: list[str],
        site_filter: str | None,
        site_hint: str | None,
        item_name: str,
        dest_name: str,
        allow_alltrails: bool,
        max_attempts: int = MAX_FALLBACK_ATTEMPTS,
    ) -> str | None:
        best: tuple[int, str] | None = None
        ranked_candidates: dict[str, tuple[int, dict[str, Any]]] = {}
        normalized_variants: list[str] = []
        seen_queries: set[str] = set()
        for raw_query in query_variants[:max_attempts]:
            key = str(raw_query or "").strip().lower()
            if not key or key in seen_queries:
                continue
            seen_queries.add(key)
            normalized_variants.append(str(raw_query))

        for query in normalized_variants:
            full_query = f"{site_hint} {query}" if site_hint else (f"site:{site_filter} {query}" if site_filter else query)
            self._note_fallback_call_site("per_item_website_hunt")
            candidates = self._search_cached(full_query, count=10)

            # Pass 1: specific pages only
            for item in candidates:
                url = item.get("url", "")
                if not url:
                    continue
                candidate_url = url
                if site_filter == "google.com/maps":
                    candidate_url = self._normalize_restaurant_url(candidate_url)
                    if not candidate_url:
                        continue
                if not self._matches_site_filter(candidate_url, site_filter):
                    continue
                if site_filter == "alltrails.com" and not self._is_alltrails_trail_url(candidate_url):
                    continue
                if not allow_alltrails and "alltrails.com" in candidate_url.lower():
                    continue
                if not self._is_specific_result_url(candidate_url, item_name, dest_name):
                    continue
                scored_item = dict(item)
                scored_item["url"] = candidate_url
                if not self._meets_place_interest_threshold(scored_item, site_filter=site_filter):
                    continue
                if self._is_alltrails_trail_url(candidate_url):
                    if self._is_relevant_result(
                        candidate_url,
                        item_name,
                        dest_name,
                        candidate=scored_item,
                        deep_check=False,
                    ):
                        score = self._score_candidate_result(
                            scored_item,
                            item_name,
                            dest_name,
                            specific=True,
                            site_filter=site_filter,
                        )
                        existing = ranked_candidates.get(candidate_url)
                        if existing is None or score > existing[0]:
                            ranked_candidates[candidate_url] = (score, scored_item)
                        best = self._pick_better_candidate(best, score, candidate_url)
                    continue
                if self._is_relevant_result(
                    candidate_url,
                    item_name,
                    dest_name,
                    candidate=scored_item,
                    deep_check=False,
                ):
                    score = self._score_candidate_result(
                        scored_item,
                        item_name,
                        dest_name,
                        specific=True,
                        site_filter=site_filter,
                    )
                    existing = ranked_candidates.get(candidate_url)
                    if existing is None or score > existing[0]:
                        ranked_candidates[candidate_url] = (score, scored_item)
                    best = self._pick_better_candidate(best, score, candidate_url)

            if self._should_short_circuit_search(best, site_filter, item_name):
                break

            # Pass 2: any live URL for this variant as fallback
            item_tokens = self._significant_tokens(item_name)
            for item in candidates:
                url = item.get("url", "")
                if not url:
                    continue
                candidate_url = url
                if site_filter == "google.com/maps":
                    candidate_url = self._normalize_restaurant_url(candidate_url)
                    if not candidate_url:
                        continue
                if not self._matches_site_filter(candidate_url, site_filter):
                    continue
                if self._is_obviously_generic_url(candidate_url.lower()):
                    continue
                if site_filter == "alltrails.com" and not self._is_alltrails_trail_url(candidate_url):
                    continue
                # Keep broad-pass matches from degenerating into generic pages.
                if site_filter == "nps.gov":
                    if not self._candidate_text_matches_item_tokens(item, item_tokens) and not any(
                        token in candidate_url.lower() for token in item_tokens
                    ):
                        continue
                if not allow_alltrails and "alltrails.com" in candidate_url.lower():
                    continue
                scored_item = dict(item)
                scored_item["url"] = candidate_url
                if not self._meets_place_interest_threshold(scored_item, site_filter=site_filter):
                    continue
                if self._is_alltrails_trail_url(candidate_url):
                    if self._is_relevant_result(
                        candidate_url,
                        item_name,
                        dest_name,
                        candidate=scored_item,
                        deep_check=False,
                    ):
                        score = self._score_candidate_result(
                            scored_item,
                            item_name,
                            dest_name,
                            specific=False,
                            site_filter=site_filter,
                        )
                        existing = ranked_candidates.get(candidate_url)
                        if existing is None or score > existing[0]:
                            ranked_candidates[candidate_url] = (score, scored_item)
                        best = self._pick_better_candidate(best, score, candidate_url)
                    continue
                if self._is_relevant_result(
                    candidate_url,
                    item_name,
                    dest_name,
                    candidate=scored_item,
                    deep_check=False,
                ):
                    score = self._score_candidate_result(
                        scored_item,
                        item_name,
                        dest_name,
                        specific=False,
                        site_filter=site_filter,
                    )
                    existing = ranked_candidates.get(candidate_url)
                    if existing is None or score > existing[0]:
                        ranked_candidates[candidate_url] = (score, scored_item)
                    best = self._pick_better_candidate(best, score, candidate_url)

            if self._should_short_circuit_search(best, site_filter, item_name):
                break

        if not ranked_candidates:
            return None

        ranked = sorted(ranked_candidates.items(), key=lambda row: row[1][0], reverse=True)
        max_deep_checks = min(3, len(ranked))
        for idx, (candidate_url, payload) in enumerate(ranked):
            score, candidate_item = payload
            if idx >= max_deep_checks:
                break
            if self._is_relevant_result(
                candidate_url,
                item_name,
                dest_name,
                candidate=candidate_item,
                deep_check=True,
            ):
                logger.debug("  URL selected by score=%s -> %s", score, candidate_url[:120])
                if hasattr(self, "_search_winner_snippets"):
                    self._search_winner_snippets[candidate_url] = candidate_item
                return candidate_url

        # Preserve fail-closed behavior when top candidates fail deep checks.
        return None

    def _meets_place_interest_threshold(
        self,
        candidate: dict[str, Any] | None,
        *,
        site_filter: str | None = None,
    ) -> bool:
        if site_filter == "alltrails.com":
            return True
        text = self._candidate_text_blob(candidate)
        rating, votes = self._extract_rating_votes(text)

        require_metadata = bool(getattr(self, "_place_interest_require_metadata", DEFAULT_PLACE_INTEREST_REQUIRE_METADATA))
        if rating is None or votes is None:
            return not require_metadata

        min_rating = float(getattr(self, "_place_interest_min_rating", DEFAULT_PLACE_INTEREST_MIN_RATING))
        min_votes = int(getattr(self, "_place_interest_min_votes", DEFAULT_PLACE_INTEREST_MIN_VOTES))
        return rating >= min_rating and votes >= min_votes

    def _collect_discovered_urls(self, trip: dict[str, Any]) -> set[str]:
        urls: set[str] = set()
        for dest in trip.get("destinations", []) or []:
            if not isinstance(dest, dict):
                continue
            ai = dest.get("ai_content", {}) if isinstance(dest.get("ai_content", {}), dict) else {}

            for attr in ai.get("top_attractions", []) or []:
                if isinstance(attr, dict):
                    url = str(attr.get("url", "") or "").strip()
                    if url:
                        urls.add(url)

            getting_here = ai.get("getting_here", {}) if isinstance(ai.get("getting_here", {}), dict) else {}
            for stop in getting_here.get("en_route_stops", []) or []:
                if isinstance(stop, dict):
                    url = str(stop.get("url", "") or "").strip()
                    if url:
                        urls.add(url)

            for rest in ai.get("dinner_recommendations", []) or []:
                if isinstance(rest, dict):
                    url = str(rest.get("url", "") or "").strip()
                    if url:
                        urls.add(url)

            for drive in dest.get("scenic_drives", []) or []:
                if isinstance(drive, dict):
                    url = str(drive.get("url", "") or "").strip()
                    if url:
                        urls.add(url)

            events = dest.get("cultural_events", {}) if isinstance(dest.get("cultural_events", {}), dict) else {}
            for event in events.get("events", []) or []:
                if isinstance(event, dict):
                    url = str(event.get("url", "") or "").strip()
                    if url:
                        urls.add(url)

        return urls

    def _is_high_confidence_provenance_url(self, url: str) -> bool:
        """URLs whose provenance already establishes high confidence don't
        need the audit pass's proactive full-content prefetch: a harvest row
        already marked direct-batch-authoritative during discovery, or an
        official .gov domain. Skipping the prewarm doesn't skip verification
        entirely -- if some later per-item check still needs this URL's
        content, it fetches on demand at that point; this only avoids paying
        for a bulk fetch that's usually never actually needed downstream."""
        if self._is_remembered_direct_batch_authoritative_url(url):
            return True
        host = urlparse(url).netloc.lower()
        return host.endswith(".gov") or ".gov." in host

    def _prewarm_url_validation_cache(self, trip: dict[str, Any]) -> None:
        candidates = sorted(self._collect_discovered_urls(trip))
        if not candidates:
            return

        safe_prefixes = SAFE_FALLBACK_URL_PREFIXES
        to_fetch: list[str] = []
        for url in candidates:
            lower = url.lower()
            if any(lower.startswith(prefix) for prefix in safe_prefixes):
                continue
            if self._is_obviously_generic_url(lower):
                continue
            if self._is_high_confidence_provenance_url(url):
                continue
            to_fetch.append(url)

        if not to_fetch:
            return

        # Keep AllTrails lazy because it has dedicated throttling/anti-block logic.
        non_alltrails = [u for u in to_fetch if not self._is_alltrails_trail_url(u)]
        if not non_alltrails:
            return

        workers = min(8, max(1, len(non_alltrails)))
        with instrumented_pool("url_audit_page_prewarm", workers) as pool:
            futures = [pool.submit(self._fetch_page_text, url, 8) for url in non_alltrails]
            for future in as_completed(futures):
                try:
                    future.result()
                except Exception:
                    # Best-effort prewarm only; strict checks will still fail closed.
                    pass

    def _should_short_circuit_search(
        self,
        best: tuple[int, str] | None,
        site_filter: str | None,
        item_name: str,
    ) -> bool:
        if not best:
            return False
        score, best_url = best
        if score < 24:
            return False

        if site_filter == "alltrails.com":
            if not self._is_alltrails_trail_url(best_url):
                return False
            return self._alltrails_slug_extra_term_count(best_url, item_name) == 0

        return True

    def _note_fallback_call_site(self, site: str) -> None:
        """Count paid fallback calls by originating call path.

        "url_discovery_fallback" is an operation PREFIX shared by four
        distinct callers of _search_cached, and the run artifacts record only
        that prefix -- every call arrives as `url_discovery_fallback:search`
        with no way to tell them apart. That ambiguity produced the
        2026-08-22 miss: 66% of a run was attributed to "the per-item website
        hunt" when only one of the four paths is that, and a change targeting
        it removed 62 of 218 calls rather than all of them.

        This makes the next run self-attributing, so the rule in
        cost-accounting-and-reduction.md section 8.3 -- no cost prediction
        without attributing the number to its call sites -- can be satisfied
        from artifacts instead of by reading code.
        """
        if not hasattr(self, "_fallback_call_sites"):
            self._fallback_call_sites: dict[str, int] = {}
        self._fallback_call_sites[site] = self._fallback_call_sites.get(site, 0) + 1

    def _search_cached(self, full_query: str, *, count: int = 10) -> list[dict[str, Any]]:
        query_key = str(full_query or "").strip()
        if not query_key:
            return []

        if not hasattr(self, "_search_results_cache"):
            self._search_results_cache = {}
        if not hasattr(self, "_request_cache_lock"):
            self._request_cache_lock = Lock()
        if not hasattr(self, "_search_failure_ts"):
            self._search_failure_ts = {}

        with self._request_cache_lock:
            cached = self._search_results_cache.get(query_key)
            if cached is not None and len(cached) > 0:
                return [dict(item) for item in cached if isinstance(item, dict)]
            failed_at = self._search_failure_ts.get(query_key)
            cooldown = float(
                getattr(self, "_search_failure_cooldown_seconds", DEFAULT_SEARCH_FAILURE_COOLDOWN_SECONDS)
            )
            if failed_at is not None and (time.monotonic() - failed_at) < cooldown:
                # An empty result is never proof this query has no answer --
                # GrokSearch.search() swallows exceptions and returns []
                # either way. Uncached, so a later call after the cooldown
                # naturally expires still gets a real attempt.
                return []

        # Falls back to self._search (the batch client) if _search_fallback
        # isn't set -- keeps ad hoc/partially-constructed instances (e.g.
        # URLDiscoverer.__new__(URLDiscoverer) in tests) working without
        # requiring every one to set both attributes.
        search_client = getattr(self, "_search_fallback", None) or getattr(self, "_search", None)
        if search_client is None:
            return []

        results = search_client.search(query_key, count=count)
        normalized = [dict(item) for item in results if isinstance(item, dict)]

        with self._request_cache_lock:
            if normalized:
                self._search_results_cache[query_key] = normalized
                self._search_failure_ts.pop(query_key, None)
            else:
                self._search_results_cache.pop(query_key, None)
                self._search_failure_ts[query_key] = time.monotonic()
        self._mark_persistent_cache_dirty()

        return [dict(item) for item in normalized]

    def _verify_url_cached(self, url: str) -> tuple[bool, int | str]:
        if not url:
            return False, "invalid_url"
        if not hasattr(self, "_url_validator"):
            return False, "no_validator"

        if not hasattr(self, "_verify_url_cache"):
            self._verify_url_cache = {}
        if not hasattr(self, "_request_cache_lock"):
            self._request_cache_lock = Lock()

        with self._request_cache_lock:
            cached = self._verify_url_cache.get(url)
        if cached is not None:
            self._record_fetch_liveness(url, bool(cached[0]), cached[1])
            return cached

        result = self._url_validator.verify_url(url)
        if not isinstance(result, tuple) or len(result) != 2:
            # Some tests/mocks provide a bare MagicMock for verify_url.
            # Normalize to a conservative failure tuple instead of raising
            # unpack/type errors in downstream relevance checks.
            result = (False, "invalid_verify_result")
        with self._request_cache_lock:
            self._verify_url_cache[url] = result
        self._record_fetch_liveness(url, bool(result[0]), result[1])
        self._mark_persistent_cache_dirty()
        return result

    @staticmethod
    def _pick_better_candidate(current: tuple[int, str] | None, score: int, url: str) -> tuple[int, str]:
        if current is None or score > current[0]:
            return score, url
        return current

    def _score_candidate_result(
        self,
        item: dict[str, Any],
        item_name: str,
        dest_name: str,
        *,
        specific: bool,
        site_filter: str | None = None,
    ) -> int:
        url = str(item.get("url", "") or "")
        title = str(item.get("name", "") or "")
        snippet = str(item.get("snippet", "") or "")

        parsed = urlparse(url)
        host_path = f"{parsed.netloc}{parsed.path}".lower()
        text = f"{title} {snippet}".lower()

        score = 0
        # A domain that IS the item outranks a page merely about it. Without
        # this the ranking treated champagne-tastes.com/rotisse and rotisse.be
        # as equivalent -- both mention "rotisse", one in the path and one in
        # the domain -- and whichever the provider returned first won. Applied
        # as a strong bonus rather than a filter so it steers the choice when
        # an official site is present and changes nothing when none is.
        if self._domain_matches_item_name(url, item_name):
            score += 25
        item_tokens = self._significant_tokens(item_name)
        dest_tokens = self._significant_tokens(dest_name)

        item_overlap_url = sum(1 for t in item_tokens if t in host_path)
        item_overlap_text = sum(1 for t in item_tokens if t in text)
        dest_overlap_url = sum(1 for t in dest_tokens if t in host_path)
        dest_overlap_text = sum(1 for t in dest_tokens if t in text)

        score += item_overlap_url * 6
        score += item_overlap_text * 4
        score += dest_overlap_url * 4
        score += dest_overlap_text * 3

        if self._is_alltrails_trail_url(url):
            slug = unquote(parsed.path.rsplit("/", 1)[-1]).replace("-", " ")
            slug_tokens = self._significant_tokens(slug)
            if slug_tokens and item_tokens:
                item_set = set(item_tokens)
                extra_slug_terms = [t for t in slug_tokens if t not in item_set]
                # Prefer canonical trail page slugs over broader route variants.
                score -= len(extra_slug_terms) * 3
            if "-via-" in parsed.path.lower() or " via " in slug.lower():
                # "via" pages are often broader route variants than the canonical trail page.
                score -= 4

        if dest_name and dest_name.lower() in text:
            score += 10

        for hint in POSITIVE_DOMAIN_HINTS:
            if hint in parsed.netloc.lower():
                score += 4
        for hint in NEGATIVE_DOMAIN_HINTS:
            if hint in parsed.netloc.lower():
                score -= 5

        inferred_tlds = self._destination_country_tlds(dest_name)
        netloc = parsed.netloc.lower()
        for tld in inferred_tlds:
            if tld == "co.uk":
                if netloc.endswith(".co.uk") or netloc == "co.uk":
                    score += 5
                continue
            if netloc.endswith(f".{tld}") or netloc == tld:
                score += 5

        path_l = parsed.path.lower()
        for hint in POSITIVE_PATH_HINTS:
            if hint in path_l:
                score += 3

        if specific:
            score += 2

        rating, votes = self._extract_rating_votes(f"{title} {snippet}")
        if self._is_alltrails_trail_url(url) or site_filter == "alltrails.com":
            score += self._rating_priority_boost(
                rating=rating,
                votes=votes,
                min_rating=float(getattr(self, "_alltrails_rating_min", DEFAULT_ALLTRAILS_RATING_MIN)),
                min_votes=int(
                    getattr(self, "_alltrails_rating_min_votes", DEFAULT_ALLTRAILS_RATING_MIN_VOTES)
                ),
                boost=int(getattr(self, "_alltrails_rating_boost", DEFAULT_ALLTRAILS_RATING_BOOST)),
            )
        elif site_filter in {"google.com/maps", "tripadvisor.com"}:
            score += self._rating_priority_boost(
                rating=rating,
                votes=votes,
                min_rating=float(getattr(self, "_restaurant_rating_min", DEFAULT_RESTAURANT_RATING_MIN)),
                min_votes=int(
                    getattr(self, "_restaurant_rating_min_votes", DEFAULT_RESTAURANT_RATING_MIN_VOTES)
                ),
                boost=int(getattr(self, "_restaurant_rating_boost", DEFAULT_RESTAURANT_RATING_BOOST)),
            )

        return score

    @staticmethod
    def _rating_priority_boost(
        *,
        rating: float | None,
        votes: int | None,
        min_rating: float,
        min_votes: int,
        boost: int,
    ) -> int:
        # High ratings are prioritized only when supported by sufficient votes.
        if rating is None or votes is None:
            return 0
        if rating < min_rating:
            return 0
        if votes < min_votes:
            return 0
        return max(0, int(boost))

    @staticmethod
    def _extract_rating_votes(text: str) -> tuple[float | None, int | None]:
        body = str(text or "")

        rating_match = re.search(
            r"(?:^|[^0-9])([0-4](?:\.\d+)?|5(?:\.0+)?)\s*(?:/\s*5|stars?)\b",
            body,
            flags=re.IGNORECASE,
        )
        rating = float(rating_match.group(1)) if rating_match else None

        votes: int | None = None
        k_votes_match = re.search(
            r"(\d+(?:\.\d+)?)\s*k\s*(?:reviews?|ratings?|votes?)\b",
            body,
            flags=re.IGNORECASE,
        )
        if k_votes_match:
            votes = int(float(k_votes_match.group(1)) * 1000)
        else:
            votes_match = re.search(
                r"(\d{1,3}(?:,\d{3})+|\d+)\s*(?:reviews?|ratings?|votes?)\b",
                body,
                flags=re.IGNORECASE,
            )
            if votes_match:
                votes = int(votes_match.group(1).replace(",", ""))

        return rating, votes

    def _is_specific_result_url(self, url: str, item_name: str, dest_name: str) -> bool:
        lower = url.lower()
        if self._is_obviously_generic_url(lower):
            return False
        if "google.com/search" in lower or "/search?" in lower:
            return False
        if "nps.gov" in lower and "/search" in lower:
            return False

        # Reject attribution/media pages that are not "more info" landing pages.
        if "commons.wikimedia.org" in lower or "wikipedia.org/wiki/file:" in lower:
            return False

        # Reject generic section landing pages, but allow specific child pages
        # under those sections (e.g. /planyourvisit/kolob-canyons.htm).
        if self._is_generic_section_landing_page(url):
            return False

        item_tokens = self._significant_tokens(item_name)
        if item_tokens and not any(token in lower for token in item_tokens):
            return False

        # If destination tokens exist in URL it's usually a much better match.
        dest_tokens = self._significant_tokens(dest_name)
        if dest_tokens and any(token in lower for token in dest_tokens):
            return True

        return True

    def _looks_like_item_specific_homepage(self, url: str, item_name: str, *, item_tokens: list[str] | None = None) -> bool:
        candidate = str(url or "").strip()
        if not candidate:
            return False

        parsed = urlparse(candidate)
        if parsed.query or parsed.fragment:
            return False

        path = unquote(parsed.path or "").strip()
        # Normalize trailing index files so /band/index.htm counts as depth 1.
        norm_path = re.sub(r"/index\.html?$", "/", path, flags=re.IGNORECASE).rstrip("/")
        path_depth = len([s for s in norm_path.strip("/").split("/") if s])

        host = (parsed.netloc or "").lower()
        if not host:
            return False

        # A bare nps.gov/<code>/ homepage is specific -- not generic -- exactly
        # when the item itself names the park that code belongs to (e.g. an
        # attraction literally titled "Canyonlands National Park"), using the
        # same code table _infer_item_nps_code already uses elsewhere for NPS
        # lookups. Any other item within that park (a district, trail,
        # viewpoint, etc. -- e.g. "Island in the Sky") still needs a more
        # specific page and does not get this pass.
        if "nps.gov" in host:
            norm_segments = [s for s in norm_path.strip("/").split("/") if s]
            if len(norm_segments) == 1 and self._infer_item_nps_code(item_name) == norm_segments[0]:
                return True

        host_text = re.sub(r"[^a-z0-9]+", " ", host).lower()
        host_slug = re.sub(r"[^a-z0-9]+", "", host).lower()
        path_slug = re.sub(r"[^a-z0-9]+", "", path).lower()
        tokens = item_tokens if item_tokens is not None else self._significant_tokens(item_name)
        if not tokens:
            return False

        # For depth-1 paths, only allow when at least one item token appears in the host.
        if path_depth > 1:
            return False
        if path_depth == 1:
            if not any(token in host_slug for token in tokens if len(token) >= 3):
                return False

        item_slug = re.sub(r"[^a-z0-9]+", "", item_name).lower()
        if item_slug and (item_slug in host_slug or item_slug in path_slug):
            return True

        if any(token in host_slug for token in tokens if len(token) >= 3):
            return True
        if any(token in path_slug for token in tokens if len(token) >= 3):
            return True
        if any(token in host_text for token in tokens if len(token) >= 3):
            return True

        # Abbreviated domains: check if host word and item token share a 5+ char common prefix.
        host_words = re.findall(r"[a-z0-9]+", host)
        for hw in host_words:
            if len(hw) < 4:
                continue
            for token in tokens:
                if len(token) < 4:
                    continue
                # tok_in_hw covers e.g. "sheridan" in "newsheridan"
                if token in hw:
                    return True
                # Shared prefix covers e.g. "cosmo*" in "cosmopolitan" vs "cosmotelluride"
                common = 0
                for a, b in zip(hw, token):
                    if a == b:
                        common += 1
                    else:
                        break
                if common >= 5:
                    return True

        try:
            ok, _status, page_html = self._fetch_page_text(candidate, timeout=8)
            if ok and page_html and self._text_matches_item_tokens(page_html, tokens):
                return True
        except Exception:
            pass

        return False

    @staticmethod
    def _is_generic_section_landing_page(url: str) -> bool:
        parsed = urlparse(str(url or ""))
        host = (parsed.netloc or "").lower()
        path = (parsed.path or "").strip().lower()
        if not path:
            return True

        if "tripadvisor." in host and path.startswith("/attractions-") and "-activities-" in path:
            return True

        segments = [seg for seg in path.strip("/").split("/") if seg]
        if not segments:
            return True

        # A bare nps.gov/<park-code>/ homepage (no further path) is the whole
        # park's generic landing page -- the same generic-ness as an empty
        # path, just with the park's own 4-letter NPS unit code standing in
        # for it (e.g. nps.gov/cany/ for Canyonlands National Park). Real
        # dipstick58 example: "Island in the Sky", a specific district within
        # Canyonlands, rendered with this bare park homepage instead of a
        # district-specific page -- this is the PR-011 "area-reference
        # instead of subject-specific destination" pattern this function
        # exists to catch, it just didn't cover the bare-park-code shape.
        # _looks_like_item_specific_homepage still allows this through for an
        # item that names that exact park (see _infer_item_nps_code).
        if "nps.gov" in host and len(segments) == 1 and re.fullmatch(r"[a-z]{4}", segments[0]):
            return True

        last = segments[-1]
        generic_sections = {
            "plan-your-visit",
            "planyourvisit",
            "visit",
            "things-to-do",
            "things2do",
            "explore",
            "about",
            "home",
            "restaurants",
            "restaurant",
            "dining",
            "food",
            "eat",
            "attractions",
            "attraction",
            "activities",
            "activity",
        }
        if last in {"index.htm", "index.html"}:
            return True
        if last in generic_sections:
            return True

        # Some NPS section pages use a generic filename under planyourvisit.
        if len(segments) >= 2 and segments[-2] in {"planyourvisit", "plan-your-visit"} and last.startswith("things2do"):
            return True

        return False

    def _redirect_target_lacks_item_relevance(
        self,
        original_url: str,
        item_name: str,
        dest_name: str,
        item_tokens: list[str],
        kind: str,
        fetched_text: str = "",
    ) -> str:
        """Return the final redirect URL if it looks like a generic hub page
        unrelated to the specific item, else "" (no redirect, or redirect
        target still looks item-specific).

        Real dipstick69 bug: the en-route stop "Poshuouinge Pueblo Ruins" was
        linked to fs.usda.gov/recarea/carson/recarea/?recid=44248 -- a URL
        whose distinguishing ?recid= query param made it LOOK item-specific,
        which is exactly why it sailed through _is_generic_section_landing_page
        (pure URL-string/path-segment matching) and then through the
        direct-batch row-matched leniency path below. Live-fetch confirmed
        it 301-redirects to fs.usda.gov/r03/carson/recreation -- a generic
        Carson National Forest recreation hub with zero mentions of
        "Poshuouinge" anywhere on the page. Notably, that final path's last
        segment ("recreation") isn't even in _is_generic_section_landing_page's
        generic_sections set, so reapplying that check unchanged to the final
        URL string would NOT have caught this specific real case -- the one
        signal that reliably does is the page's own fetched text (already
        captured by _fetch_page_text, which follows the redirect itself and
        returns the final destination's body): if the item's own name never
        appears on the page the URL actually resolves to, the URL is not
        item-specific no matter how its original path looked.
        """
        # Only a recorded redirect gives this gate anything to judge. Both
        # `not_redirected` (measured: it resolved to itself) and `unchecked`
        # (nothing observed) fail open, and deliberately for different
        # reasons: the first has nothing to reject on, the second has no
        # evidence to reject on. Splitting them changes no verdict here --
        # they were already the same branch -- but it means a later reader can
        # tell a URL this gate cleared from one it never saw.
        state = self.link_redirect_state(original_url)
        if state != LINK_REDIRECT_FOLLOWED:
            return ""
        final_url = getattr(self, "_fetch_final_url_cache", {}).get(original_url)
        if kind == "restaurant":
            redirect_generic = self._is_generic_restaurant_landing_url(
                final_url, item_name, dest_name, item_tokens=item_tokens
            )
        else:
            redirect_generic = self._is_generic_section_landing_page(final_url)
        if not redirect_generic and fetched_text:
            redirect_generic = not self._text_matches_item_tokens(fetched_text.lower(), item_tokens)
        return final_url if redirect_generic else ""

    def _is_relevant_result(
        self,
        url: str,
        item_name: str,
        dest_name: str,
        candidate: dict[str, Any] | None = None,
        deep_check: bool = True,
        item_description: str = "",
    ) -> bool:
        """Lightweight relevance gate: avoid live but useless links."""
        if self._is_obviously_generic_url(url.lower()):
            return False
        if self._is_campground_focused_result_for_noncamping_item(url, item_name, candidate_text=self._candidate_text_blob(candidate)):
            return False
        if self._is_alltrails_trail_url(url):
            item_tokens = self._significant_tokens(item_name)
            if not self._alltrails_slug_matches_item(url, item_name):
                return False
            # Slug denylist fast-reject (known-invalid/dead slugs from config).
            _slug = urlparse(url).path.rsplit("/", 1)[-1].lower()
            if _slug in getattr(self, "_alltrails_slug_denylist", frozenset()):
                return False
            if self._alltrails_slug_has_numbered_suffix(url):
                return False
            max_trail_miles = float(getattr(self, "_max_trail_miles", DEFAULT_MAX_TRAIL_MILES) or DEFAULT_MAX_TRAIL_MILES)
            if max_trail_miles > 0:
                candidate_miles = self._extract_trail_miles(self._candidate_text_blob(candidate))
                if candidate_miles is not None and candidate_miles > max_trail_miles:
                    return False
            metadata_ok = self._candidate_text_matches_item_tokens(candidate, item_tokens)
            destination_ok = self._candidate_text_matches_destination_tokens(candidate, dest_name)
            listing_signal_ok = self._candidate_has_alltrails_listing_signal(candidate)
            candidate_text_blob = self._candidate_text_blob(candidate)
            if self._has_alltrails_closure_marker(candidate_text_blob):
                return False
            slug_extra_terms = self._alltrails_slug_extra_term_count(url, item_name)

            if not deep_check:
                if candidate is not None:
                    has_candidate_metadata = bool(candidate_text_blob.strip())
                    if has_candidate_metadata:
                        if not metadata_ok:
                            return False
                        if len(item_tokens) <= 1 and not destination_ok:
                            return False
                        if slug_extra_terms >= 2 and not listing_signal_ok:
                            return False
                return True

            try:
                ok, status, text = self._fetch_page_text(url, timeout=8)
                if not ok:
                    verified_ok, verified_status = self._verify_url_cached(url)
                    if not verified_ok and self._is_definitively_dead_status(verified_status):
                        return False
                    final_url = getattr(self, "_fetch_final_url_cache", {}).get(url, url)
                    if final_url != url and self._is_alltrails_trail_url(final_url):
                        if not self._alltrails_slug_matches_item(final_url, item_name):
                            logger.info(
                                "AllTrails redirect entity mismatch (blocked fetch): %s -> %s (item: %s)",
                                url,
                                final_url,
                                item_name,
                            )
                            return False
                    blocked_status = isinstance(status, int) and status in (401, 403)
                    if blocked_status and not bool(getattr(self, "_allow_blocked_alltrails", DEFAULT_ALLOW_BLOCKED_ALLTRAILS)):
                        return False
                    if candidate is not None:
                        if not metadata_ok:
                            return False
                        if len(item_tokens) <= 1 and not destination_ok:
                            return False
                        if slug_extra_terms >= 2 and not listing_signal_ok:
                            return False
                        return True
                    # Audit paths do not provide a search-result candidate. When
                    # AllTrails page text fetch fails (bot protection, transient
                    # network), keep strong slug matches by default. A separate
                    # liveness probe is often blocked and causes false negatives.
                    # Only reject explicit not-found statuses.
                    if candidate is None:
                        if self._is_definitively_dead_status(status):
                            return False
                        return True
                    if self._is_definitively_dead_status(status):
                        return False
                    # Search candidates may sometimes provide only URL with no
                    # snippet/title metadata and still be valid; slug match is
                    # already enforced above, so keep as fallback.
                    return True
                text = (text or "").lower()
                if any(marker in text for marker in ALLTRAILS_404_MARKERS):
                    return False
                if self._has_alltrails_closure_marker(text):
                    return False
                # Redirect entity check: if AllTrails silently redirected to a
                # different trail, the final URL slug won't match the item.
                final_url = getattr(self, "_fetch_final_url_cache", {}).get(url, url)
                if final_url != url and self._is_alltrails_trail_url(final_url):
                    if not self._alltrails_slug_matches_item(final_url, item_name):
                        logger.info(
                            "AllTrails redirect entity mismatch: %s -> %s (item: %s)",
                            url, final_url, item_name,
                        )
                        return False
                if max_trail_miles > 0:
                    fetched_miles = self._extract_trail_miles(text)
                    if fetched_miles is not None and fetched_miles > max_trail_miles:
                        return False
                if not self._text_matches_item_tokens(text, item_tokens):
                    # Some AllTrails pages are bot-protected and may return sparse
                    # HTML despite a correct trail URL/result card. Keep strict slug
                    # matching and allow strong result metadata as a fallback signal.
                    if candidate is None:
                        return True
                    return metadata_ok
                # Destination names for gateway towns (e.g., Telluride) are often
                # omitted from AllTrails page copy even when the trail is correct.
                # Slug + on-page item-token matching is the primary relevance gate.
                return True
            except Exception:
                return metadata_ok
        try:
            if not deep_check:
                item_tokens = self._significant_tokens(item_name)
                if item_tokens:
                    in_url = any(t in (url or "").lower() for t in item_tokens)
                    in_candidate = self._candidate_text_matches_item_tokens(candidate, item_tokens)
                    if not (in_url or in_candidate):
                        return False
                return True

            ok, status, text = self._fetch_page_text(url, timeout=8)
            if not ok:
                if self._is_definitively_dead_status(status):
                    return False
                # A blocked/transient fetch failure (403/401/timeout/5xx/SSL)
                # is not proof the URL is dead -- a bot-blocking site (e.g.
                # TripAdvisor) fails this exact same way for a perfectly live
                # page. Mirror the AllTrails branch above: try a secondary
                # liveness probe, then fall back to candidate metadata rather
                # than rejecting outright on an inconclusive fetch.
                verified_ok, verified_status = self._verify_url_cached(url)
                if not verified_ok and self._is_definitively_dead_status(verified_status):
                    return False
                item_tokens = self._significant_tokens(item_name)
                if candidate is not None:
                    return self._candidate_text_matches_item_tokens(candidate, item_tokens)
                return True
            text = (text or "").lower()
            if self._is_under_construction_page(text):
                return False
            if self._is_campground_focused_result_for_noncamping_item(url, item_name, fetched_text=text):
                return False
            item_tokens = self._significant_tokens(item_name)
            dest_tokens = self._significant_tokens(dest_name)
            if not self._text_matches_item_tokens(text, item_tokens):
                return False
            if self._looks_like_item_specific_homepage(url, item_name, item_tokens=item_tokens):
                return True
            if dest_tokens and not any(t in text for t in dest_tokens[:2]):
                return False
            # Real bug (Bryce Canyon eval run): "Scenic Drive Overlooks" (an
            # attraction name built entirely from generic route/content
            # vocabulary -- "scenic"/"drive" are already excluded by
            # _significant_tokens as generic descriptors, leaving only
            # "overlook[s]", itself a member of GENERIC_VIEWPOINT_SUFFIX_TOKENS)
            # matched nps.gov's hoodoo-geology explainer page: any Bryce
            # Canyon page mentioning "overlook" once (the single-token
            # relevance bar, _required_general_token_matches(1) == 1) plus the
            # destination name trivially clears the checks above, even though
            # the page is about a completely different topic (how hoodoos
            # form, not the scenic drive/auto-tour the item actually names).
            # When the item's own display name carries no real distinguishing
            # identity -- every significant token is a generic viewpoint/
            # overlook descriptor -- the only remaining source of real
            # specificity is the item's own AI-written description (e.g.
            # "18-mile auto tour with multiple pullouts for hoodoo viewing").
            # Require some overlap with that description too, so a same-park
            # page about an unrelated topic can no longer pass on destination
            # + a single generic word alone.
            name_tokens_are_weak = not item_tokens or set(item_tokens) <= GENERIC_VIEWPOINT_SUFFIX_TOKENS
            if name_tokens_are_weak:
                desc_tokens = self._significant_tokens(item_description)
                if desc_tokens and not self._text_matches_item_tokens(text, desc_tokens):
                    return False
            return True
        except Exception:
            return False

    @staticmethod
    def _is_definitively_dead_status(status: int | str | None) -> bool:
        """True when a fetch status means the URL is genuinely gone -- an explicit
        404/410 HTTP status, or a connection-level failure meaning the host
        doesn't exist or refuses all connections (DNS resolution failure, refused
        connection). A DNS failure is at least as conclusive as a 404 -- the
        domain doesn't exist at all -- so it must not be treated differently just
        because requests/urllib3 report it as an exception string rather than a
        status code. Other failure modes (timeouts, 401/403/500/503, SSL errors)
        are deliberately NOT included: those can be transient or bot-blocking
        false positives and must keep failing open.
        """
        if isinstance(status, int):
            return status in (404, 410)
        text = str(status or "").lower()
        return any(
            marker in text
            for marker in (
                "getaddrinfo failed",
                "name or service not known",
                "nodename nor servname",
                "nameresolutionerror",
                "failed to resolve",
                "failed to establish a new connection",
                "no address associated with hostname",
                "errno 11001",
            )
        )

    @staticmethod
    def _is_bot_block_false_negative_dead_status(url: str, status: int | str | None) -> bool:
        """True when a "definitively dead" verdict is actually an ambiguous
        connection-level failure against a domain too well-established to
        plausibly have disappeared.

        "Failed to establish a new connection" is the one marker in
        `_is_definitively_dead_status` that is genuinely ambiguous: urllib3
        uses that exact phrasing both when DNS resolution never got far
        enough to try connecting (host doesn't exist) *and* when a live
        host's TCP connection is actively refused or reset -- which is
        exactly what an aggressive bot-blocking WAF does to automated
        traffic from a flagged IP, rather than completing the handshake and
        returning a clean HTTP 403. Federal recreation-site domains
        (nps.gov, blm.gov, fs.usda.gov, ...) are both essentially certain to
        still exist and well documented for exactly this kind of
        connection-level bot blocking (dipstick67: direct-batch harvest
        found the real, live nps.gov pages for "Cliff Palace" at Mesa Verde
        and "Checkerboard Mesa" -- both among the most-visited, actively
        maintained pages on the entire site -- and both got rejected here as
        "dead" on a single fetch).

        This carve-out is deliberately narrow: an explicit HTTP 404/410, or
        any of the other, unambiguous DNS-resolution-failure markers (a much
        more specific and reliable "this host doesn't exist" signal), still
        means dead regardless of domain -- this only second-guesses the one
        marker that a live, bot-blocking host can also trigger.
        """
        if isinstance(status, int):
            return False
        text = str(status or "").lower()
        unambiguous_dns_failure_markers = (
            "getaddrinfo failed",
            "name or service not known",
            "nodename nor servname",
            "nameresolutionerror",
            "failed to resolve",
            "no address associated with hostname",
            "errno 11001",
        )
        if any(marker in text for marker in unambiguous_dns_failure_markers):
            return False
        if "failed to establish a new connection" not in text:
            return False
        host = urlparse(url or "").netloc.lower()
        return host.endswith(".gov") or ".gov." in host

    # ------------------------------------------------------------------
    # Link liveness ledger (see LINK_LIVENESS_* above)
    #
    # Purely observational. Nothing below changes which URLs are accepted or
    # rejected -- it records what the gate learned while deciding, so that a
    # published link can afterwards say which of the three states it is in
    # rather than only that it shipped.
    # ------------------------------------------------------------------

    def classify_link_liveness(self, url: str, ok: bool, status: int | str | None) -> str:
        """Map one fetch outcome onto a liveness state.

        Deliberately built from the same two predicates the publication gate
        itself uses, so the ledger cannot drift away from the decision it is
        describing: a status is `dead` exactly when the gate would reject on
        it, and `unchecked` covers every outcome the gate deliberately fails
        open on -- timeouts, 401/403 block pages, SSL errors, cooldown
        synthetics, and the bot-block carve-out that withdraws an otherwise
        definitive verdict.
        """
        if ok:
            return LINK_LIVENESS_LIVE
        if self._is_definitively_dead_status(status) and not self._is_bot_block_false_negative_dead_status(url, status):
            return LINK_LIVENESS_DEAD
        return LINK_LIVENESS_UNCHECKED

    def _record_link_liveness(self, url: str | None, state: str, detail: str = "") -> None:
        """Merge one observation into the run's ledger, most severe wins."""
        normalized = str(url or "").strip()
        if not normalized or state not in LINK_LIVENESS_PRECEDENCE:
            return
        if not hasattr(self, "_link_liveness"):
            self._link_liveness = {}
        if not hasattr(self, "_request_cache_lock"):
            self._request_cache_lock = Lock()
        with self._request_cache_lock:
            previous = self._link_liveness.get(normalized)
            if previous is not None and LINK_LIVENESS_PRECEDENCE[previous[0]] >= LINK_LIVENESS_PRECEDENCE[state]:
                return
            self._link_liveness[normalized] = (state, str(detail or ""))

    def _record_fetch_liveness(self, url: str | None, ok: bool, status: int | str | None, detail: str = "") -> None:
        normalized = str(url or "").strip()
        if not normalized:
            return
        state = self.classify_link_liveness(normalized, ok, status)
        self._record_link_liveness(normalized, state, detail or str(status if status is not None else ""))

    def link_liveness_state(self, url: str | None) -> str:
        """The state recorded for one URL this run. Never checked -> unchecked."""
        normalized = str(url or "").strip()
        if not normalized:
            return LINK_LIVENESS_UNCHECKED
        recorded = getattr(self, "_link_liveness", {}) or {}
        entry = recorded.get(normalized)
        return entry[0] if entry else LINK_LIVENESS_UNCHECKED

    def link_liveness_report(self, trip: dict[str, Any]) -> dict[str, Any]:
        """Tri-state liveness for every link the trip is about to publish.

        A URL the ledger never saw is `unchecked` with detail `never_fetched`
        -- the state the whole exercise exists to make visible, because it is
        the one that previously looked exactly like `live`.

        `unchecked_by_domain` is here rather than left to the caller because
        the failure is domain-shaped in practice: a handful of bot-blocking
        hosts account for nearly all of it, and a per-domain count is what
        turns the number into something actionable.
        """
        published = self._collect_discovered_urls(trip)
        recorded = dict(getattr(self, "_link_liveness", {}) or {})
        states: dict[str, str] = {}
        details: dict[str, str] = {}
        counts = {LINK_LIVENESS_LIVE: 0, LINK_LIVENESS_DEAD: 0, LINK_LIVENESS_UNCHECKED: 0}
        unchecked_by_domain: dict[str, int] = {}
        for url in sorted(published):
            entry = recorded.get(url)
            state, detail = entry if entry else (LINK_LIVENESS_UNCHECKED, "never_fetched")
            states[url] = state
            details[url] = detail
            counts[state] = counts.get(state, 0) + 1
            if state == LINK_LIVENESS_UNCHECKED:
                domain = urlparse(url).netloc.lower()
                if domain:
                    unchecked_by_domain[domain] = unchecked_by_domain.get(domain, 0) + 1
        total = len(states)
        return {
            "states": states,
            "details": details,
            "counts": counts,
            "published_count": total,
            "unchecked_share": (counts[LINK_LIVENESS_UNCHECKED] / total) if total else 0.0,
            "unchecked_by_domain": dict(
                sorted(unchecked_by_domain.items(), key=lambda row: (-row[1], row[0]))
            ),
        }

    # ------------------------------------------------------------------
    # Redirect ledger (see LINK_REDIRECT_* above)
    #
    # Same posture as the liveness ledger: recording only. Nothing here
    # changes which URLs are accepted or rejected -- it makes the record able
    # to distinguish "resolved to itself" from "never looked", so that the
    # gate reading it can fail open on the second without having to guess.
    # ------------------------------------------------------------------

    def classify_link_redirect(self, url: str | None, final_url: str | None) -> str:
        """Map one observed (url, final url) pair onto a redirect state.

        An empty or missing final URL is `unchecked`, never `not_redirected`:
        the fetch paths that leave it empty are the ones that did not observe
        a destination at all -- a request that raised, a validator with no
        final-URL channel -- and reading their silence as "it did not
        redirect" is the ambiguity this ledger exists to remove.
        """
        original = str(url or "").strip()
        final = str(final_url or "").strip()
        if not original or not final:
            return LINK_REDIRECT_UNCHECKED
        return LINK_REDIRECT_NONE if final == original else LINK_REDIRECT_FOLLOWED

    def _record_link_redirect(self, url: str | None, final_url: str | None) -> None:
        """Record one observation of where a URL resolved to.

        `not_redirected` is stored as the URL itself, so an entry's value is
        always the final URL and `unchecked` remains the absence of an entry.
        An unobserved final URL records nothing, which leaves the URL
        `unchecked` rather than downgrading an earlier real observation.
        """
        original = str(url or "").strip()
        state = self.classify_link_redirect(original, final_url)
        if state == LINK_REDIRECT_UNCHECKED:
            return
        if not hasattr(self, "_fetch_final_url_cache"):
            self._fetch_final_url_cache = {}
        self._fetch_final_url_cache[original] = (
            original if state == LINK_REDIRECT_NONE else str(final_url or "").strip()
        )

    def link_redirect_state(self, url: str | None) -> str:
        """The redirect state recorded for one URL. Never observed -> unchecked."""
        original = str(url or "").strip()
        if not original:
            return LINK_REDIRECT_UNCHECKED
        recorded = getattr(self, "_fetch_final_url_cache", {}) or {}
        return self.classify_link_redirect(original, recorded.get(original))

    def _fetch_page_text(self, url: str, timeout: int = 8) -> tuple[bool, int | str, str]:
        if self._is_alltrails_trail_url(url):
            result = self._fetch_alltrails_text(url, timeout=timeout)
            self._record_fetch_liveness(url, bool(result[0]), result[1])
            return result

        if not hasattr(self, "_page_text_cache"):
            self._page_text_cache = {}
        if not hasattr(self, "_request_cache_lock"):
            self._request_cache_lock = Lock()
        if not hasattr(self, "_domain_blocked_until_ts"):
            self._domain_blocked_until_ts = {}

        with self._request_cache_lock:
            cached = self._page_text_cache.get(url)
        if cached is not None:
            # Recorded on a cache hit too, not only on the miss that filled it:
            # the page-text cache persists across runs, so a later run can
            # publish a link whose only observation it inherited and would
            # otherwise report as never checked.
            self._record_fetch_liveness(url, bool(cached[0]), cached[1])
            return cached

        domain = urlparse(url).netloc.lower()
        if domain:
            with self._request_cache_lock:
                blocked_until = float(self._domain_blocked_until_ts.get(domain, 0.0) or 0.0)
            if blocked_until > time.monotonic():
                # Generic equivalent of the AllTrails block-cooldown: a domain
                # that just returned 401/403 to us is very likely to do so
                # again for any other distinct URL on that same domain
                # (e.g. a different TripAdvisor restaurant page). Return a
                # synthetic blocked result immediately -- uncached, so a
                # later call after the cooldown naturally expires still gets
                # a real attempt -- instead of paying a full network timeout
                # for a call very unlikely to succeed.
                #
                # This is the archetypal `unchecked`: no request was made, so
                # the 403 is a placeholder rather than an observation, and the
                # ledger says so instead of letting a synthetic status stand in
                # for a verdict.
                self._record_link_liveness(url, LINK_LIVENESS_UNCHECKED, "domain_cooldown")
                return False, 403, ""

        result = self._fetch_page_text_uncached(url, timeout=timeout)
        status = result[1]
        self._record_fetch_liveness(url, bool(result[0]), status)
        if domain and isinstance(status, int) and status in (401, 403):
            cooldown = float(
                getattr(self, "_domain_block_cooldown_seconds", DEFAULT_DOMAIN_BLOCK_COOLDOWN_SECONDS) or 0.0
            )
            if cooldown > 0:
                with self._request_cache_lock:
                    self._domain_blocked_until_ts[domain] = time.monotonic() + cooldown

        with self._request_cache_lock:
            self._page_text_cache[url] = result
        self._mark_persistent_cache_dirty()
        return result

    def _fetch_page_text_uncached(self, url: str, timeout: int = 8) -> tuple[bool, int | str, str]:
        if not hasattr(self, "_url_validator"):
            return False, "no_validator", ""
        get_text = getattr(self._url_validator, "get_text", None)
        if callable(get_text):
            try:
                out = get_text(url, timeout=timeout)
                if isinstance(out, tuple) and len(out) == 3:
                    # Thread-local on the validator, and cleared at the top of
                    # every get_text call, so an empty value here means this
                    # call observed no destination -- `unchecked`, which
                    # _record_link_redirect declines to write. A value equal to
                    # `url` is a measured non-redirect and IS written.
                    final_url = str(getattr(self._url_validator, "_last_final_url", "") or "")
                    self._record_link_redirect(url, final_url)
                    return bool(out[0]), out[1], str(out[2] or "")
            except Exception:
                pass

        # Backward-compat fallback for tests/mocks that only expose session.get.
        try:
            resp = self._url_validator.session.get(url, timeout=timeout)
            # Track final URL after any redirect for entity-match verification.
            # This branch reads its own response, so a URL equal to `url` is a
            # real observation of "it did not redirect" and is recorded as one.
            final_url = str(getattr(resp, "url", None) or url)
            self._record_link_redirect(url, final_url)
            return resp.status_code < 400, resp.status_code, resp.text or ""
        except Exception as exc:
            return False, str(exc), ""

    def _fetch_alltrails_text(self, url: str, timeout: int = 8) -> tuple[bool, int | str, str]:
        # Some tests build URLDiscoverer via __new__; lazily initialize fields.
        if not hasattr(self, "_alltrails_fetch_cache"):
            self._alltrails_fetch_cache = {}
        if not hasattr(self, "_alltrails_fetch_lock"):
            self._alltrails_fetch_lock = Lock()
        if not hasattr(self, "_alltrails_last_request_ts"):
            self._alltrails_last_request_ts = 0.0
        if not hasattr(self, "_alltrails_blocked_until_ts"):
            self._alltrails_blocked_until_ts = 0.0

        delay_seconds = float(
            getattr(
                self,
                "_alltrails_request_delay_seconds",
                DEFAULT_ALLTRAILS_REQUEST_DELAY_SECONDS,
            )
            or 0.0
        )
        cooldown_seconds = float(
            getattr(
                self,
                "_alltrails_block_cooldown_seconds",
                DEFAULT_ALLTRAILS_BLOCK_COOLDOWN_SECONDS,
            )
            or 0.0
        )

        with self._alltrails_fetch_lock:
            cached = self._alltrails_fetch_cache.get(url)
            if cached is not None:
                return cached

            now = time.monotonic()
            blocked_until = float(getattr(self, "_alltrails_blocked_until_ts", 0.0) or 0.0)
            if blocked_until > now:
                # AllTrails' bot-detection block is DataDome-driven and tends to
                # be sustained rather than a simple time-window rate limit --
                # sleeping out the cooldown and probing again almost always
                # just fails again. Return a synthetic blocked result
                # immediately (uncached, so a later call after the cooldown
                # naturally expires still gets a real attempt) instead of
                # tying up a worker thread waiting to re-fail.
                return False, 403, ""

            if delay_seconds > 0:
                last_request = float(getattr(self, "_alltrails_last_request_ts", 0.0) or 0.0)
                elapsed = time.monotonic() - last_request
                if elapsed < delay_seconds:
                    time.sleep(delay_seconds - elapsed)

            self._alltrails_last_request_ts = time.monotonic()
            result = self._fetch_page_text_uncached(url, timeout=timeout)

            status = result[1]
            if isinstance(status, int) and status in (401, 403) and cooldown_seconds > 0:
                self._alltrails_blocked_until_ts = time.monotonic() + cooldown_seconds

            self._alltrails_fetch_cache[url] = result
            self._mark_persistent_cache_dirty()
            return result

    def _fetch_wayback_alltrails_text(self, url: str, timeout: int = 8) -> tuple[bool, int | str, str]:
        """Fallback for _alltrails_geo_maps_url when a direct AllTrails fetch
        is bot-blocked (see _fetch_alltrails_text's own comment on
        DataDome): looks up the most recent good archived snapshot of `url`
        via the Wayback Machine's CDX Server API and fetches that
        snapshot's HTML instead. archive.org's own crawler isn't the
        automated traffic DataDome is trying to block, and it stores the
        ORIGINAL page HTML (including JSON-LD) at crawl time.

        ROOT CAUSE of dipstick72's 0/20 fires (found 2026-08-18 by live
        reproduction against real trail URLs, not by re-reading the code):
        this originally called the single-URL `https://archive.org/wayback
        /available?url=<trail-url>` "availability" helper API. That
        endpoint is a *shared, low-quota* archive.org service (a different
        host/service than the CDX search below) that returns HTTP 429 for
        long stretches under completely ordinary, humble request volumes --
        reproduced live: a clean, correctly-1-req/sec-paced, single-process
        run against all 20 real dipstick72 trail URLs got 429 on all 20/20,
        and even isolated single lookups kept 429ing for 60+ seconds
        straight afterwards with no code-side retry. Nothing in this
        module's call path raises or logs on that failure (by design, this
        function fails closed silently, and _alltrails_geo_maps_url's own
        contract is "return None on any fetch failure, leave maps_url
        alone") -- so a 429 that lasts the entire ~20-second span of a
        destination's real audit pass silently zeroes out every single
        item, with no exception and no log line anywhere. That is exactly
        the dipstick72 signature: 0/20 fires, no "wayback" string anywhere
        in run-console.log, no traceback. The feature's own unit tests
        never caught this because every one of them mocks _fetch_page_text/
        _fetch_wayback_alltrails_text directly and never makes a real
        request to archive.org at all (see test_url_discovery.py's
        test_alltrails_geo_maps_url_* tests).

        Fix: use the Wayback Machine's CDX Server API
        (`https://web.archive.org/cdx/search/cdx`) instead -- confirmed
        live to be a separate host/quota from the `archive.org/wayback/
        available` endpoint above: while `archive.org/wayback/available`
        was mid-429 (reproduced continuously for 60+ seconds), the CDX
        endpoint answered every query with a real 200 and correct snapshot
        data, for the exact same trail URLs, in the exact same window. CDX
        also lets the query filter to `statuscode:200` and take the most
        recent matching snapshot directly (`limit=-N`, ascending by
        timestamp) -- strictly better than the old availability API's
        "closest" snapshot, which could hand back an archived DataDome
        block page (also reproduced live: a `web.archive.org/web/2024/
        <url>` redirect landed on a 2025-08-11 snapshot that was itself a
        403 bot-block capture, not real trail content).

        Live-verified end-to-end via this exact path (2026-08-18, CDX +
        snapshot fetch + _extract_alltrails_geo_from_html, real network,
        no mocks): Double Arch Trail (Moab) -> (38.68828, -109.53838);
        Mesa Arch (Moab) -> (38.38909, -109.86796) -- the identical
        coordinate the original live-verification agent found before this
        feature was merged, confirming the extraction logic itself was
        always correct and only the availability lookup was the failure
        point.

        A trail recrawled recently by archive.org (2024 onward, based on
        spot checks) carries the SAME `<script type="application/ld+json">`
        `geo` block _extract_alltrails_geo_from_html already parses --
        confirmed against a real 2026-01-08 snapshot (american-samoa/
        tutuila/lower-sauma-ridge-trail). A trail whose only archived
        snapshot predates AllTrails' JSON-LD rollout (seen in 2023-and-
        earlier snapshots, e.g. Hickman Bridge Trail's only snapshot is from
        2023-07-10) used schema.org *microdata* instead (`itemprop="geo"`
        `<meta itemprop="latitude"|"longitude">` tags, no ld+json at all) --
        that will not yield a coordinate through this path. That is a
        fail-closed miss, not a bug, per this module's no-invented-data
        rule: _extract_alltrails_geo_from_html is reused as-is rather than
        adding a second, microdata-specific parser for it. The
        `statuscode:200` CDX filter reduces how often this happens (an old
        DataDome-blocked-at-crawl-time snapshot is excluded outright), but
        a genuinely old good snapshot can still predate the JSON-LD
        rollout.

        Deliberately does NOT route through _fetch_page_text/
        _fetch_alltrails_text: both archive.org URLs used here (the CDX
        query and the snapshot URL itself) contain the literal substrings
        "alltrails.com" and "/trail/" (the original AllTrails URL is
        embedded in each), so _is_alltrails_trail_url's plain substring
        check would misroute them into AllTrails' own request-pacing/
        block-cooldown state even though the actual HTTP request goes to a
        completely different host (archive.org / web.archive.org) -- and
        this fallback specifically runs right after a direct AllTrails
        fetch just failed, i.e. exactly when that cooldown is most likely
        to be active. Goes straight to _fetch_page_text_uncached instead,
        with its own independent cache/pacing/persistence
        (_wayback_fetch_cache, mirroring _alltrails_fetch_cache's shape and
        persistence pattern).
        """
        if not hasattr(self, "_wayback_fetch_cache"):
            self._wayback_fetch_cache = {}
        if not hasattr(self, "_wayback_fetch_lock"):
            self._wayback_fetch_lock = Lock()
        if not hasattr(self, "_wayback_last_request_ts"):
            self._wayback_last_request_ts = 0.0

        delay_seconds = float(
            getattr(self, "_wayback_request_delay_seconds", DEFAULT_WAYBACK_REQUEST_DELAY_SECONDS) or 0.0
        )

        with self._wayback_fetch_lock:
            cached = self._wayback_fetch_cache.get(url)
            if cached is not None:
                return cached

            def _single_fetch(target_url: str) -> tuple[bool, int | str, str]:
                if delay_seconds > 0:
                    last_request = float(getattr(self, "_wayback_last_request_ts", 0.0) or 0.0)
                    elapsed = time.monotonic() - last_request
                    if elapsed < delay_seconds:
                        time.sleep(delay_seconds - elapsed)
                self._wayback_last_request_ts = time.monotonic()
                return self._fetch_page_text_uncached(target_url, timeout=timeout)

            def _paced_fetch(target_url: str) -> tuple[bool, int | str, str]:
                # One retry on a transient-looking failure (429, 5xx, or a
                # read timeout) after a short pause -- live-reproduced
                # 2026-08-18: archive.org's web.archive.org host (both the
                # CDX search above and the snapshot playback below) goes
                # through short stretches of 429s/timeouts that clear up
                # within seconds on their own (a request that 429s can
                # succeed on a plain retry moments later; a request that
                # read-timed-out at 8s can succeed well within a second
                # retry). A single retry is cheap insurance against exactly
                # that pattern without turning a real, durable failure (no
                # snapshot, invalid URL) into a slow one.
                result = _single_fetch(target_url)
                ok, status, _text = result
                if ok:
                    return result
                transient = status == 429 or (isinstance(status, int) and status >= 500) or (
                    isinstance(status, str) and "timed out" in status.lower()
                )
                if not transient:
                    return result
                time.sleep(2.0)
                return _single_fetch(target_url)

            # CDX Server API, not the archive.org/wayback/available helper --
            # see this function's docstring for the live-reproduced evidence
            # that the availability helper is a separate, much lower-quota
            # host/service that 429s under ordinary production request
            # volume while CDX keeps working. limit=-5 asks for (up to) the
            # 5 most recent statuscode:200 snapshots, returned oldest-first,
            # so rows[-1] is the newest usable one -- newer snapshots are
            # both more likely to carry the JSON-LD geo block (see docstring)
            # and less likely to be stale trail data.
            cdx_url = (
                "https://web.archive.org/cdx/search/cdx?url="
                f"{quote(url, safe=':/')}&output=json&filter=statuscode:200&limit=-5"
            )
            ok, status, body = _paced_fetch(cdx_url)

            snapshot_url = ""
            if ok and body:
                try:
                    rows = json.loads(body)
                except (json.JSONDecodeError, TypeError):
                    rows = None
                if isinstance(rows, list) and len(rows) >= 2 and isinstance(rows[0], list):
                    header = rows[0]
                    try:
                        ts_idx = header.index("timestamp")
                        orig_idx = header.index("original")
                    except ValueError:
                        ts_idx = orig_idx = -1
                    if ts_idx >= 0 and orig_idx >= 0:
                        last_row = rows[-1]
                        if isinstance(last_row, list) and len(last_row) > max(ts_idx, orig_idx):
                            timestamp = str(last_row[ts_idx] or "").strip()
                            original = str(last_row[orig_idx] or "").strip()
                            if timestamp and original:
                                snapshot_url = f"https://web.archive.org/web/{timestamp}/{original}"

            if not snapshot_url:
                # No archived snapshot (or the CDX lookup itself failed) --
                # cache the miss in memory for this run only (never
                # persisted to disk, see _save_persistent_caches), since it
                # may be a transient archive.org hiccup rather than a
                # durable "never archived" answer.
                result: tuple[bool, int | str, str] = (False, status if not ok else "no_snapshot", "")
                self._wayback_fetch_cache[url] = result
                self._mark_persistent_cache_dirty()
                return result

            snap_ok, snap_status, snap_text = _paced_fetch(snapshot_url)
            result = (bool(snap_ok and snap_text), snap_status, str(snap_text or ""))
            self._wayback_fetch_cache[url] = result
            self._mark_persistent_cache_dirty()
            return result

    @staticmethod
    def _is_alltrails_trail_url(url: str) -> bool:
        lower = (url or "").lower()
        return "alltrails.com" in lower and "/trail/" in lower

    @classmethod
    def _alltrails_slug_matches_item(cls, url: str, item_name: str) -> bool:
        item_tokens = cls._significant_tokens(item_name)
        if not item_tokens:
            return True
        raw_slug = unquote(urlparse(url).path.rsplit("/", 1)[-1]).lower()
        slug = raw_slug.replace("-", " ")
        slug_tokens = cls._significant_tokens(slug)
        if not slug_tokens:
            return False
        item_set = set(item_tokens)
        slug_set = set(slug_tokens)
        overlap = len(item_set & slug_set)
        required = cls._required_alltrails_token_matches(len(item_tokens))
        if overlap < required:
            return False

        # AllTrails' own naming convention for a joined/combined route is
        # "trail-a-to-trail-b" (real dipstick69 case: Bryce Canyon's "Navajo
        # Loop Trail", a real ~1.3mi loop, token-overlap-matched the slug
        # "navajo-loop-trail-to-peekaboo-loop" because "navajo"/"loop"/"trail"
        # are all subset tokens of it -- even though that page is actually a
        # different, ~5.3mi combined route through two joined trails, a real
        # distance mismatch against the item's own stated length). A trip
        # owner's own seed name can legitimately describe a combined route
        # (e.g. "Bear Lake to Emerald Lake"), so only reject when the item
        # name itself doesn't already contain "to".
        if "-to-" in raw_slug and not re.search(r"\bto\b", (item_name or "").lower()):
            return False

        # Use raw tokens for trail/loop: _significant_tokens excludes them as stop words
        # but they carry real semantic meaning ("Bryce Point Trail" ≠ "bryce-point").
        _trail_descriptors = {"trail", "loop"}
        _item_raw = set(re.findall(r"[a-z]+", (item_name or "").lower()))
        _slug_raw = set(slug.split())
        if _trail_descriptors & _item_raw and not (_trail_descriptors & _slug_raw):
            return False

        generic_trail_tokens = {
            "trail",
            "loop",
            "falls",
            "fall",
            "river",
            "creek",
            "mountain",
            "peak",
            "point",
            "canyon",
            "overlook",
            "vista",
            "ridge",
            "lake",
            "pass",
        }
        anchor_tokens = [token for token in item_tokens if token not in generic_trail_tokens]
        if anchor_tokens and not any(token in slug_set for token in anchor_tokens):
            return False
        return True

    @staticmethod
    def _required_alltrails_token_matches(token_count: int) -> int:
        if token_count <= 1:
            return 1
        return max(2, ceil(token_count / 2))

    @staticmethod
    def _required_general_token_matches(token_count: int) -> int:
        if token_count <= 1:
            return 1
        return max(2, ceil(token_count / 2))

    @classmethod
    def _text_matches_item_tokens(cls, text: str, item_tokens: list[str]) -> bool:
        if not item_tokens:
            return True
        overlap = sum(1 for token in item_tokens if token in text)
        return overlap >= cls._required_general_token_matches(len(item_tokens))

    @classmethod
    def _candidate_text_matches_item_tokens(
        cls,
        candidate: dict[str, Any] | None,
        item_tokens: list[str],
    ) -> bool:
        if not candidate:
            return False
        text = cls._candidate_text_blob(candidate)
        return cls._text_matches_item_tokens(text, item_tokens)

    @classmethod
    def _direct_batch_row_matches_item(
        cls,
        row: dict[str, Any] | None,
        item_name: str,
        dest_name: str = "",
    ) -> bool:
        return cls._direct_batch_row_match_strength(row, item_name, dest_name) > 0

    @classmethod
    def _direct_batch_row_match_strength(
        cls,
        row: dict[str, Any] | None,
        item_name: str,
        dest_name: str = "",
    ) -> int:
        """0 = no match, 1 = weak (single short-name anchor token only), 2 = strong
        (full required token overlap via description/snippet/URL text, or an
        AllTrails slug match), 3 = exact (every word of the item's name --
        minus generic descriptive suffixes -- appears in the row's own
        declared name).

        Corroboration signal: when a batch has multiple ambiguously-matching rows
        for the same item name (e.g. two different "* Temple" entries in a
        "St. George" destination once the shared destination-name token is
        excluded), the row reached only through the lenient single-anchor-token
        fallback must not out-rank -- or get pooled equally with -- a row that
        actually satisfies the full token-overlap bar. Selection logic should
        prefer strength-3 rows over strength-2 rows, and strength-2 rows over
        strength-1 rows, when disambiguating.
        """
        if not row:
            return 0

        item_tokens = cls._significant_tokens(item_name)
        if not item_tokens:
            return 2

        raw_url = str(row.get("url", "") or "").strip()
        if raw_url and cls._is_alltrails_trail_url(raw_url):
            return 2 if cls._alltrails_slug_matches_item(raw_url, item_name) else 0

        # The row's own declared name (unlike the full blob below, which
        # includes snippet/URL text prone to incidental substring pollution --
        # e.g. a "Links: https://www.nps.gov/zion/..." trailer makes "zion"
        # match essentially any row for a "Zion National Park" destination,
        # and "canyon" is a substring of "canyons" -- regardless of what the
        # row actually is) is a deliberate, low-risk identifier. If *every*
        # word of the item's name (minus generic descriptive suffixes like
        # "Overlook"/"View") is present in the row's own name, that is a real
        # match even when one of those words happens to coincide with the
        # destination's own name -- e.g. "Bryce Point" genuinely starting with
        # "Bryce" for a "Bryce Canyon" destination; excluding "bryce" below
        # (as a destination-name token) would otherwise leave no way to match
        # "Bryce Point Overlook" at all. This uses raw words (not
        # _significant_tokens, which drops "Point") and requires the *whole*
        # remaining phrase, not a single token, precisely so a single shared
        # word like "bryce" alone can't match an unrelated row such as "Bryce
        # Canyon Lodge".
        #
        # This exact/full-name match is checked -- and ranked -- ahead of the
        # generic blob-overlap check just below because two rows can each
        # satisfy that check's lower bar by sharing only a couple of
        # generic, non-distinctive words with the item (dipstick59: real
        # Zion National Park attraction-batch rows "Zion Canyon Visitor
        # Center" and even "Kolob Canyons Visitor Center" both matched
        # "Zion Canyon Scenic Drive" at the old single "strong" tier purely
        # via shared "Zion"/"Canyon" text, tying with -- and in the final
        # ranking beating -- the row that is actually an exact title match).
        # A same-destination row that only shares generic words must not
        # tie with the row that is the item, word for word.
        row_name_only = str(row.get("name") or row.get("title") or "").strip()
        if row_name_only:
            item_raw_words = set(re.findall(r"[a-z]+", item_name.lower())) - GENERIC_VIEWPOINT_SUFFIX_TOKENS - _MATCH_STOPWORDS
            row_name_raw_words = set(re.findall(r"[a-z]+", row_name_only.lower()))
            if len(item_raw_words) >= 2 and item_raw_words <= row_name_raw_words:
                return 3

        if cls._candidate_text_matches_item_tokens(row, item_tokens):
            return 2

        # Destination-name tokens are not distinctive of a specific item -- e.g.
        # for a "St. George, Utah" destination, "george" trivially appears in
        # almost every row's address text (via the maps query string), so the
        # short-name anchor fallback below must not treat a shared
        # destination-name word alone as proof of an item match.
        dest_tokens = set(cls._significant_tokens(dest_name)) if dest_name else set()
        anchor_tokens = [
            t for t in item_tokens
            if t not in dest_tokens and t not in _GENERIC_ANCHOR_EXCLUDED_TOKENS
        ]

        row_blob = cls._candidate_text_blob(row)
        if len(item_tokens) <= 2 and anchor_tokens:
            anchor_overlap = sum(1 for token in anchor_tokens if len(token) >= 5 and token in row_blob.lower())
            if anchor_overlap >= 1:
                return 1

        parsed = urlparse(raw_url)
        query = parse_qs(parsed.query)
        query_parts: list[str] = []
        for key in ("query", "q", "name", "destination"):
            query_parts.extend(query.get(key, []))

        decoded_url = unquote(raw_url).replace("+", " ").lower()
        haystack = " ".join(
            [
                decoded_url,
                unquote(parsed.path or "").replace("-", " ").replace("_", " ").lower(),
                " ".join(unquote(v).replace("+", " ") for v in query_parts).lower(),
            ]
        )
        overlap = sum(1 for token in item_tokens if token in haystack)
        if overlap >= cls._required_general_token_matches(len(item_tokens)):
            return 2
        if len(item_tokens) <= 2 and anchor_tokens:
            anchor_overlap = sum(1 for token in anchor_tokens if len(token) >= 5 and token in haystack)
            if anchor_overlap >= 1:
                return 1
        return 0

    @classmethod
    def _direct_batch_url_matches_item(cls, url: str | None, item_name: str, dest_name: str = "") -> bool:
        candidate = str(url or "").strip()
        if not candidate:
            return False

        item_tokens = cls._significant_tokens(item_name)
        if not item_tokens:
            return True

        parsed = urlparse(candidate)
        query = parse_qs(parsed.query)
        query_parts: list[str] = []
        for key in ("query", "q", "name", "destination"):
            query_parts.extend(query.get(key, []))

        decoded_url = unquote(candidate).replace("+", " ").lower()
        haystack = " ".join(
            [
                decoded_url,
                unquote(parsed.path or "").replace("-", " ").replace("_", " ").lower(),
                " ".join(unquote(v).replace("+", " ") for v in query_parts).lower(),
            ]
        )
        overlap = sum(1 for token in item_tokens if token in haystack)
        if overlap >= cls._required_general_token_matches(len(item_tokens)):
            return True
        # Destination-name tokens (e.g. "george" for "St. George, Utah") trivially
        # appear in almost every candidate URL's address query string, so they
        # must not count as the sole anchor token proving a specific-item match.
        dest_tokens = set(cls._significant_tokens(dest_name)) if dest_name else set()
        anchor_tokens = [
            t for t in item_tokens
            if t not in dest_tokens and t not in _GENERIC_ANCHOR_EXCLUDED_TOKENS
        ]
        if len(item_tokens) <= 2 and anchor_tokens:
            anchor_overlap = sum(1 for token in anchor_tokens if len(token) >= 5 and token in haystack)
            return anchor_overlap >= 1
        return False

    @classmethod
    def _candidate_text_matches_destination_tokens(
        cls,
        candidate: dict[str, Any] | None,
        dest_name: str,
    ) -> bool:
        if not candidate:
            return False
        dest_tokens = cls._significant_tokens(dest_name)
        if not dest_tokens:
            return False
        text = cls._candidate_text_blob(candidate)
        return any(token in text for token in dest_tokens[:2])

    @classmethod
    def _candidate_mentions_conflicting_destination(
        cls,
        candidate: dict[str, Any] | None,
        dest_name: str,
        *,
        item_name: str | None = None,
    ) -> bool:
        if not candidate:
            return False
        text = cls._candidate_text_blob(candidate)
        if not text:
            return False

        if item_name and cls._direct_batch_row_matches_item(candidate, item_name, dest_name):
            return False

        dest_tokens = set(cls._significant_tokens(dest_name))
        if not dest_tokens:
            return False
        if any(token in text for token in list(dest_tokens)[:3]):
            return False

        # If a row explicitly names a different park/monument than the active
        # destination, treat it as off-target to avoid cross-destination leakage.
        for match in re.finditer(
            r"\b([a-z][a-z'\-]*(?:\s+[a-z][a-z'\-]*){0,3})\s+(national park|state park|national monument)\b",
            text,
        ):
            mention_tokens = set(cls._significant_tokens(match.group(1)))
            if mention_tokens and mention_tokens.isdisjoint(dest_tokens):
                return True
        return False

    @classmethod
    def _candidate_has_alltrails_listing_signal(cls, candidate: dict[str, Any] | None) -> bool:
        if not candidate:
            return False
        text = cls._candidate_text_blob(candidate)
        if "alltrails" not in text:
            return False
        return any(marker in text for marker in ("review", "rating", "map", "photos"))

    @classmethod
    def _is_noisy_alltrails_slug(cls, url: str, item_name: str) -> bool:
        parsed = urlparse(url)
        slug = unquote(parsed.path.rsplit("/", 1)[-1]).lower()
        if "-via-" in slug:
            return True
        if cls._alltrails_slug_has_numbered_suffix(url):
            return True
        # Use raw tokens for trail/loop since _significant_tokens treats them as stop words.
        _trail_descriptors = {"trail", "loop"}
        _item_raw = set(re.findall(r"[a-z]+", (item_name or "").lower()))
        _slug_raw = set(unquote(parsed.path.rsplit("/", 1)[-1]).lower().split("-"))
        if _trail_descriptors & _item_raw and not (_trail_descriptors & _slug_raw):
            return True
        return cls._alltrails_slug_extra_term_count(url, item_name) >= 1

    @staticmethod
    def _alltrails_slug_has_numbered_suffix(url: str) -> bool:
        slug = unquote(urlparse(url).path.rsplit("/", 1)[-1]).lower()
        return bool(re.search(r"--\d+$", slug))

    @staticmethod
    def _alltrails_slug_extra_term_count(url: str, item_name: str) -> int:
        slug = unquote(urlparse(url).path.rsplit("/", 1)[-1]).replace("-", " ")
        slug_tokens = URLDiscoverer._significant_tokens(slug)
        item_tokens = URLDiscoverer._significant_tokens(item_name)
        if not slug_tokens or not item_tokens:
            return 0
        item_set = set(item_tokens)
        return sum(1 for token in slug_tokens if token not in item_set)

    @staticmethod
    def _extract_trail_miles(text: str) -> float | None:
        if not text:
            return None
        lower_text = text.lower()
        _word_miles = {
            "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
            "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
        }
        mile_match = re.search(r"\b(\d+(?:\.\d+)?)[ \-\u2013\u2014]*(?:mile|miles|mi)\b", lower_text)
        if mile_match:
            try:
                return float(mile_match.group(1))
            except (TypeError, ValueError):
                pass
        word_match = re.search(
            r"\b(" + "|".join(_word_miles) + r")[ \-]*(?:mile|miles|mi)\b", lower_text
        )
        if word_match:
            return float(_word_miles[word_match.group(1)])
        km_match = re.search(r"\b(\d+(?:\.\d+)?)[ \-\u2013\u2014]*(?:km|kilometer|kilometers|kilometre|kilometres)\b", lower_text)
        if km_match:
            try:
                return float(km_match.group(1)) * 0.621371
            except (TypeError, ValueError):
                return None
        return None

    @staticmethod
    def _build_trail_threshold_note(*, miles: float | None, max_miles: float | None) -> str:
        try:
            miles_f = float(miles) if miles is not None else None
        except (TypeError, ValueError):
            miles_f = None
        try:
            max_f = float(max_miles) if max_miles is not None else None
        except (TypeError, ValueError):
            max_f = None

        if miles_f is None or max_f is None or max_f <= 0:
            return ""

        miles_text = f"{miles_f:.1f}".rstrip("0").rstrip(".")
        max_text = f"{max_f:.1f}".rstrip("0").rstrip(".")
        return (
            f"Trail distance is about {miles_text} miles, which exceeds the configured {max_text}-mile threshold; "
            "review route demands and permits before committing."
        )

    @staticmethod
    def _candidate_text_blob(candidate: dict[str, Any] | None) -> str:
        if not candidate:
            return ""
        return " ".join(
            [
                str(candidate.get("name", "") or ""),
                str(candidate.get("title", "") or ""),
                str(candidate.get("snippet", "") or ""),
                str(candidate.get("description", "") or ""),
            ]
        ).lower()

    @classmethod
    def _extract_urls_from_text(cls, text: str) -> list[str]:
        raw_text = str(text or "")
        if not raw_text:
            return []

        extracted: list[str] = []
        for match in TEXT_URL_RE.findall(raw_text):
            candidate = match.strip().rstrip(".,;:!?")
            while candidate.endswith(")") and candidate.count("(") < candidate.count(")"):
                candidate = candidate[:-1]
            while candidate.endswith("]") and candidate.count("[") < candidate.count("]"):
                candidate = candidate[:-1]
            normalized = cls._normalize_direct_batch_authoritative_url(candidate)
            if normalized:
                extracted.append(normalized)
        return extracted

    @classmethod
    def _direct_batch_row_url_candidates(cls, row: dict[str, Any] | None) -> list[str]:
        if not row:
            return []

        candidates: list[str] = []
        seen: set[str] = set()

        def remember(url: str | None) -> None:
            normalized = cls._normalize_direct_batch_authoritative_url(url)
            if not normalized or normalized in seen:
                return
            seen.add(normalized)
            candidates.append(normalized)

        remember(row.get("url", ""))
        remember(row.get("maps_url", ""))

        for key in ("name", "title", "snippet", "description"):
            for extracted in cls._extract_urls_from_text(str(row.get(key, "") or "")):
                remember(extracted)

        return candidates

    @classmethod
    def _direct_batch_row_quality_metadata_for_url(
        cls, rows: list[dict[str, Any]], url: str | None
    ) -> dict[str, Any]:
        """Carry rating/vote/cuisine/price metadata from an already-harvested
        direct-batch row onto the item whose url we just accepted -- at zero
        extra network cost, since these rows were already fetched to resolve
        that url in the first place. Without this, only items built via the
        batch-shortfall padding path (_build_primary_items_from_direct_batch)
        ever got this data; every other per-item resolution site silently
        discarded it -- including the "existing URL already attached, just
        validate it" shortcuts (direct_batch_existing_url_preserved), which
        is how e.g. "Red Hills Desert Garden" ended up with no rating badge
        at all despite its harvested row carrying a 4.8 rating (dipstick55
        Theme D)."""
        normalized_url = cls._normalize_direct_batch_authoritative_url(url or "")
        if not normalized_url:
            return {}
        for row in rows:
            if row.get("rating") is None and not row.get("raw_rating") and row.get("votes") is None:
                continue
            if normalized_url in cls._direct_batch_row_url_candidates(row):
                return {
                    key: row.get(key)
                    for key in ("rating", "raw_rating", "votes", "source_type", "cuisine", "price_range")
                    if row.get(key) not in (None, "")
                }
        return {}

    @staticmethod
    def _has_alltrails_closure_marker(text: str) -> bool:
        # Strip HTML comments / <script>/<style> content first, for the same
        # reason as _has_attraction_closure_marker below: both callers that
        # matter here (_alltrails_url_meets_seed_relaxed_standard and the
        # audit-path check in _passes_alltrails_post_search_filters) pass raw
        # fetched page text from _fetch_page_text/_fetch_alltrails_text,
        # which is URLValidator.get_text's unmodified resp.text -- the same
        # fetch mechanism the attraction path used before that false
        # positive was found and fixed. AllTrails pages are React/Next.js
        # rendered, so their raw (unrendered) HTML is unusually script- and
        # JSON-hydration-heavy, making incidental non-visible closure-phrase
        # matches at least as plausible here as on a typical venue site.
        # (No AllTrails-specific partial-closure exemption is applied here:
        # unlike the attraction wing/gallery/exhibit case, there is no
        # confirmed real-world evidence of an analogous partial-closure
        # pattern on trail pages, so inventing a scope-marker list would be
        # speculative rather than evidence-based.)
        lower_text = _strip_non_visible_html_noise(str(text or "")).lower()
        if not lower_text:
            return False
        return any(marker in lower_text for marker in ALLTRAILS_CLOSURE_MARKERS)

    @staticmethod
    def _has_attraction_closure_marker(text: str) -> bool:
        # Strip HTML comments / <script>/<style> content first: raw fetched
        # page text (URLValidator.get_text returns resp.text unmodified) can
        # contain a closure marker phrase inside markup that is never
        # visible to a real visitor -- e.g. a stale dev comment left on the
        # page. Matching against that is a false positive, not evidence the
        # attraction is closed.
        lower_text = _strip_non_visible_html_noise(str(text or "")).lower()
        if not lower_text:
            return False
        if not any(marker in lower_text for marker in ATTRACTION_CLOSURE_MARKERS):
            return False
        # A marker exists somewhere on the (comment-stripped) page, but a
        # real, page-specific partial-closure notice -- a wing/gallery/
        # exhibit closed for repair -- does not mean the whole attraction is
        # closed. Evaluate at sentence granularity: if every sentence that
        # contains a closure marker also names a specific sub-part of the
        # venue, treat this as a partial closure rather than a whole-
        # attraction closure. This is a coarse, deliberately simple
        # heuristic (same-sentence co-occurrence, not deep NLP) -- it only
        # suppresses a match when the sub-part language sits in the exact
        # same sentence as the marker, to keep the false-negative risk (a
        # real full closure being missed) low.
        sentences = re.split(r"(?<=[.!?])\s+|\n+", lower_text)
        saw_marker_sentence = False
        for sentence in sentences:
            if not any(marker in sentence for marker in ATTRACTION_CLOSURE_MARKERS):
                continue
            saw_marker_sentence = True
            if not any(scope in sentence for scope in ATTRACTION_PARTIAL_CLOSURE_SCOPE_MARKERS):
                return True
        if not saw_marker_sentence:
            # Marker text spanned a sentence-splitting boundary in some
            # unexpected way -- fail closed (treat as a closure) rather than
            # silently dropping a real match, matching this check's
            # original whole-text behavior.
            return True
        return False

    @staticmethod
    def _is_under_construction_page(text: str) -> bool:
        """True when a fetched page's own text says it is a placeholder /
        stub rather than real content -- e.g. NPS's "Page In-Progress"
        pages, which pass every other liveness/relevance check (200 status,
        correct domain, mentions the destination and even the item's own
        name) because they genuinely are the right, live URL for that
        entity -- it just isn't populated yet. Real example (Bryce Canyon
        eval run): "Bryce Canyon Visitor Center" linked to
        nps.gov/brca/planyourvisit/visitorcenters.htm, whose entire visible
        content is "Page In-Progress -- This page is currently being worked
        on. Please check back later." None of the existing generic-URL or
        closure-marker checks catch this: the URL shape is unremarkable and
        the page isn't reporting the venue as closed, just empty.
        """
        lower_text = _strip_non_visible_html_noise(str(text or "")).lower()
        if not lower_text:
            return False
        return any(marker in lower_text for marker in UNDER_CONSTRUCTION_PAGE_MARKERS)

    @staticmethod
    def _is_generic_listing_title(text: str) -> bool:
        candidate = str(text or "").strip()
        if not candidate:
            return False
        return any(pattern.search(candidate) for pattern in GENERIC_LISTING_TITLE_PATTERNS)

    @staticmethod
    def _is_obviously_generic_url(lower_url: str) -> bool:
        if "yelp.com/search" in lower_url:
            return True
        for marker in GENERIC_BAD_URL_MARKERS:
            if marker not in lower_url:
                continue
            # NPS detail pages often live under /planyourvisit/<topic>. Allow
            # those through; generic section roots remain blocked later.
            if marker in {"/planyourvisit", "/plan-your-visit"} and "nps.gov" in lower_url:
                continue
            return True
        return False

    @staticmethod
    def _is_generic_directions_url(url: str) -> bool:
        """Return True for pages that are navigation/directions endpoints, not specific place pages."""
        lower = str(url or "").lower()
        path = urlparse(lower).path
        # NPS /planyourvisit/directions* pages are route guides, not specific stops.
        if "nps.gov" in lower and "/planyourvisit/" in path and "direction" in path:
            return True
        generic_endings = ("/directions", "/directions.htm", "/directions.asp", "/directions.html", "/how-to-get-here")
        return any(path.rstrip("/").endswith(e) for e in generic_endings)

    def _update_route_distance_and_time(
        self,
        *,
        ai: dict[str, Any],
        getting_here: dict[str, Any],
        origin_name: str,
        dest_name: str,
        origin_lat: Any,
        origin_lng: Any,
        dest_lat: Any,
        dest_lng: Any,
        dest: dict[str, Any] | None = None,
    ) -> None:
        """Overwrite AI-generated distance/time with values derived from the real route.

        Not on a transit leg. `best_time` below is a scraped Google DRIVING
        duration or a 60 mph Haversine estimate, and the overwrite condition
        fires when `distance_miles` is empty -- which is exactly the state a
        transit leg is supposed to be in, road miles being meaningless there.
        Without this return a real 3h15 rail duration is silently replaced by
        a car estimate two stages later (multimodal-routing.md 4.1).

        Belt and braces: the only caller reaches this after an early return
        that already covers booked non-road arrivals. That return is about
        en-route stops and could move; this one is about the number.
        """
        # is_road_leg rather than "is it transit": a scraped DRIVING duration
        # describes a bicycle no better than it describes a train. Reachable
        # on a bike leg, which keeps its en-route discovery and so does not
        # take the early return above.
        if not leg_mode(dest).is_road_leg:
            logger.info(
                "  Route distance/time left alone for '%s': %s leg, not a drive",
                dest_name, leg_mode(dest).declared,
            )
            return
        fetched_miles: float | None = None
        fetched_time: str | None = None
        if bool(
            getattr(self, "_route_distance_live_fetch_enabled", DEFAULT_ROUTE_DISTANCE_LIVE_FETCH_ENABLED)
        ):
            # Build the same route URL the assembler will render so we fetch the real data.
            stops = getting_here.get("en_route_stops", []) or []
            waypoint_names = [str(s.get("name", "") or "") for s in stops[:8] if s.get("name")]
            params = [f"destination={quote(dest_name)}", "travelmode=driving", "api=1"]
            if origin_name:
                params.append(f"origin={quote(origin_name)}")
            if waypoint_names:
                params.append("waypoints=" + quote("|".join(waypoint_names), safe="|"))
            route_url = "https://www.google.com/maps/dir/?" + "&".join(params)

            fetched_miles, fetched_time = self._parse_route_info_from_maps_html(route_url)

        haversine_miles, haversine_time = self._estimate_route_from_haversine(
            origin_lat, origin_lng, dest_lat, dest_lng
        )

        # Prefer fetched data; fall back to Haversine estimate.
        best_miles = fetched_miles or haversine_miles
        best_time = fetched_time or haversine_time

        if not best_miles and not best_time:
            return

        current_miles_raw = str(getting_here.get("distance_miles", "") or "").strip()
        current_time_raw = str(getting_here.get("travel_time", "") or "").strip()

        try:
            current_miles = float(re.sub(r"[^\d.]", "", current_miles_raw)) if current_miles_raw else None
        except ValueError:
            current_miles = None

        # Overwrite when the AI value is missing or deviates >25% from our estimate.
        if best_miles:
            if not current_miles or abs(current_miles - best_miles) / best_miles > 0.25:
                getting_here["distance_miles"] = str(int(round(best_miles)))
                ai["getting_here"] = getting_here
                logger.info("  Route distance updated to %d mi (was '%s')", int(round(best_miles)), current_miles_raw)
        if best_time and (not current_time_raw or not current_miles):
            getting_here["travel_time"] = best_time
            ai["getting_here"] = getting_here
            logger.info("  Route travel_time updated to '%s' (was '%s')", best_time, current_time_raw)

    def _parse_route_info_from_maps_html(self, route_url: str) -> tuple[float | None, str | None]:
        """Fetch Google Maps directions HTML and extract distance (miles) and duration."""
        ok, _status, html = self._fetch_page_text(route_url, timeout=10)
        if not ok or not html:
            return None, None

        miles: float | None = None
        duration_text: str | None = None

        # Distance — Google embeds values like "125 mi" in various places in the HTML/JSON
        for pat in (
            r'"(\d+(?:\.\d+)?)\s*mi"',
            r'(\d+(?:\.\d+)?)\s*mi(?:les?)?(?:["\]},\s])',
            r'"distanceText"\s*:\s*"(\d+(?:\.\d+)?)\s*mi',
        ):
            m = re.search(pat, html, re.IGNORECASE)
            if m:
                try:
                    candidate = float(m.group(1))
                    if 1 < candidate < 5000:
                        miles = candidate
                        break
                except ValueError:
                    pass

        # Duration — look for patterns like "1 hr 49 min" or "2 hours 15 minutes"
        for pat in (
            r'"(\d+)\s*hr\s*(\d+)\s*min"',
            r'"(\d+)\s*hour[s]?\s*(\d+)\s*min',
            r'"durationText"\s*:\s*"(\d+)\s*hr\s*(\d+)\s*min',
            r'(\d+)\s*hr[s]?\s*(\d+)\s*min(?:["\]},\s])',
        ):
            m = re.search(pat, html, re.IGNORECASE)
            if m and len(m.groups()) == 2:
                h, mn = int(m.group(1)), int(m.group(2))
                duration_text = f"{h} hr {mn} min" if mn else f"{h} hr"
                break

        return miles, duration_text

    def _estimate_route_from_haversine(
        self,
        origin_lat: Any,
        origin_lng: Any,
        dest_lat: Any,
        dest_lng: Any,
        *,
        road_factor: float = ROAD_DISTANCE_FACTOR,
        avg_speed_mph: float | None = None,
    ) -> tuple[float | None, str | None]:
        """Estimate driving distance and time from straight-line (Haversine) distance.

        Distance factor and speed model live in generator/road_estimate.py, shared
        with ai_content so the two cannot drift apart.
        """
        origin = self._parse_lat_lng(origin_lat, origin_lng)
        dest = self._parse_lat_lng(dest_lat, dest_lng)
        if not origin or not dest:
            return None, None
        straight = self._haversine_miles(origin, dest)
        if straight <= 0.5:
            return None, None
        driving = straight * road_factor
        time_str = format_drive_time(drive_minutes(driving, avg_speed_mph=avg_speed_mph))
        return round(driving), time_str

    @classmethod
    def _is_campground_focused_result_for_noncamping_item(
        cls,
        url: str,
        item_name: str,
        *,
        candidate_text: str = "",
        fetched_text: str = "",
    ) -> bool:
        item_l = str(item_name or "").lower()
        if any(token in item_l for token in ("campground", "campsite", "camping", "rv park", "rv campground", "camp")):
            return False

        lower_url = str(url or "").lower()
        if not lower_url:
            return False

        campground_url_markers = (
            "/campground",
            "/campgrounds",
            "/camping",
            "campground=",
            "recreation.gov/camping",
            "reserveamerica",
        )
        # Candidate text is a search result's title and snippet -- short, and a
        # campground marker in it is meaningful. A fetched page body is not:
        # every state park, national forest and BLM recreation page names a
        # campground somewhere, because it has one, so matching the whole body
        # rejects the correct page for the place itself.
        #
        # A campground-focused page announces it at the top; a park page
        # mentions its campground in a footer or a related-links block. Measured
        # on four live pages, the first marker sat 54k, 63k, 185k and 212k
        # characters in, while a genuine campground page carries it in the
        # title. Scanning only the opening separates the two by a wide margin.
        text_blob = (
            f" {str(candidate_text or '').lower()} "
            f"{str(fetched_text or '').lower()[:CAMPGROUND_TEXT_SCAN_CHARS]} "
        )
        campground_text_markers = (
            " campground",
            " campgrounds",
            " campsite",
            " campsites",
            " rv park",
            " book campsite",
            " camping reservation",
            " reserve campsite",
        )

        return any(marker in lower_url for marker in campground_url_markers) or any(
            marker in text_blob for marker in campground_text_markers
        )

    @classmethod
    def _is_category_style_activity(cls, item_name: str) -> bool:
        text = (item_name or "").lower()
        if not text:
            return False
        activity_cues = (
            "fly fishing",
            "fishing",
            "stargazing",
            "birding",
            "kayaking",
            "rafting",
            "paddleboarding",
            "wine tasting",
            "food tour",
            "photography",
        )
        return any(cue in text for cue in activity_cues)

    @classmethod
    def _is_generic_geographic_url_for_category(cls, url: str, item_name: str) -> bool:
        lower = (url or "").lower()
        if not lower:
            return False

        activity_tokens = {
            token
            for token in cls._significant_tokens(item_name)
            if token in {"fishing", "stargazing", "birding", "kayaking", "rafting", "paddleboarding", "photography"}
        }
        if activity_tokens and any(token in lower for token in activity_tokens):
            return False

        if "google.com/maps/search" in lower or "google.com/search" in lower:
            return True
        if "wikipedia.org/wiki/" in lower:
            return True

        path = (urlparse(url).path or "").lower()
        geographic_markers = (
            "river",
            "lake",
            "mountain",
            "canyon",
            "park",
            "pass",
            "valley",
            "forest",
            "reservoir",
            "byway",
        )
        return any(marker in path for marker in geographic_markers)

    @classmethod
    def _is_the_destinations_own_page(cls, url: str, item_name: str, dest_name: str) -> bool:
        """True when the URL is the DESTINATION's page, offered as an ITEM's link.

        GH #59: "Red Canyon" linked to a generic Utah.com Capitol Reef listing.
        Reproduced on v3 2026-09-08 -- that one live page was accepted as the
        link for four different attractions (Red Canyon, Hickman Bridge Trail,
        Cathedral Valley, Sulphur Creek).

        **No text check can catch this, and that is why the rule is about the
        URL.** A destination's overview page legitimately talks about the things
        inside it: fetched live, that Capitol Reef page contains "hickman
        bridge", "cathedral valley" and "sulphur creek". A relevance gate
        reading the prose is therefore right to say the page is about the item,
        and still wrong to accept it -- the page is *about* the destination, and
        mentions the item the way a table of contents mentions a chapter.

        Two conditions, and both are needed:

          1. every distinctive word of the DESTINATION appears in the URL path,
             which is what makes it that destination's own page rather than
             some page that merely sits under it, and
          2. no distinctive word of the ITEM appears anywhere in the URL.

        (2) is what keeps `.../capitol-reef/hickman-bridge` -- a real page for a
        real thing -- from being caught by (1). The item's words are taken after
        subtracting the destination's, so an attraction named for its park is
        judged on what it adds: "Capitol Reef Visitor Center" is asked for
        "visitor" or "center", not for "capitol".

        Returns False when the item adds nothing to the destination's own name.
        An attraction called simply "Capitol Reef" has no word of its own to
        look for, condition (2) would hold vacuously, and the park's page is
        the right answer for it rather than the wrong one.

        **(1) is read from the place name, not the whole destination string.**
        As first written it took every significant token of "Capitol Reef
        National Park, Utah" -- and caught #59 only because `utah` happens to
        sit in `_significant_tokens`' stoplist, a leftover of the project's
        Southwest origins alongside `colorado`, `new` and `mexico`. For
        "Mount Rainier National Park, Washington" the tokens were `mount`,
        `rainier`, `washington`, and a guide to the park does not put its state
        in its path. Measured 2026-09-12 against nine live visitor guides to
        Rainier, Crater Lake and Yosemite, found by search: the rule caught
        none of them. Read from the place name it catches eight; the ninth is
        a bare domain whose path names nothing. 32 destinations across the
        repo's manifests carry a state token that survives the stoplist. The
        suffix after the first comma is there to disambiguate a geocode; URLs
        name the place.
        The stoplist itself is left alone -- every relevance matcher in this
        module reads it.

        The ITEM's words are still reduced by the whole destination string, so
        an item is never judged on its state: "Oregon Vortex" in "Gold Hill,
        Oregon" is asked for `vortex`, not for the `oregon` that half the
        state's URLs contain.

        **On nps.gov the park code stands in for (1).** NPS scopes every park
        page by a four-letter code -- /care/ is Capitol Reef -- so no path
        there ever contains the park's name, and park-wide listings such as
        /care/planyourvisit/trailguide.htm, hiking.htm and thingstodo.htm
        passed both this rule and `_is_obviously_generic_url`. (2) is
        unchanged, so /care/planyourvisit/sulphur-creek.htm still stands for
        Sulphur Creek. A code belonging to a different park is caught too,
        which is also the right answer.
        """
        parsed = urlparse(url or "")
        path = (parsed.path or "").lower()
        if not path:
            return False

        all_dest_tokens = set(cls._significant_tokens(dest_name))
        place_name = str(dest_name or "").split(",", 1)[0]
        place_tokens = set(cls._significant_tokens(place_name)) or all_dest_tokens

        in_destination_scope = bool(place_tokens) and all(token in path for token in place_tokens)
        if not in_destination_scope:
            in_destination_scope = cls._is_nps_park_scoped_path(parsed.netloc, path)
        if not in_destination_scope:
            return False

        item_tokens = set(cls._significant_tokens(item_name)) - all_dest_tokens - place_tokens
        if not item_tokens:
            return False

        if cls._host_carries_the_items_name(parsed.netloc, item_name):
            return False

        lower = (url or "").lower()
        return not any(token in lower for token in item_tokens)

    @staticmethod
    def _host_carries_the_items_name(netloc: str, item_name: str) -> bool:
        """True when the domain is the item's own name, run together.

        `_significant_tokens` drops words under four letters, which is right for
        matching prose and wrong for a brand. "Dru Bru Brewery" at
        drubru.com/snoqualmie-pass/ is the brewery's own site, but its only
        surviving token is `brewery`, absent from the URL -- so the destination
        rule, read from the place name, took it for a page about Snoqualmie
        Pass. Measured on the published Pacific Crest Trail guide: the one
        false positive among ten changed verdicts.

        Businesses squash their names into domains, so this looks for any run
        of two or more adjacent words, joined, in the host. Runs shorter than
        five letters are ignored, as too easy to find by accident.
        """
        host = (netloc or "").lower().split(":", 1)[0]
        if not host:
            return False
        words = re.findall(r"[a-z0-9]+", (item_name or "").lower())
        for size in range(len(words), 1, -1):
            for start in range(0, len(words) - size + 1):
                joined = "".join(words[start:start + size])
                if len(joined) >= 5 and joined in host:
                    return True
        return False

    #: Four-letter top-level nps.gov sections that are not park codes. Park
    #: codes are four letters; so are these, and a page under /news/ is not
    #: scoped to any one park.
    _NPS_NON_PARK_SECTIONS = frozenset({"news", "orgs", "apps", "data", "subj"})

    @classmethod
    def _is_nps_park_scoped_path(cls, netloc: str, path: str) -> bool:
        """True for an nps.gov path under a park code: /care/..., /zion/...

        Not /thingstodo/..., /places/... or /articles/... -- those are
        service-wide sections holding pages about single things, and are
        exactly where entity-specific links live.
        """
        host = (netloc or "").lower().split(":", 1)[0]
        if host != "nps.gov" and not host.endswith(".nps.gov"):
            return False
        segments = [s for s in (path or "").split("/") if s]
        if len(segments) < 2:
            # /care or /care/ alone is the park home, which
            # _is_obviously_generic_url already handles as /index.htm.
            return False
        first = segments[0]
        return bool(re.fullmatch(r"[a-z]{4}", first)) and first not in cls._NPS_NON_PARK_SECTIONS

    @staticmethod
    def _is_category_offer_listing_url(url: str) -> bool:
        lower = (url or "").lower()
        if not lower:
            return False
        path = (urlparse(url).path or "").lower()
        markers = (
            "/things-to-do",
            "/things2do",
            "/activities",
            "/activity",
            "/listing",
            "/listings",
            "/offers",
            "/offer",
            "/experiences",
            "/experience",
            "/plan-your-visit",
            "/planyourvisit",
            "/explore",
        )
        return any(marker in path for marker in markers)

    @staticmethod
    def _is_ambiguous_geographic_feature_name(item_name: str) -> bool:
        text = str(item_name or "").strip().lower()
        if not text:
            return False

        # Preserve expected map fallback behavior for explicit park entities.
        if " park" in text or text.endswith("park"):
            return False
        if "reserve" in text:
            return False

        geographic_markers = (
            "river",
            "canyon",
            "desert",
            "reserve",
            "mountain",
            "valley",
            "forest",
            "plateau",
            "mesa",
            "basin",
            "range",
        )
        specific_landmark_cues = (
            "trail",
            "road",
            "scenic drive",
            "overlook",
            "viewpoint",
            "visitor center",
            "museum",
        )

        if any(cue in text for cue in specific_landmark_cues):
            return False

        token_count = len(re.findall(r"[a-z0-9]+", text))
        has_geo_marker = any(marker in text for marker in geographic_markers)
        return has_geo_marker and token_count <= 4

    @staticmethod
    def _is_trail_like_attraction(name: str, attr_type: str, description: str = "") -> bool:
        type_norm = (attr_type or "").strip().lower()
        name_l = (name or "").lower()
        haystack = f"{name} {description}".lower()
        normalized = re.sub(r"[^a-z0-9\s]", "", haystack)

        # Explicit negations of hiking/walking access ("no hiking required",
        # "without a hike", "doesn't require any walking") describe a place
        # that does NOT require trail activity -- the opposite of a trail
        # signal -- so the negated word must not be counted as one below.
        # Real Bryce Canyon "Paria View" (a plain viewpoint, correctly
        # harvested with its own NPS page nps.gov/brca/planyourvisit/
        # paria.htm) carries the practical note "Accessible with no hiking
        # required; parking is limited." -- "hiking" as a bare substring
        # would otherwise misclassify it as trail-like, sending it down the
        # AllTrails-only path where a same-named-but-different "Paria View
        # Trail" AllTrails candidate could be picked up instead of its own
        # correct attraction link (dipstick59: this is exactly how the
        # published "paria-view-trail" AllTrails URL, a real 404, got
        # selected in place of the viewpoint's real NPS page).
        normalized = re.sub(
            r"\bno\s+(hiking|hikes?|walking|walks?|trails?|trekking|treks?)\b"
            r"(\s+(required|needed|necessary|involved))?",
            " ",
            normalized,
        )
        normalized = re.sub(
            r"\bwithout\s+(a\s+|any\s+)?(hiking|hikes?|walking|walks?|trails?|trekking|treks?)\b",
            " ",
            normalized,
        )
        normalized = re.sub(
            r"\b(doesnt|does not|dont|do not)\s+require\s+(a\s+|any\s+)?(hiking|hikes?|walking|walks?|trails?)\b",
            " ",
            normalized,
        )

        non_trail_place_cues = (
            "museum",
            "history museum",
            "historic museum",
            "discovery site",
            "interpretive center",
            "visitor center",
            "cultural center",
        )
        if any(cue in name_l for cue in non_trail_place_cues):
            if not re.search(r"\b(trail|hike|loop|walk|trek|path|summit)\b", name_l):
                return False

        # Place-level attractions (parks/monuments/districts) often mention trails in
        # their description, but should not be forced into AllTrails-first routing.
        place_level_name = any(
            cue in name_l
            for cue in (
                "state park",
                "national park",
                " park",
                "desert reserve",
                "national monument",
                "historic district",
                "downtown",
                "visitor center",
                "discovery site",
                "petroglyph",
                "overlook",
                "viewpoint",
                "homestead",
            )
        )
        name_has_trail_cue = bool(re.search(r"\b(trail|hike|hiking|loop|walk|trek|path|summit)\b", name_l))
        if place_level_name and not name_has_trail_cue:
            return False

        trail_types = {
            "hike",
            "hiking",
            "trail",
            "trek",
            "walk",
        }
        if type_norm in trail_types:
            return True

        trail_substrings = (
            "this trail",
            "hiking trail",
            "trailhead",
            "switchback",
            "backcountry",
            "slot canyon",
        )
        if any(marker in normalized for marker in trail_substrings):
            return True

        # Catch common trail phrasing even when type is labeled as generic attraction.
        # "walk" alone is treated separately below: unlike "trail"/"hike"/"trek"/
        # "path"/"summit"/"loop", it's also common in short access-instruction
        # phrasing for viewpoints that aren't trails at all (e.g. "Accessible via
        # a short walk from the parking lot, this viewpoint provides..." --
        # Bryce Canyon's real "Inspiration Point"). A non-"walk" trail keyword
        # anywhere in the name+description is still a reliable signal on its own.
        if re.search(r"\b(trail|hike|hiking|loop|trek|path|summit)\b", normalized):
            return True

        if not re.search(r"\bwalk\b", normalized):
            return False

        # "walk" appearing in the item's own name (e.g. "Riverside Walk", "The
        # Zion Narrows Riverside Walk") names the route itself and is a strong
        # signal, unlike a description mentioning a walk only in passing.
        if re.search(r"\bwalk\b", name_l):
            return True

        # From here, "walk" only appears in the description. That's still a
        # useful signal for genuine short trails/walks, EXCEPT when it reads as
        # a mere short access note to a non-trail viewpoint/pullout -- i.e. a
        # short/brief/easy/quick "walk" or "stroll" mentioned together with a
        # parking/pullout/overlook/viewpoint cue, and nothing else in the text
        # (mileage, "trailhead", explicit difficulty language) corroborates an
        # actual trail. That combination is exactly the false-positive pattern
        # seen in real Bryce Canyon viewpoint descriptions ("Inspiration
        # Point": "Accessible via a short walk from the parking lot, this
        # viewpoint provides an elevated look...").
        has_trail_corroboration = bool(
            re.search(r"\d+(\.\d+)?\s*[- ]?\s*miles?\b", normalized)
            or re.search(r"\b(round[- ]trip|elevation|switchback|difficulty|strenuous|moderate|steep)\b", normalized)
        )
        if has_trail_corroboration:
            return True

        short_walk_access_note = bool(
            re.search(r"\b(short|brief|quick|easy)\s+(walk|stroll)\b", normalized)
            and re.search(r"\b(parking (lot|area)|pullout|trailhead lot|overlook|viewpoint)\b", normalized)
        )
        if short_walk_access_note:
            return False

        return True

    @staticmethod
    def _attraction_trail_context(attr: dict[str, Any]) -> str:
        return " ".join(
            [
                str(attr.get("description", "") or ""),
                str(attr.get("practical_note", "") or ""),
                str(attr.get("difficulty", "") or ""),
                str(attr.get("duration", "") or ""),
            ]
        )

    @staticmethod
    def _canonical_token(token: str) -> str:
        t = (token or "").lower()
        if t.endswith("ies") and len(t) > 4:
            return t[:-3] + "y"
        if t.endswith("s") and len(t) > 4 and not t.endswith("ss"):
            return t[:-1]
        return t

    @staticmethod
    def _significant_tokens(text: str) -> list[str]:
        tokens = re.findall(r"[a-z0-9]+", (text or "").lower())
        stop = {
            "the", "and", "for", "with", "from", "near", "park", "national", "state", "trail",
            "road", "drive", "point", "restaurant", "cafe", "grill", "utah", "colorado", "new", "mexico",
            # "scenic"/"byway" are generic route-type descriptors, not identifying
            # words -- the same reason "road"/"drive"/"trail"/"point" are already
            # excluded above. Without this, an item like "Enchanted Circle Scenic
            # Byway" only needs 2 of its 4 tokens to overlap a candidate (see
            # _required_general_token_matches), and "scenic"+"byway" alone are
            # generic enough to appear in boilerplate copy for almost any other
            # place near a scenic road, wrongly satisfying that bar without either
            # of the item's real identifying words ("enchanted"/"circle") ever
            # matching. Real example (dipstick62): the en-route-stop seed
            # "Enchanted Circle Scenic Byway" (a real byway near Taos, NM) matched
            # and linked to "Ouray Hot Springs Pool" -- an unrelated Ouray, CO
            # attraction whose harvested candidate text mentions a nearby "scenic"
            # byway (San Juan Skyway) in passing, sharing zero real identity with
            # the seeded item.
            "scenic", "byway",
        }
        out: list[str] = []
        seen: set[str] = set()
        for t in tokens:
            if len(t) < 4 or t in stop:
                continue
            canonical = URLDiscoverer._canonical_token(t)
            if len(canonical) < 4 or canonical in stop or canonical in seen:
                continue
            seen.add(canonical)
            out.append(canonical)
        return out

    @staticmethod
    def _restaurant_significant_tokens(text: str) -> list[str]:
        """Token extractor for restaurant names.

        Lowers the minimum length to 3 and keeps 'grill' and 'cafe' since these
        often appear verbatim in restaurant domain slugs (e.g. kipsgrill.com).
        """
        tokens = re.findall(r"[a-z0-9]+", (text or "").lower())
        stop = {
            "the", "and", "for", "with", "from", "near", "park", "national", "state", "trail",
            "road", "drive", "point", "restaurant", "utah", "colorado", "new", "mexico",
        }
        out: list[str] = []
        seen: set[str] = set()
        for t in tokens:
            if len(t) < 3 or t in stop:
                continue
            canonical = URLDiscoverer._canonical_token(t)
            if len(canonical) < 3 or canonical in stop or canonical in seen:
                continue
            seen.add(canonical)
            out.append(canonical)
        return out

    @staticmethod
    def _destination_country_tlds(dest_name: str) -> set[str]:
        lowered = (dest_name or "").lower()
        out: set[str] = set()
        for key, tlds in COUNTRY_TLD_HINTS.items():
            if key in lowered:
                out.update(tlds)
        return out

    @classmethod
    def _looks_location_qualified(cls, text: str) -> bool:
        lowered = (text or "").lower().strip()
        if not lowered:
            return False
        if "," in lowered:
            return True
        strong_location_terms = (
            "national park",
            "state park",
            "junction",
            "utah",
            "colorado",
            "arizona",
            "new mexico",
            "nevada",
            "california",
        )
        if any(term in lowered for term in strong_location_terms):
            return True
        if re.search(r"\b(?:st|saint)\.?\s+[a-z]", lowered):
            return True
        return False

    @classmethod
    def _en_route_maps_fallback_query_text(cls, item_name: str, origin_name: str, dest_name: str) -> str:
        base = cls._maps_fallback_query_text(item_name, dest_name)
        origin = str(origin_name or "").strip()
        dest = str(dest_name or "").strip()
        lowered = base.lower()
        if dest and dest.lower() not in lowered:
            base = f"{base} near {dest}".strip()
        if origin and origin.lower() not in lowered and origin.lower() not in base.lower():
            base = f"{base} route from {origin}".strip()
        return base

    @classmethod
    def _name_mentions_destination(cls, name: str, dest_name: str) -> bool:
        name_tokens = set(cls._significant_tokens(name))
        dest_tokens = set(cls._significant_tokens(dest_name))
        if not name_tokens or not dest_tokens:
            return False
        return bool(name_tokens & dest_tokens)

    @classmethod
    def requalify_maps_query_url(cls, url: str, item_name: str, dest_name: str) -> str:
        """Rebuild a Maps text-query link so its query names the destination.

        Every builder in this file qualifies through _maps_fallback_query_text,
        yet the sw run published

            https://www.google.com/maps/search/?api=1&query=Rico%20Historic%20District

        for a stop on the Telluride -> Pagosa Springs leg, and carried the same
        bare text as a waypoint in that leg's route URL. A bare place name is
        ambiguous to Google: "Rico Historic District" is not unique, and an
        unqualified waypoint is how a Colorado route acquires a pin in
        Washington.

        Applied where the value is final rather than at each builder. One
        call site passes an empty dest_name and reading did not find it; this
        makes the output correct regardless of which, and is a no-op for a
        query that already names the destination.

        Coordinate queries are left exactly as they are -- they are the
        precise form, and rewriting one into a name search is the downgrade
        the 2026-08-22 run was caught doing.
        """
        raw = str(url or "").strip()
        if not raw or "google.com/maps/search/" not in raw:
            return raw
        if cls._is_coordinate_maps_query_url(raw):
            return raw
        match = re.search(r"([?&])query=([^&]*)", raw)
        if not match:
            return raw
        current = unquote(match.group(2)).replace("+", " ").strip()
        if not current:
            return raw
        wanted = cls._maps_fallback_query_text(item_name or current, dest_name)
        if not wanted or wanted.strip().lower() == current.lower():
            return raw
        return raw[:match.start(2)] + quote(wanted) + raw[match.end(2):]

    @classmethod
    def _maps_fallback_query_text(cls, item_name: str, dest_name: str) -> str:
        item = str(item_name or "").strip()
        dest = str(dest_name or "").strip()
        if not item:
            return dest
        if cls._name_mentions_destination(item, dest):
            return item
        if cls._looks_location_qualified(item):
            return item
        return f"{item} {dest}".strip()
