"""
main.py — CLI entry point for the Road Trip Itinerary Generator.

Usage:
  python -m generator.main --manifest trip_manifest.yaml --output output/

Flags:
  --manifest        Path to trip manifest YAML (required)
  --output          Output directory (default: output/)
  --config          Path to config.yaml (default: config.yaml)
    --llm-provider    Override LLM provider for this run
    --llm-model       Override LLM model for this run
  --dry-run         Parse + validate manifest only; no AI calls, no output
  --skip-images     Skip image fetching (useful for fast content iteration)
  --skip-events     Skip cultural events discovery
  --skip-url-discovery  Skip URL discovery (AI content only)
  --destination     Process only this destination id (repeatable)
  --verbose         Enable debug logging
"""
from __future__ import annotations
import logging, sys
from datetime import datetime, timezone
from pathlib import Path
import click
from generator import __version__, __template_version__

logger = logging.getLogger(__name__)


def _setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
        datefmt="%H:%M:%S",
    )


@click.command()
@click.option("--manifest", required=True, type=click.Path(exists=True), help="Trip manifest YAML")
@click.option("--output", default="output", show_default=True, help="Output directory")
@click.option("--config", "config_path", default="config.yaml", show_default=True, help="Config YAML")
@click.option(
    "--llm-provider",
    type=click.Choice(["openai", "anthropic", "deepseek", "gemini", "grok", "azure_openai"], case_sensitive=False),
    help="Override LLM provider for this run",
)
@click.option("--llm-model", type=str, help="Override LLM model for this run")
@click.option("--dry-run", is_flag=True, help="Parse & validate only; no AI calls")
@click.option("--skip-images", is_flag=True, help="Skip image fetching")
@click.option("--skip-events", is_flag=True, help="Skip cultural events discovery")
@click.option("--skip-url-discovery", is_flag=True, help="Skip URL discovery")
@click.option("--destination", "destinations", multiple=True, help="Limit to specific destination ids")
@click.option("--verbose", is_flag=True, help="Enable debug logging")
def main(
    manifest: str,
    output: str,
    config_path: str,
    llm_provider: str | None,
    llm_model: str | None,
    dry_run: bool,
    skip_images: bool,
    skip_events: bool,
    skip_url_discovery: bool,
    destinations: tuple[str, ...],
    verbose: bool,
) -> None:
    _setup_logging(verbose)
    output_dir = Path(output)
    output_dir.mkdir(parents=True, exist_ok=True)

    click.echo(f"🗺  Road Trip Itinerary Generator")
    click.echo(f"   Manifest : {manifest}")
    click.echo(f"   Output   : {output_dir.resolve()}")
    click.echo(f"   Config   : {config_path}")
    if llm_provider:
        click.echo(f"   LLM      : provider override = {llm_provider.lower()}")
    if llm_model:
        click.echo(f"   LLM      : model override = {llm_model}")
    if dry_run:
        click.echo("   Mode     : DRY RUN (no AI calls)")
    click.echo()

    # ── Stage 1: Parse & validate manifest ──────────────────────────────────
    click.echo("Stage 1/6 — Parsing manifest…")
    from generator.parser import ManifestParser
    parser = ManifestParser()
    trip = parser.load(manifest)

    if destinations:
        # Filter to requested destination ids
        trip["destinations"] = [
            d for d in trip["destinations"] if d["id"] in destinations
        ]
        if not trip["destinations"]:
            click.echo(f"  ERROR: None of {destinations} matched any destination id.", err=True)
            sys.exit(1)

    click.echo(f"  ✓ {len(trip['destinations'])} destination(s) loaded")

    if dry_run:
        click.echo("\n✅ Dry run complete — manifest valid.")
        return

    # ── Stage 2: Geocode + auto-enrich ──────────────────────────────────────
    click.echo("Stage 2/6 — Geocoding & enrichment…")
    from generator.geocoder import Geocoder
    from generator.nps_resolver import NPSResolver
    geo = Geocoder()
    nps = NPSResolver()
    for dest in trip["destinations"]:
        lat, lng = geo.geocode(dest["name"])
        dest["lat"] = lat
        dest["lng"] = lng
        dest["nps_park_code"] = nps.resolve(dest["name"])
        click.echo(f"  ✓ {dest['name']}: lat={lat:.4f} lng={lng:.4f} nps={dest['nps_park_code']}")

    # ── Stage 3: AI content generation ──────────────────────────────────────
    click.echo("Stage 3/6 — AI content generation…")
    from generator.llm_client import MultiLLMClient
    from generator.ai_content import AIContentGenerator
    from generator.costs import print_cost_summary, summarize_from_usage
    llm_overrides = dict(trip.get("trip", {}).get("llm", {}))
    if trip.get("trip", {}).get("llm_provider"):
        llm_overrides["provider"] = trip["trip"].get("llm_provider")
    if trip.get("trip", {}).get("llm_features"):
        llm_overrides["features"] = trip["trip"].get("llm_features")
    if llm_provider:
        llm_overrides["provider"] = llm_provider.lower()
    if llm_model:
        llm_overrides["model"] = llm_model
    llm_client = MultiLLMClient(config_path, llm_overrides=llm_overrides)
    ai_gen = AIContentGenerator(config_path, llm_client=llm_client)
    ai_gen.generate_all(trip)
    click.echo(f"  ✓ AI content generated for {len(trip['destinations'])} destination(s)")

    # ── Stage 4: Cultural events discovery ──────────────────────────────────
    if not skip_events:
        click.echo("Stage 4/6 — Cultural events discovery…")
        from generator.cultural_events import CulturalEventsDiscoverer
        events = CulturalEventsDiscoverer(config_path, llm_client=llm_client)
        events.discover(trip)
        click.echo("  ✓ Cultural events resolved")
    else:
        click.echo("Stage 4/6 — Cultural events discovery SKIPPED")

    # ── Stage 5a: Image fetching ─────────────────────────────────────────────
    if not skip_images:
        click.echo("Stage 5/6 — Fetching images…")
        from generator.image_fetcher import ImageFetcher
        fetcher = ImageFetcher(config_path)
        fetcher.fetch_all(trip)
        total = sum(len(d.get("images", [])) for d in trip["destinations"])
        click.echo(f"  ✓ {total} images fetched")
    else:
        click.echo("Stage 5/6 — Image fetching SKIPPED")
        for dest in trip["destinations"]:
            dest.setdefault("images", [])

    # ── Stage 5b: URL discovery ──────────────────────────────────────────────
    if not skip_url_discovery:
        click.echo("Stage 5b — URL discovery…")
        from generator.url_discovery import URLDiscoverer
        url_disc = URLDiscoverer(config_path)
        url_disc.discover_all(trip)
        click.echo("  ✓ URLs discovered and verified")
    else:
        click.echo("Stage 5b — URL discovery SKIPPED")

    # ── Stage 6: Assemble HTML ───────────────────────────────────────────────
    click.echo("Stage 6/6 — Assembling HTML…")
    from generator.attribution_builder import AttributionBuilder
    from generator.html_assembler import HTMLAssembler
    trip["_meta"] = {
        "generator_version": __version__,
        "template_version": __template_version__,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "llm": {
            "provider": llm_client.provider,
            "model": llm_client.model,
            "usage": llm_client.usage_summary(),
        },
    }
    attr_block = AttributionBuilder().build(trip)
    assembler = HTMLAssembler(config_path)
    html = assembler.assemble(trip, attr_block)

    output_file = output_dir / "index.html"
    output_file.write_text(html, encoding="utf-8")
    click.echo(f"  ✓ index.html written ({output_file.stat().st_size:,} bytes)")

    # ── Validate ─────────────────────────────────────────────────────────────
    from generator.html_validator import HTMLValidator
    from generator.report_writer import ReportWriter
    validator = HTMLValidator(config_path)
    report = validator.validate(output_file, trip)
    report_path = ReportWriter(output_dir).write(report)
    click.echo(f"  ✓ Validation report: {report_path}")

    predicted_cost, actual_cost = summarize_from_usage(trip.get("_meta", {}).get("llm", {}).get("usage", {}))
    print_cost_summary(
        model=trip.get("_meta", {}).get("llm", {}).get("model", llm_client.model),
        manifest_path=manifest,
        predicted_usd=predicted_cost,
        actual_usd=actual_cost,
    )

    if not report["valid"]:
        click.echo(f"\n⚠️  {report['error_count']} validation error(s) found:", err=True)
        for e in report["errors"]:
            click.echo(f"   ✗ {e}", err=True)
        sys.exit(2)

    click.echo(f"\n✅ Done! Open {output_file.resolve()} in your browser.")


if __name__ == "__main__":
    main()
