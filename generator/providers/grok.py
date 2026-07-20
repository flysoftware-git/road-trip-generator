# generator/providers/grok.py
# xAI Grok provider using the OpenAI-compatible /chat/completions endpoint.
# Uses the same XAI_API_KEY / XAI_MODEL env vars as grok_search.py.

import os
import requests
from typing import Any

_BASE_URL = "https://api.x.ai/v1/chat/completions"


class GrokProvider:
    def __init__(self, model: str | None = None) -> None:
        self.api_key = os.environ["XAI_API_KEY"]
        self.model = model or os.environ.get("XAI_MODEL", "grok-2-latest")
        self.base_url = _BASE_URL

    def create_json_completion(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        temperature: float,
        max_tokens: int,
    ) -> tuple[str, dict[str, Any]]:

        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": temperature,
            "max_tokens": max_tokens,
        }

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        resp = requests.post(self.base_url, json=payload, headers=headers, timeout=60)
        resp.raise_for_status()
        data = resp.json()

        text = data["choices"][0]["message"]["content"]
        usage_raw = data.get("usage", {})
        usage = {
            "prompt_tokens": usage_raw.get("prompt_tokens", 0),
            "completion_tokens": usage_raw.get("completion_tokens", 0),
            "model": self.model,
        }

        return text, usage

