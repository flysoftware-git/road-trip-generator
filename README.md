# Road Trip Itinerary Generator

A Python CLI tool that transforms a minimal YAML trip manifest into a single self-contained `index.html` road trip itinerary — visually and functionally identical to the [Southwest Road Trip Itinerary v2.5](https://swiftsure-pro.github.io/Travel-apps/), with AI-generated content tailored to any destinations worldwide.

Write a manifest in minutes. Get a polished, deployable trip guide with:
- AI-generated environment descriptions, attraction writeups, en-route stops, and daily schedules
- Auto-discovered cultural events (via Bing Search + AI synthesis — never hallucinated)
- 5–6 restaurant recommendations per destination with cuisine and price diversity
- 2–4 scenic drives/viewpoints per destination (fully AI-discovered, not user-seeded)
- Verified URLs for every attraction, restaurant, and stop
- Destination images from NPS API and Wikimedia Commons
- Interactive Leaflet map with auto-generated Google Maps overview link
- Collapsible attribution footer with image credits and events disclaimer

## Quick Start

### 1. Clone and install

```bash
git clone https://github.com/flysoftware-git/road-trip-generator.git
cd road-trip-generator
pip install -r requirements.txt
```

### 2. Set up environment variables

```bash
cp .env.example .env
# Edit .env with your API keys
```

Required variables:
| Variable | Description |
|---|---|
| `AZURE_OPENAI_ENDPOINT` | Your Azure OpenAI resource URL |
| `AZURE_OPENAI_API_KEY` | Your Azure OpenAI API key |
| `AZURE_OPENAI_DEPLOYMENT` | Your model deployment name (e.g., `gpt-4o`) |
| `BING_SEARCH_API_KEY` | Bing Web Search API key |

Optional:
| Variable | Description |
|---|---|
| `NPS_API_KEY` | NPS API key (defaults to `DEMO_KEY`, which is rate-limited) |
| `AZURE_OPENAI_API_VERSION` | API version (default: `2024-02-01`) |

### 3. Write your manifest

```yaml
trip:
  title: "Pacific Coast Highway"
  subtitle: "September 2026 — California"
  theme_color: "#2E6B8A"

destinations:
  - id: sf
    name: "San Francisco, California"
    dates: "September 5–7, 2026"
    planning_links:
      - label: "Hotel Reservation"
        url: "https://..."
    seeds:
      - "Golden Gate Bridge"
      - "Alcatraz"
      - "Lombard Street"

  - id: bigsur
    name: "Big Sur, California"
    dates: "September 7–9, 2026"
    planning_links:
      - label: "Campsite Reservation"
        url: "https://www.recreation.gov/..."
    seeds:
      - "McWay Falls"
      - "Bixby Creek Bridge"
```

Seeds are **name hints only** — attractions, hikes, or experiences you specifically want included. The AI discovers scenic drives, cultural events, and restaurants independently.

### 4. Generate

```bash
python -m generator.main --manifest trip_manifest.yaml --output output/
```

Your itinerary is at `output/index.html`. Open it in any browser or deploy to GitHub Pages.

---

## CLI Options

```
python -m generator.main [OPTIONS]

  --manifest PATH          Trip manifest YAML (required)
  --output PATH            Output directory [default: output/]
  --config PATH            Config YAML [default: config.yaml]
  --dry-run                Parse & validate manifest only; no AI calls
  --skip-images            Skip image fetching (faster iteration)
  --skip-events            Skip cultural events discovery
  --skip-url-discovery     Skip URL discovery (AI content only)
  --destination TEXT       Limit to specific destination id (repeatable)
  --verbose                Enable debug logging
```

**Examples:**

```bash
# Validate manifest only
python -m generator.main --manifest trip.yaml --dry-run

# Generate content for one destination only
python -m generator.main --manifest trip.yaml --destination zion

# Fast iteration (skip images and events)
python -m generator.main --manifest trip.yaml --skip-images --skip-events
```

---

## Pipeline

```
manifest.yaml
    │
    ▼  Stage 1: Parse & Validate
       • Schema validation (jsonschema)
       • Seed URL rejection
       • Planning link HTTP verification
    │
    ▼  Stage 2: Auto-Enrich
       • Geocoding via Nominatim
       • NPS park code detection
       • Google Maps URL auto-generation
    │
    ▼  Stage 3: AI Content (Azure OpenAI)
       • Environment, attractions, en-route stops, schedule
       • Scenic drives & viewpoints (fully AI-discovered)
       • Cultural events (Bing Search + AI synthesis)
    │
    ▼  Stage 4: URL Discovery (Bing Search)
       • NPS.gov filter for park attractions
       • Two-pass restaurant strategy (Google Maps → TripAdvisor)
       • 4-variant fallback per item, HEAD-verified
    │
    ▼  Stage 5: Images
       • NPS API (for national parks)
       • Wikimedia Commons (all destinations)
       • 4-attempt fallback on failure
       • Hard fail if < 2 images per destination
    │
    ▼  Stage 6: Assemble + Validate
       • SHA-256 template checksum verification
       • HTML assembly via Python strings
       • Div balance, script isolation, drive key checks
       • JSON validation report
    │
    ▼
output/index.html   ← Deploy to GitHub Pages
```

---

## Manifest Specification

See [docs/requirements.md](docs/requirements.md) for the full v0.5 requirements specification including:
- Complete manifest schema
- AI content JSON schemas
- URL discovery strategy
- Image pipeline details
- Configuration reference

---

## Testing

```bash
pip install pytest
pytest tests/ -v
```

Test fixtures in `tests/fixtures/` include sample manifests, AI outputs, Bing results, and NPS API responses for offline testing.

---

## Template Integrity

The v2.5 HTML template (`templates/v2.5_template.html`) is frozen and checksum-verified on every run. The SHA-256 hash is stored in `templates/checksums.txt`. A mismatch causes an immediate hard failure — the template may not be modified without regenerating the checksum.

To update the template checksum after an intentional template change:

```bash
python -c "
import hashlib
t = open('templates/v2.5_template.html', encoding='utf-8').read()
h = hashlib.sha256(t.encode()).hexdigest()
open('templates/checksums.txt', 'w').write(h + '  templates/v2.5_template.html\n')
print('Checksum updated:', h[:16] + '...')
"
```

---

## Configuration

Edit `config.yaml` to tune AI behavior, image counts, and URL discovery:

```yaml
ai:
  temperature: 0.7          # LLM temperature (0.0–1.0)
  max_tokens: 3000          # Max tokens per AI response

images:
  min_per_destination: 2    # Hard fail if fewer verified images
  max_per_destination: 4    # Max images fetched per destination

url_discovery:
  max_fallback_attempts: 4  # Query variants tried per item
```

---

## Output Structure

```
output/
├── index.html              ← Self-contained trip itinerary
├── images/
│   └── {md5hash}.jpg       ← Downloaded destination photos
└── validation_report.json  ← Post-assembly validation results
```

---

## Example Manifest (Southwest Road Trip)

`trip_manifest.yaml` in the project root is the reverse-engineered manifest for the Southwest Road Trip v2.5 (Zion → Bryce → Capitol Reef → Moab → Telluride → Pagosa Springs → Santa Fe). Use it for testing and comparing generator output against the hand-crafted v2.5 reference.

---

## License

MIT — see [LICENSE](LICENSE)
