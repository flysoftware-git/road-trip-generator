"""
ai_content.py — Azure OpenAI content generation.

CRITICAL: AI must NEVER generate URLs. This module produces names,
descriptions, schedules, and structured content only. All URLs are
discovered separately by url_discovery.py after this stage completes.
"""
from __future__ import annotations
import json, logging, os
from pathlib import Path
from typing import Any
from openai import AzureOpenAI
from tenacity import retry, stop_after_attempt, wait_exponential

logger = logging.getLogger(__name__)
PROMPTS_DIR = Path(__file__).parent.parent / "prompts"


class AIContentGenerator:
    def __init__(self, config_path: Path | str = "config.yaml") -> None:
        import yaml
        with Path(config_path).open() as f:
            self._config = yaml.safe_load(f)
        self._client = AzureOpenAI(
            azure_endpoint=os.environ["AZURE_OPENAI_ENDPOINT"],
            api_key=os.environ["AZURE_OPENAI_API_KEY"],
            api_version=os.environ.get("AZURE_OPENAI_API_VERSION", "2024-02-01"),
        )
        self._deployment = os.environ["AZURE_OPENAI_DEPLOYMENT"]
        self._system_prompt = (PROMPTS_DIR / "system_prompt.txt").read_text(encoding="utf-8")
        self._dest_template = (PROMPTS_DIR / "destination_content.txt").read_text(encoding="utf-8")
        self._drives_template = (PROMPTS_DIR / "scenic_drives.txt").read_text(encoding="utf-8")

    def generate_destination_content(self, trip: dict[str, Any]) -> None:
        """Generate AI content for every destination. Attaches 'ai_content' in-place."""
        destinations = trip.get("destinations", [])
        for i, dest in enumerate(destinations):
            prev = destinations[i - 1]["name"] if i > 0 else "none"
            logger.info("Generating AI content for '%s'…", dest["name"])
            dest["ai_content"] = self._generate_for_destination(dest, trip["trip"], prev)

    def generate_scenic_drive_descriptions(self, trip: dict[str, Any]) -> None:
        """Generate scenic drive popup descriptions. Attaches 'scenic_drives' in-place."""
        for dest in trip.get("destinations", []):
            logger.info("Generating scenic drives for '%s'…", dest["name"])
            dest["scenic_drives"] = self._generate_drives(dest)

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
        resp = self._client.chat.completions.create(
            model=self._deployment,
            messages=[
                {"role": "system", "content": self._system_prompt},
                {"role": "user", "content": prompt},
            ],
            temperature=self._config["azure_openai"]["temperature"],
            max_tokens=self._config["azure_openai"]["max_tokens"],
            response_format={"type": "json_object"},
        )
        return json.loads(resp.choices[0].message.content)

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=2, min=2, max=30))
    def _generate_drives(self, dest: dict[str, Any]) -> list[dict[str, Any]]:
        import re
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
        resp = self._client.chat.completions.create(
            model=self._deployment,
            messages=[
                {"role": "system", "content": self._system_prompt},
                {"role": "user", "content": prompt},
            ],
            temperature=self._config["azure_openai"]["temperature"],
            max_tokens=2048,
            response_format={"type": "json_object"},
        )
        data = json.loads(resp.choices[0].message.content)
        return data.get("scenic_drives", [])
