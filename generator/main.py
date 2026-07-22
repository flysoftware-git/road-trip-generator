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
import logging, os, sys
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
@click.option(
    "--environment",
    type=click.Choice(["dev", "test", "prod"], case_sensitive=False),
    help="Environment override (dev/test/prod). Optional.",
)
@click.option(
    "--env-file",
    type=click.Path(exists=True),
    help="Optional path to .env file. If provided, loaded before environment resolution.",
)
@click.option("--llm-model", type=str, help="Override LLM model for this run")
@click.option("--dry-run", is_flag=True, help="Parse & validate only; no AI calls")
@click.option("--skip-images", is_flag=True, help="Skip image fetching")
@click.option("--skip-events", is_flag=True, help="Skip cultural events discovery")
@click.option("--skip-url-discovery", is_flag=True, help="Skip URL discovery")
@click.option("--noschedule", is_flag=True, help="Suppress schedule card rendering in output HTML")
@click.option("--destination", "destinations", multiple=True, help="Limit to specific destination ids")
@click.option("--verbose", is_flag=True, help="Enable debug logging")
def main(
    manifest: str,
    output: str,
    config_path: str,
    llm_provider: str | None,
    environment: str | None,
    env_file: str | None,
    llm_model: str | None,
    dry_run: bool,
    skip_images: bool,
    skip_events: bool,
    skip_url_discovery: bool,
    noschedule: bool,
    destinations: tuple[str, ...],
    verbose: bool,
) -> None:
    _setup_logging(verbose)
    output_dir = Path(output)

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

    # ── Optional .env loading ───────────────────────────────────────────────
    if env_file:
        try:
            from dotenv import load_dotenv
            load_dotenv(env_file)
            click.echo(f"   EnvFile  : loaded from {env_file}")
        except Exception as exc:
            click.echo(f"   EnvFile  : failed to load ({exc})", err=True)

    # ── Stage 1: Parse & validate manifest ──────────────────────────────────
    click.echo("Stage 1/6 — Parsing manifest…")
    from generator.parser import ManifestParser
    parser = ManifestParser()
    trip = parser.load(manifest)

    # ── Hybrid environment selection ─────────────────────────────────────────
    env_from_manifest = trip.get("trip", {}).get("environment")
    env_from_cli = environment
    env_from_env = os.environ.get("ENVIRONMENT")

    environment_selected = (
        (env_from_cli or env_from_manifest or env_from_env or "dev").lower()
    )

    click.echo(
        click.style("   Env      : ", fg="cyan") +
        click.style(environment_selected, fg="green")
    )

    # Add environment tag to logger name
    logger.name = f"{logger.name}[{environment_selected}]"

    # Environment-aware output directory
    output_dir = Path(output) / environment_selected
    output_dir.mkdir(parents=True, exist_ok=True)

    click.echo()

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
    from concurrent.futures import ThreadPoolExecutor, as_completed
    geo = Geocoder()
    nps = NPSResolver()
    # Geocoding is sequential (Nominatim ToS: 1 req/sec)
    for dest in trip["destinations"]:
        lat, lng = geo._geocode(dest["name"])
        dest["lat"] = lat
        dest["lng"] = lng

    # Optional departure/return geocoding for full-route maps and first-card routing context.
    departure_name = trip.get("trip", {}).get("departure")
    return_name = trip.get("trip", {}).get("return")
    if departure_name:
        dlat, dlng = geo._geocode(departure_name)
        trip["trip"]["departure_lat"] = dlat
        trip["trip"]["departure_lng"] = dlng
    if return_name:
        rlat, rlng = geo._geocode(return_name)
        trip["trip"]["return_lat"] = rlat
        trip["trip"]["return_lng"] = rlng
    # NPS resolution is independent — run in parallel
    def _resolve_nps(dest: dict) -> None:
        dest["nps_park_code"] = nps.resolve(dest["name"])
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {pool.submit(_resolve_nps, d): d for d in trip["destinations"]}
        for f in as_completed(futures):
            f.result()
    for dest in trip["destinations"]:
        click.echo(f"  \u2713 {dest['name']}: lat={dest['lat']:.4f} lng={dest['lng']:.4f} nps={dest['nps_park_code']}")

    # ── Stage 3: AI content generation ──────────────────────────────────────
    click.echo("Stage 3/6 — AI content generation…")
    from generator.llm_client import MultiLLMClient
    from generator.ai_content import AIContentGenerator
    from generator.costs import print_cost_summary, summarize_from_usage
    llm_overrides = dict(trip.get("trip", {}).get("llm", {}))

    # ── Hybrid provider selection ───────────────────────────────────────────
    provider_from_manifest = trip.get("trip", {}).get("llm_provider")
    provider_from_cli = llm_provider
    provider_from_env = os.environ.get("LLM_PROVIDER")
    provider_selected = (provider_from_cli or provider_from_manifest or provider_from_env)

    if trip.get("trip", {}).get("llm_provider"):
        llm_overrides["provider"] = trip["trip"].get("llm_provider")
    if trip.get("trip", {}).get("llm_features"):
        llm_overrides["features"] = trip["trip"].get("llm_features")
    if llm_provider:
        llm_overrides["provider"] = llm_provider.lower()
    elif provider_selected:
        llm_overrides["provider"] = provider_selected.lower()

    click.echo(
        click.style("   LLM      : provider = ", fg="cyan") +
        click.style(llm_overrides.get("provider"), fg="green")
    )

    if llm_model:
        llm_overrides["model"] = llm_model

    # ── Optional environment-aware config merging ───────────────────────────
    try:
        import yaml
        with Path(config_path).open(encoding="utf-8") as f:
            cfg_full = yaml.safe_load(f) or {}
        if environment_selected in cfg_full:
            env_cfg = cfg_full[environment_selected]
            ai_env_cfg = env_cfg.get("ai", {})
            for key, val in ai_env_cfg.items():
                llm_overrides.setdefault(key, val)
    except Exception:
        pass
    llm_client = MultiLLMClient(
        config_path=config_path,
        llm_overrides=llm_overrides,
    )

    ai_gen = AIContentGenerator(config_path, llm_client=llm_client)
    ai_gen.generate_all(trip)

    if noschedule:
        for dest in trip.get("destinations", []):
            if "ai_content" in dest and isinstance(dest["ai_content"], dict):
                dest["ai_content"]["possible_daily_schedule"] = []

    click.echo(f"  ✓ AI content generated for {len(trip['destinations'])} destination(s)")

    # ── Stages 4 + 5a + 5b: run concurrently (all independent of each other) ─
    from concurrent.futures import ThreadPoolExecutor, as_completed as _as_completed

    def _run_events() -> None:
        if not skip_events:
            click.echo("Stage 4/6 — Cultural events discovery…")
            from generator.cultural_events import CulturalEventsDiscoverer
            CulturalEventsDiscoverer(config_path, llm_client=llm_client).discover(trip)
            click.echo("  ✓ Cultural events resolved")
        else:
            click.echo("Stage 4/6 — Cultural events discovery SKIPPED")

    def _run_images() -> None:
        if not skip_images:
            click.echo("Stage 5/6 — Fetching images…")
            from generator.image_fetcher import ImageFetcher
            ImageFetcher(config_path, output_dir=output_dir / "images").fetch_all(trip)
            total = sum(len(d.get("images", [])) for d in trip["destinations"])
            click.echo(f"  ✓ {total} images fetched")
        else:
            click.echo("Stage 5/6 — Image fetching SKIPPED")
            for dest in trip["destinations"]:
                dest.setdefault("images", [])

    def _run_urls() -> None:
        if not skip_url_discovery:
            click.echo("Stage 5b — URL discovery…")
            from generator.url_discovery import URLDiscoverer
            URLDiscoverer(config_path, llm_client=llm_client).discover_all(trip)
            click.echo("  ✓ URLs discovered and verified")
        else:
            click.echo("Stage 5b — URL discovery SKIPPED")

    click.echo("Stages 4–5b — Cultural events, images, and URL discovery (parallel)…")
    with ThreadPoolExecutor(max_workers=3) as _stage_pool:
        _stage_futures = [_stage_pool.submit(fn) for fn in (_run_events, _run_images, _run_urls)]
        for _f in _as_completed(_stage_futures):
            _f.result()

    # ── Stage 6: Assemble HTML ───────────────────────────────────────────────
    click.echo("Stage 6/6 — Assembling HTML…")
    from generator.attribution_builder import AttributionBuilder
    from generator.html_assembler import HTMLAssembler
    trip["_meta"] = {
        "generator_version": __version__,
        "template_version": __template_version__,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "environment": environment_selected,
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
        environment=environment_selected,
    )
    usage_models = trip.get("_meta", {}).get("llm", {}).get("usage", {}).get("models", [])
    if usage_models:
        click.echo("  Usage breakdown by provider/model:")
        for row in usage_models:
            click.echo(
                f"    - {row.get('provider')}/{row.get('model')}: "
                f"calls={row.get('calls', 0)} tokens={row.get('total_tokens', 0)} "
                f"est=${row.get('estimated_cost_usd', 0.0):.4f}"
            )

    if not report["valid"]:
        click.echo(f"\n⚠️  {report['error_count']} validation error(s) found:", err=True)
        for e in report["errors"]:
            click.echo(f"   ✗ {e}", err=True)
        sys.exit(2)

    click.echo(f"\n✅ Done! Open {output_file.resolve()} in your browser.")


if __name__ == "__main__":
    main()
