# Road Trip Itinerary Generator — Requirements Document
**Version 0.17 · July 23, 2026**

### Changelog from v0.17
| # | Section | Change |
|---|---|---|
| 1 | §4, §12 | Added local iterative image cache index (`.cache/images/cache_index.json`) with destination-keyed reuse and TTL control to reduce repeated provider discovery calls |
| 2 | §5, §11 | Added CLI switch `--refresh-image-cache` to bypass local image cache on demand |

### Changelog from v0.16
| # | Section | Change |
|---|---|---|
| 1 | §13 | Consolidated PWA flow to one canonical implementation: static `manifest.webmanifest`, single `sw.js` registration path, and one install-prompt UX path |
| 2 | §12, §13 | Generator now writes PWA companion assets beside `index.html` in the active output environment folder |

### Changelog from v0.15
| # | Section | Change |
|---|---|---|
| 1 | §4, §7 | Daily schedule rendering updated to hanging-indent format so wrapped lines align under time-of-day content while icon+label stay on one line |
| 2 | §4.3 | Local-tip fallback content now includes a "More info" link using query-based lookup when no direct source URL is available |
| 3 | §6, §7 | Single supplemental image layout now centers/fills gallery space rather than left-column pinning |
| 4 | §5 | External link rendering hardened with URL normalization/escaping to reduce broken links in attractions, restaurants, events, and en-route cards |

### Changelog from v0.14
| # | Section | Change |
|---|---|---|
| 1 | §4, §7 | Schedule time-of-day labels now require non-wrapping icon+label alignment, with content text wrapping under the schedule content column |
| 2 | §4, §5 | CAN'T-MISS ENROUTE stop links hardened with escaped href rendering and actionable fallback links |
| 3 | §4.3 | Cultural events now require a resolvable "More info" link per identified event (source URL or generated search fallback) |

### Changelog from v0.13
| # | Section | Change |
|---|---|---|
| 1 | §4 | Schedule normalization now injects arrival/departure travel context for first/last itinerary days when multi-day stays are present |
| 2 | §5 | Attraction and en-route links now fall back to Google Maps query links when strict URL discovery yields no verified page |
| 3 | §7 | Scenic-drive popup now includes a "More Info" external link and suppresses attribution-style text in popup body |
| 4 | §4, §6 | Attraction deduplication and image-localization relevance scoring added to reduce redundant entries and off-location photos |
| 5 | §7 | Cultural events card styling normalized to match core card visual language |

### Changelog from v0.12
| # | Section | Change |
|---|---|---|
| 1 | §5 | Hike URL reliability hardened: specific AllTrails trail URLs are accepted without brittle liveness checks that often fail on bot-protected pages |
| 2 | §4, §7 | En-route stop schema/rendering now includes detour metadata (`detour_distance_miles`, `detour_time_minutes`) for CAN'T-MISS ENROUTE cards |
| 3 | §7, §8 | Footer credit now renders generator name + version + generation timestamp with repository link; static "Made by Copilot" removed |
| 4 | §4 | Added budget-aware dinner price filtering rules in post-normalization |
| 5 | §13-§18 | Added requirements coverage for PWA support, print formatting, per-destination maps, dinner price logic, planning-link formatting, and month-specific weather grounding |

### Changelog from v0.11
| # | Section | Change |
|---|---|---|
| 1 | §4 | Added deterministic weather grounding: `expected_environment.temperature_high_f` / `temperature_low_f` are normalized from historical monthly climate normals by destination coordinates and travel month |
| 2 | §4 | Environment summary temperature claims are rewritten to reflect grounded normals, reducing hallucinated weather ranges |

### Changelog from v0.8
| # | Section | Change |
|---|---|---|
| 1 | §7 | `v2.5_template.html` converted from hardcoded reference document to true generator template: trip title, nav tabs, Google Maps URL, Leaflet map markers, and all destination sections now use injection placeholders |
| 2 | §7 | Assembler updated to produce output matching template's CSS/JS conventions: section IDs use `section-{id}` format, CSS class `dest-section`, drive buttons use `class="drive-link"` + `data-drive-title`, `DRIVE_DESCRIPTIONS` keyed by raw title string |
| 3 | §7 | Validator updated to check `data-drive-title` attribute (not `data-drive-key`) and `id="section-{id}"` format (not bare `id="{id}"`) |
| 4 | §11 | Added `XAI_MODEL` env var to select Grok model; `XAI_API_KEY` already present since v0.8 |

### Changelog from v0.10
| # | Section | Change |
|---|---|---|
| 1 | §3 | Added optional `trip.departure` and `trip.return` manifest fields; geocoded and used in route links/map context |
| 2 | §5, §9 | Added `--noschedule` CLI flag to suppress schedule rendering |
| 3 | §5 | URL policy tightened: hike links resolve via AllTrails; non-hike attractions may use NPS/official sources |
| 4 | §5 | URL selection requires relevance checks (not only liveness), reducing generic search/landing-page links |
| 5 | §7 | Full Route Map now uses Google Maps Directions API parameters (origin/destination/waypoints by place name) rather than bare coordinate chains |
| 6 | §8 | Debug block rendering is opt-in (`config.render.show_debug_block`) and off by default |
| 7 | §11 | LLM cost tracking now includes Grok/xAI usage from URL discovery and cultural-event search calls |

### Changelog from v0.7
| # | Section | Change |
|---|---|---|
| 1 | §2, §5, §11 | Search client migrated from Google Programmable Search Engine (v1.4, rate-limited) to xAI Grok semantic search (v1.5); env var changed to `XAI_API_KEY` (single key, simpler setup) |

### Changelog from v0.6 *(superseded)*
Migrated from Bing Web Search (v1.3) → Google Programmable Search (v1.4). Fully superseded by v0.8 Grok migration.

### Changelog from v0.5 *(superseded)*
Migrated from Brave Search (v1.2) → Bing Web Search (v1.3). Fully superseded by v0.8 Grok migration. Added parallel `ThreadPoolExecutor` execution model.

---

## 1. Purpose & Scope

A Python command-line program that accepts a minimal user-defined trip manifest and produces a single self-contained `index.html` file that is visually, structurally, and functionally identical to the Southwest Road Trip Itinerary v2.5 — but with AI-generated content tailored to any set of destinations worldwide.

The output file must be deployable to GitHub Pages or any static host with zero additional dependencies.

---

## 2. System Architecture

```
┌─────────────────────────────────────────────────────────┐
│  trip_manifest.yaml          (user-authored, minimal)   │
└────────────────────┬────────────────────────────────────┘
                     │
                     ▼
┌─────────────────────────────────────────────────────────┐
│  STAGE 1: Input Validation & Auto-Enrichment            │
│  • Parse and validate manifest schema                   │
│  • Geocode each destination → lat/lng (Nominatim API)   │
│  • Detect NPS park code from destination name           │
│  • Construct weather.gov URL from lat/lng               │
│  • Verify all user-provided planning link URLs          │
│  • Auto-generate Google Maps overview URL               │
└────────────────────┬────────────────────────────────────┘
                     │
                     ▼
┌─────────────────────────────────────────────────────────┐
│  STAGE 2: AI Content Generation (Azure OpenAI)          │
│  • Per-destination content: environment, attractions,   │
│    en-route stops, schedule, restaurants (NO URLS)      │
│  • Post-normalization grounds monthly temperatures       │
│    from climate normals and rewrites weather narrative   │
│  • Scenic drives + viewpoints (fully AI-discovered)     │
│  • Cultural events via Grok semantic search + AI synthesis│
└────────────────────┬────────────────────────────────────┘
                     │
                     ▼
┌─────────────────────────────────────────────────────────┐
│  STAGE 3: URL Discovery (xAI Grok Semantic Search)      │
│  • Per-item URL discovery for every named entity        │
│  • NPS domain filter for park attractions               │
│  • Two-pass restaurant strategy:                        │
│    Pass 1: Google Maps (top-rated, hours)               │
│    Pass 2: TripAdvisor (diversity, local favorites)     │
│  • 4-variant fallback query sequence per item           │
│  • HTTP HEAD verification of every discovered URL       │
└────────────────────┬────────────────────────────────────┘
                     │
                     ▼
┌─────────────────────────────────────────────────────────┐
│  STAGE 4: Image Fetching                                │
│  • NPS API for national parks (park code required)      │
│  • Wikimedia Commons MediaSearch for all destinations   │
│  • THUMB_WIDTH = 960px always                           │
│  • 4-attempt automatic fallback on verification fail    │
│  • Hard fail if < min_per_destination verified images   │
└────────────────────┬────────────────────────────────────┘
                     │
                     ▼
┌─────────────────────────────────────────────────────────┐
│  STAGE 5: HTML Assembly                                 │
│  • SHA-256 checksum verification of frozen template     │
│  • Python string assembly (no Jinja2)                   │
│  • var DRIVE_DESCRIPTIONS JS object (not const)         │
│  • Google Maps overview URL auto-injected               │
│  • Attribution <details> block appended at page bottom  │
└────────────────────┬────────────────────────────────────┘
                     │
                     ▼
┌─────────────────────────────────────────────────────────┐
│  STAGE 6: Validation & Reporting                        │
│  • Div balance per destination section                  │
│  • Script tag isolation check                           │
│  • var DRIVE_DESCRIPTIONS presence (not const)          │
│  • Drive modal key/button alignment                     │
│  • Image count >= min_per_destination per section       │
│  • JSON validation report output                        │
└─────────────────────────────────────────────────────────┘
```

---

## 3. Trip Manifest Schema (v0.5)

The manifest is intentionally minimal. All geocoding, NPS detection, URL discovery, and content generation happen automatically.

### 3.1 Trip-Level Fields

| Field | Required | Description |
|---|---|---|
| `title` | ✅ | Trip title (e.g., "Southwest Road Trip") |
| `subtitle` | ✅ | Trip subtitle (e.g., "October 2026 — Utah & Colorado") |
| `theme_color` | ✅ | Hex color for nav, headers, map markers (e.g., `#C0623E`) |
| `llm` | ❌ optional | Override model routing for this trip: `{provider, model, temperature, max_tokens}` |
| `budget` | ❌ optional | Budget guidance (string/number/object) passed into AI prompts |
| `departure` | ❌ optional | Route origin location name for first leg and full-route map |
| `return` | ❌ optional | Route final endpoint location name for full-route map |

`trip.llm.provider` supports: `openai`, `anthropic`, `deepseek`, `gemini`.

### 3.2 Auto-Resolved Fields (NOT in manifest)

| Field | Resolution Method |
|---|---|
| `lat` / `lng` | Nominatim geocoding from `name` |
| `nps_park_code` | Keyword detection + NPS API text search |
| `weather_url` | Constructed from lat/lng |
| `google_maps_link` | Auto-generated from origin/destination/waypoints (names); includes `departure`/`return` when provided |

### 3.3 Destination Fields

| Field | Required | Description |
|---|---|---|
| `id` | ✅ | Unique slug (e.g., `zion`, `moab`) |
| `name` | ✅ | Full destination name for geocoding and AI prompts |
| `dates` | ✅ | Human-readable date range (e.g., `"October 7–9, 2026"`) |
| `planning_links[]` | ✅ | Array of `{label, url}` — Notion, TripIt, reservation links |
| `seeds[]` | ❌ optional | Attraction/hike/experience **name hints only** — things the user specifically intends to include. No URLs. No scenic drive titles (AI discovers those). |

### 3.4 Seeds Rules

Seeds are lightweight hints that anchor AI content generation to specific user intentions. They are:
- ✅ Attraction names: `"Angels Landing"`, `"The Narrows"`
- ✅ Hike names: `"Navajo Loop Trail"`, `"Hickman Bridge"`
- ✅ Experience anchors: `"Dark Sky Stargazing"`, `"Jeep rental"`
- ❌ NOT URLs — any seed containing `://` is rejected with a validation error
- ❌ NOT scenic drive titles — AI discovers those independently
- ❌ NOT restaurant names — AI discovers those via TripAdvisor/Google Maps sourcing

---

## 4. AI Content Generation

### 4.1 Per-Destination Content Schema

```json
{
  "expected_environment": {
    "summary": "string — sensory lead + operational note; temp claims are grounded post-generation",
    "temperature_high_f": "integer — grounded monthly daytime normal in °F",
    "temperature_low_f": "integer — grounded monthly overnight normal in °F",
    "what_to_pack": ["string", "..."]
  },
  "getting_here": {
    "from_previous": "string — driving directions from previous destination",
    "en_route_stops": [
      {
        "name": "string",
        "highway_reference": "string — highway and exit/milepost",
        "description": "string — what makes this worth stopping for",
        "time_required": "string — e.g., '30 minutes'",
        "detour_distance_miles": "number — extra miles off the direct route (0 if on-route)",
        "detour_time_minutes": "number — extra drive minutes for detour (0 if on-route)"
      }
    ]
  },
  "top_attractions": [
    {
      "name": "string",
      "description": "string",
      "difficulty": "Easy | Moderate | Strenuous",
      "duration": "string — e.g., '4–5 hours'",
      "must_see": true,
      "practical_note": "string — permit info, seasonal closures, gear requirements"
    }
  ],
  "possible_daily_schedule": ["string array — timed itinerary items"],
  "dinner_recommendations": [
    {
      "name": "string",
      "cuisine": "string — specific cuisine type",
      "price": "$ | $$ | $$$ | $$$$",
      "description": "string"
    }
  ]
}
```

**Restaurant requirements:** 5–6 per destination. Must include 3+ distinct cuisine types and 2+ price tiers. Coverage must include both top-rated and local/casual options.

**Dinner price filtering logic:**
- If trip budget indicates budget/economy/value, recommendations should be centered on `$`/`$$` with at most one splurge (`$$$`/`$$$$`).
- If trip budget indicates premium/luxury/upscale, recommendations should be centered on `$$$`/`$$$$` with at most one casual option (`$`/`$$`).
- If no clear budget signal exists, include a mixed tier spread.

**Schedule realism rules:**
- Multi-day destination schedules must account for arrival-driving impact on Day 1.
- Final day should account for onward departure preparation to the next destination.
- Day-level sequencing should remain feasible given same-day drive and activity load.

### 4.2 Scenic Drives & Viewpoints Schema

```json
[
  {
    "title": "string — AI-discovered, not seeded",
    "category": "scenic_drive | viewpoint | aerial | day_trip | historic",
    "distance_or_duration": "string",
    "best_time": "string",
    "description": "string",
    "vehicle_requirement": "any | high_clearance | 4wd"
  }
]
```

2–4 entries per destination. Titles are fully AI-discovered — never seeded by the user.

### 4.3 Cultural Events Schema (has_events decision tree)

**Format A — Events found:**
```json
{
  "has_events": true,
  "intro": "string",
  "events": [
    {
      "name": "string",
      "date": "string",
      "venue": "string — physical address",
      "admission": "string",
      "ambient_scene": "string — what it feels like to be there",
      "url": "string — source event URL when available"
    }
  ]
}
```

Each identified event should render a "More info" link. If a source event URL is unavailable after verification, renderer may fall back to a query-based search link using event name + venue + destination.

**Format B — Honest fallback (no invented events):**
```json
{
  "has_events": false,
  "honest_assessment": "string — what the park/town IS good for in this season",
  "local_tip": "string — one practical insight"
}
```

If `local_tip` references a specific weekday (for example, Friday or Saturday), that weekday must be within the destination itinerary date window; otherwise `local_tip` is omitted.

The AI must NEVER invent events. Remote national parks almost always return Format B.

---

## 5. URL Discovery

AI content generation and URL discovery are strictly separate pipeline stages. **AI never generates URLs.**

After AI content is generated, the URL Discoverer uses xAI Grok semantic search for every named entity:

1. **Hike attractions:** Resolve via AllTrails domain filter (primary policy)
2. **Non-hike attractions in NPS parks:** Prefer `site:nps.gov` domain filter
3. **Non-hike attractions:** Fall back to official/specific pages from broader search
3. **Restaurants:** Two-pass — Google Maps domain filter, then TripAdvisor
4. **All items:** 4-variant fallback query sequence (most specific → broadest)
5. **Final fallback:** Empty string stored (no fabricated URLs)

Every discovered URL is HTTP-verified before storage, and strict candidates must also pass relevance checks against item/destination tokens. Live-but-generic search pages are rejected.

Exception for hike links:
- For specific AllTrails trail pages (`alltrails.com/trail/...`), URL acceptance may bypass strict liveness checks when provider-side bot protections reject automated HEAD/GET requests.

Fallback policy for unresolved links:
- If strict discovery does not produce a verified attraction/en-route/scenic URL, render a Google Maps query link so cards still resolve to actionable context.

---

## 6. Image Pipeline

| Priority | Source | Condition |
|---|---|---|
| 1st | NPS API | `nps_park_code` present |
| 2nd | Wikimedia Commons MediaSearch | Always attempted |
| Fallback | Broader Wikimedia queries (4 attempts) | If < min_per_destination verified |
| Hard fail | RuntimeError raised | If still < min_per_destination |

- `THUMB_WIDTH = 960` always
- Images saved to `output/images/` with MD5-hashed filenames
- Image metadata (source, license, author) stored for attribution footer

---

## 7. Template Integrity

The v2.5 HTML template is committed to the repository as `templates/v2.5_template.html`. A SHA-256 checksum is stored in `templates/checksums.txt`.

On every run, the generator verifies the template checksum before processing. A mismatch causes an immediate hard failure with a clear error message.

The template is never fetched at runtime.

### 7.1 Template Injection Placeholders

The template is a true generator template — all trip-specific content is injected at runtime. Hardcoded content from the reference document has been replaced with the following placeholders:

| Placeholder | Replaced With |
|---|---|
| `<!--TRIP_TITLE-->` | `trip.title` from manifest |
| `<!--NAV_TABS-->` | Generated `<button class="tab-btn" data-tab="section-{id}">` elements + Google Maps link |
| `<!--DESTINATION_SECTIONS-->` | Full per-destination section HTML built by `HTMLAssembler` |
| `'<!--MAP_MARKERS_JSON-->'` | JSON array of `{c:[lat,lng], mo, dy, name}` objects for Leaflet map |
| `var DRIVE_DESCRIPTIONS = {};` | Populated with AI-generated drive descriptions keyed by raw title string |

### 7.2 Template CSS/JS Conventions

The assembler must produce output conforming to the template's JavaScript expectations:

| Element | Required Format |
|---|---|
| Destination sections | `<section id="section-{id}" class="dest-section">` |
| Nav tab buttons | `<button class="tab-btn" data-tab="section-{id}">` |
| Scenic drive buttons | `<button class="drive-link" data-drive-title="{title}">` |
| `DRIVE_DESCRIPTIONS` keys | Raw title string (e.g. `"Zion Canyon Scenic Drive"`) |

The template JavaScript queries `.dest-section` for scroll-spy, `.tab-btn[data-tab]` for navigation, and `.drive-link[data-drive-title]` for scenic drive modals.

---

## 8. Attribution Footer

A collapsible `<details>` block is appended before `</body>` containing:

1. Generator version + generation timestamp (UTC)
2. Image attribution table (destination, title, source, credit, license per image)
3. Cultural events disclaimer: *"Event information was auto-discovered and may not be current."*
4. Generator credit line with repository link, generator version, and generation timestamp (UTC)

---

## 9. CLI Interface

```
python -m generator.main [OPTIONS]

Options:
  --manifest PATH          Trip manifest YAML (required)
  --output PATH            Output directory [default: output/]
  --config PATH            Config YAML [default: config.yaml]
  --dry-run                Parse & validate manifest only; no AI calls
  --skip-images            Skip image fetching
  --skip-events            Skip cultural events discovery
  --skip-url-discovery     Skip URL discovery (AI content only)
    --noschedule             Suppress schedule rendering in output HTML
  --destination TEXT       Limit to specific destination id (repeatable)
  --verbose                Enable debug logging
```

---

## 10. Configuration (config.yaml)

Key configurable values:

| Key | Default | Description |
|---|---|---|
| `ai.temperature` | `0.7` | LLM temperature for content generation |
| `ai.max_tokens` | `3000` | Max tokens per AI response |
| `images.min_per_destination` | `2` | Hard fail threshold |
| `images.max_per_destination` | `4` | Maximum images fetched per destination |
| `url_discovery.max_fallback_attempts` | `4` | Fallback query attempts per item |
| `validation.min_images_per_section` | `2` | HTML validator image count threshold |

---

## 11. Environment Variables

| Variable | Required | Description |
|---|---|---|
| `AZURE_OPENAI_ENDPOINT` | ✅ | Azure OpenAI resource URL |
| `AZURE_OPENAI_API_KEY` | ✅ | Azure OpenAI API key |
| `AZURE_OPENAI_DEPLOYMENT` | ✅ | Model deployment name |
| `AZURE_OPENAI_API_VERSION` | ❌ | API version (default: `2024-02-01`) |
| `XAI_API_KEY` | ✅ | xAI Grok API key |
| `XAI_MODEL` | ❌ | Grok model name (default: `grok-2-latest`; set to `grok-4.5` or later as available) |
| `NPS_API_KEY` | ❌ | NPS API key (default: `DEMO_KEY`, rate-limited) |

Cost accounting note:
- LLM usage/cost summary includes OpenAI (content/drives), plus xAI Grok usage from URL discovery and cultural-event search requests.
- URL liveness/relevance HTTP checks do not consume LLM tokens and are not part of LLM-cost.

---

## 12. Output Structure

```
output/
├── index.html              ← Self-contained itinerary (deploy to GitHub Pages)
├── images/
│   ├── {md5hash}.jpg       ← Downloaded destination images
│   └── ...
└── validation_report.json  ← Post-assembly validation results
```

---

## 13. PWA Support Requirements

- Output HTML must include installable web app metadata (manifest + app icons).
- A service worker must be registered best-effort for offline shell behavior and static asset caching.
- Install prompt UX should be exposed when browser eligibility allows.

---

## 14. Print-Friendly Requirements

- Print stylesheet must hide non-essential interactive UI (map nav chrome, install buttons, galleries where needed).
- Printed output must preserve section readability, headings, and link traceability (show URL targets in print).
- Page-break behavior should keep each destination section coherent and avoid orphaned headers.

---

## 15. Per-Destination Map Requirements

- Each destination section should support an embedded local map panel showing the destination coordinate context.
- Embedded map content must not break static-host deployment and should degrade gracefully when map scripts fail.

Related popup requirement:
- Scenic-drive and day-trip modal content should include one direct "More Info" link and should not include attribution-list boilerplate.

---

## 16. Planning Link Formatting Rules

- Planning links render as compact pill-style buttons in destination headers.
- Labels should be short, action-oriented, and consistently capitalized.
- Invalid or missing URLs must be omitted rather than rendered as dead controls.

---

## 17. Month-Specific Weather Grounding Rules

- Temperature fields in `expected_environment` must be post-normalized from historical monthly climate normals using destination coordinates and trip month.
- Grounding source currently uses Open-Meteo historical daily temperatures, aggregated to monthly daytime high and overnight low normals.
- Narrative summary temperature claims must be rewritten to match grounded values.

---

## 18. En-Route Detour Display Rules

- CAN'T-MISS ENROUTE entries should display detour overhead (`detour_distance_miles`, `detour_time_minutes`) when available.
- Zero-detour stops should remain valid and may display as on-route.
- Stop cards should use content-appropriate iconography (trail, viewpoint, food, market, etc.) and should avoid forced em-dash-only sentence formatting.
