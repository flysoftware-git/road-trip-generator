# Road Trip Itinerary Generator — Requirements Document
**Version 0.9 · July 20, 2026**

### Changelog from v0.8
| # | Section | Change |
|---|---|---|
| 1 | §7 | `v2.5_template.html` converted from hardcoded reference document to true generator template: trip title, nav tabs, Google Maps URL, Leaflet map markers, and all destination sections now use injection placeholders |
| 2 | §7 | Assembler updated to produce output matching template's CSS/JS conventions: section IDs use `section-{id}` format, CSS class `dest-section`, drive buttons use `class="drive-link"` + `data-drive-title`, `DRIVE_DESCRIPTIONS` keyed by raw title string |
| 3 | §7 | Validator updated to check `data-drive-title` attribute (not `data-drive-key`) and `id="section-{id}"` format (not bare `id="{id}"`) |
| 4 | §11 | Added `XAI_MODEL` env var to select Grok model; `XAI_API_KEY` already present since v0.8 |

### Changelog from v0.7
| # | Section | Change |
|---|---|---|
| 1 | §2, §5, §11 | Search client migrated from Google Programmable Search Engine (v1.4, rate-limited) to xAI Grok semantic search (v1.5); env var changed to `XAI_API_KEY` (single key, simpler setup) |

### Changelog from v0.6
| # | Section | Change |
|---|---|---|
| 1 | §2, §5, §11 | Search client migrated from Bing Web Search API (v1.3) to Google Programmable Search Engine (v1.4); env vars changed to `GOOGLE_SEARCH_API_KEY` + `GOOGLE_SEARCH_ENGINE_ID` |

### Changelog from v0.5
| # | Section | Change |
|---|---|---|
| 1 | §2, §5, §11 | Search client migrated from Brave Search API (v1.2) to Bing Web Search API — Azure AI Services (v1.3); env var renamed `BRAVE_SEARCH_API_KEY` → `BING_SEARCH_API_KEY` |
| 2 | §2 | Parallel execution model added: AI calls, cultural events, image fetching, and URL discovery now run concurrently across destinations via `ThreadPoolExecutor` |

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
│  • Scenic drives + viewpoints (fully AI-discovered)     │
│  • Cultural events via Bing Search + AI synthesis       │
└────────────────────┬────────────────────────────────────┘
                     │
                     ▼
┌─────────────────────────────────────────────────────────┐
│  STAGE 3: URL Discovery (Bing Search API)               │
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

`trip.llm.provider` supports: `openai`, `anthropic`, `deepseek`, `gemini`.

### 3.2 Auto-Resolved Fields (NOT in manifest)

| Field | Resolution Method |
|---|---|
| `lat` / `lng` | Nominatim geocoding from `name` |
| `nps_park_code` | Keyword detection + NPS API text search |
| `weather_url` | Constructed from lat/lng |
| `google_maps_link` | Auto-generated from ordered destination coordinates |

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
  "expected_environment": "string — sensory lead + temp range + operational note",
  "getting_here": {
    "from_previous": "string — driving directions from previous destination",
    "en_route_stops": [
      {
        "name": "string",
        "highway_reference": "string — highway and exit/milepost",
        "description": "string — what makes this worth stopping for",
        "time_required": "string — e.g., '30 minutes'"
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
      "ambient_scene": "string — what it feels like to be there"
    }
  ]
}
```

**Format B — Honest fallback (no invented events):**
```json
{
  "has_events": false,
  "honest_assessment": "string — what the park/town IS good for in this season",
  "local_tip": "string — one practical insight"
}
```

The AI must NEVER invent events. Remote national parks almost always return Format B.

---

## 5. URL Discovery

AI content generation and URL discovery are strictly separate pipeline stages. **AI never generates URLs.**

After AI content is generated, the URL Discoverer runs Bing Search for every named entity:

1. **Attractions in NPS parks:** First query uses `site:nps.gov` domain filter
2. **All attractions:** Falls back to AllTrails domain filter
3. **Restaurants:** Two-pass — Google Maps domain filter, then TripAdvisor
4. **All items:** 4-variant fallback query sequence (most specific → broadest)
5. **Final fallback:** Empty string stored (no fabricated URLs)

Every discovered URL is HTTP HEAD-verified before storage.

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
4. Generator credit line with link to repository

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
