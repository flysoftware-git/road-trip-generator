# generator/providers/grok.py

import os
import requests
from typing import Any


class GrokProvider:
    def __init__(self, model: str | None = None) -> None:
        self.api_key = os.environ["GROK_API_KEY"]
        self.model = model or os.environ.get("GROK_MODEL", "grok-2-latest")
        self.base_url = "https://api.x.ai/v1/responses"

    def create_json_completion(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        temperature: float,
        max_tokens: int,
    ) -> tuple[str, dict[str, Any]]:

        # Foundry Grok requires a single "input" string
        prompt = system_prompt + "\n\n" + user_prompt

        payload = {
            "model": self.model,
            "input": prompt,
            "temperature": temperature,
        }

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        resp = requests.post(self.base_url, json=payload, headers=headers, timeout=60)
        resp.raise_for_status()
        data = resp.json()

        text = data.get("output_text", "")

        usage = {
            "prompt_tokens": data.get("usage", {}).get("input_tokens", 0),
            "completion_tokens": data.get("usage", {}).get("output_tokens", 0),
            "model": self.model,
        }

        return text, usage
