"""
ai_content.py — Multi-provider LLM content generation.

CRITICAL: AI must NEVER generate URLs. This module produces names,
descriptions, schedules, and structured content only. All URLs are
discovered separately by url_discovery.py after this stage completes.
"""
from __future__ import annotations
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any
from tenacity import retry, stop_after_attempt, wait_exponential
from generator.llm_client import MultiLLMClient

logger = logging.getLogger(__name__)
PROMPTS_DIR = Path(__file__).parent.parent / "prompts"


class AIContentGenerator:
    def __init__(
        self,
        config_path: Path | str = "config.yaml",
        llm_client: MultiLLMClient | None = None,
    ) -> None:
        import yaml
        with Path(config_path).open() as f:
            self._config = yaml.safe_load(f)
        self._llm = llm_client or MultiLLMClient(config_path)
        self._system_prompt = (PROMPTS_DIR / "system_prompt.txt").read_text(encoding="utf-8")
        self._dest_template = (PROMPTS_DIR / "destination_content.txt").read_text(encoding="utf-8")
        self._drives_template = (PROMPTS_DIR / "scenic_drives.txt").read_text(encoding="utf-8")

    def generate_destination_content(self, trip: dict[str, Any]) -> None:
        """Generate AI content for every destination. Attaches 'ai_content' in-place."""
        destinations = trip.get("destinations", [])
        prev_names = ["none"] + [d["name"] for d in destinations[:-1]]

        def _one(args: tuple[int, dict]) -> None:
            i, dest = args
            logger.info("Generating AI content for '%s'…", dest["name"])
            dest["ai_content"] = self._generate_for_destination(dest, trip["trip"], prev_names[i])

        with ThreadPoolExecutor(max_workers=min(len(destinations), 4)) as pool:
            futures = [pool.submit(_one, (i, d)) for i, d in enumerate(destinations)]
            for f in as_completed(futures):
                f.result()

    def generate_scenic_drive_descriptions(self, trip: dict[str, Any]) -> None:
        """Generate scenic drive popup descriptions. Attaches 'scenic_drives' in-place."""
        destinations = trip.get("destinations", [])

        def _one(dest: dict) -> None:
            logger.info("Generating scenic drives for '%s'…", dest["name"])
            dest["scenic_drives"] = self._generate_drives(dest)

        with ThreadPoolExecutor(max_workers=min(len(destinations), 4)) as pool:
            futures = [pool.submit(_one, d) for d in destinations]
            for f in as_completed(futures):
                f.result()

    def generate_all(self, trip: dict[str, Any]) -> None:
        self.generate_destination_content(trip)
        self.generate_scenic_drive_descriptions(trip)

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=2, min=2, max=30))
    def _generate_for_destination(
        self, dest: dict[str, Any], trip_meta: dict[str, Any], prev: str
    ) -> dict[str, Any]:
        seeds = dest.get("seeds", [])
        prompt = self._dest_template.format(
            destination_name=dest["name"],
            dates=dest["dates"],
            trip_title=trip_meta["title"],
            previous_destination=prev,
            seeds="\n  ".join(f"- {s}" for s in seeds) if seeds else "  (none — generate full recommendations)",
        )
        return self._llm.generate_json(
            system_prompt=self._system_prompt,
            user_prompt=prompt,
            operation=f"destination_content:{dest['id']}",
            temperature=self._config.get("ai", {}).get("temperature", self._config.get("azure_openai", {}).get("temperature", 0.7)),
            max_tokens=self._config.get("ai", {}).get("max_tokens", self._config.get("azure_openai", {}).get("max_tokens", 4096)),
        )

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=2, min=2, max=30))
    def _generate_drives(self, dest: dict[str, Any]) -> list[dict[str, Any]]:
        # Derive region from destination name
        region_map = {"utah": "Utah", "colorado": "Colorado", "new mexico": "New Mexico",
                      "arizona": "Arizona", "nevada": "Nevada", "california": "California"}
        name_lower = dest["name"].lower()
        region = next((v for k, v in region_map.items() if k in name_lower), "Western United States")

        prompt = self._drives_template.format(
            destination_name=dest["name"],
            dates=dest["dates"],
            region=region,
        )
        data = self._llm.generate_json(
            system_prompt=self._system_prompt,
            user_prompt=prompt,
            operation=f"scenic_drives:{dest['id']}",
            temperature=self._config.get("ai", {}).get("temperature", self._config.get("azure_openai", {}).get("temperature", 0.7)),
            max_tokens=2048,
        )
        return data.get("scenic_drives", [])
