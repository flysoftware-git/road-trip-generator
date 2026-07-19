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

### 1. Clone

```bash
git clone https://github.com/flysoftware-git/road-trip-generator.git
cd road-trip-generator
```

### 2. Bootstrap environment (Windows)

PowerShell:

```powershell
.\scripts\bootstrap.ps1
```

Batch wrapper:

```bat
scripts\bootstrap.bat
```

Optional flags:

```powershell
# Rebuild venv from scratch
.\scripts\bootstrap.ps1 -Recreate

# Skip tests during setup
.\scripts\bootstrap.ps1 -SkipTests
```

### 3. Set up environment variables

`scripts/bootstrap.ps1` auto-creates `.env` from `.env.example` if missing.
Then edit `.env` with your API keys.

Required variables:
| Variable | Description |
|---|---|
| `OPENAI_API_KEY` | OpenAI API key (when `ai.provider: openai`) |
| `ANTHROPIC_API_KEY` | Anthropic API key (when `ai.provider: anthropic`) |
| `DEEPSEEK_API_KEY` | DeepSeek API key (when `ai.provider: deepseek`) |
| `GEMINI_API_KEY` | Gemini API key (when `ai.provider: gemini`) |
| `GROK_API_KEY` | Grok API key (when `ai.provider: grok`) |
| `BING_SEARCH_API_KEY` | Bing Web Search API key (Azure AI Services) |

Optional:
| Variable | Description |
|---|---|
| `NPS_API_KEY` | NPS API key (defaults to `DEMO_KEY`, which is rate-limited) |
| `OPENAI_MODEL` | Default OpenAI model override (e.g. `gpt-4o-mini`) |
| `AZURE_OPENAI_*` | Legacy Azure OpenAI compatibility variables |

### 4. Write your manifest

```yaml
trip:
  title: "Pacific Coast Highway"
  subtitle: "September 2026 — California"
  theme_color: "#2E6B8A"
  llm:
    provider: "openai"
    model: "gpt-4o-mini"
    temperature: 0.6
    max_tokens: 4096

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

### 5. Generate

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
  --llm-provider [openai|anthropic|deepseek|gemini|grok|azure_openai]
                           Override LLM provider for this run
  --llm-model TEXT         Override LLM model for this run
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

# Test with a different provider without editing config.yaml/manifest
python -m generator.main --manifest trip.yaml --llm-provider anthropic

# Test provider+model combination from CLI
python -m generator.main --manifest trip.yaml --llm-provider openai --llm-model gpt-4o-mini
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

```powershell
.\venv\Scripts\python.exe -m pytest tests -v
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

## Nested Folder Cleanup (Windows)

If you accidentally extracted or cloned the repo into itself (for example `road-trip-generator/road-trip-generator`), keep one canonical root and remove the duplicate copy.

Safe process:

```powershell
# 1) See current status
git status --short

# 2) Compare duplicate folder contents (optional)
Get-ChildItem .\road-trip-generator -Recurse | Select-Object FullName

# 3) Remove accidental nested copy if not needed
Remove-Item -Recurse -Force .\road-trip-generator

# 4) Verify tree is clean
git status --short
```

If the nested folder has unique files you need, move them into the root before deletion.

---

## License

MIT — see [LICENSE](LICENSE)
