"""query-master's own LLM client, bound from a checkout: the same backends, key rotation and retries the product uses.

The backend comes from query-master's settings, read from the environment (LLM_BACKEND, OPENROUTER_API_KEYS,
RunPod settings ...). The model is EKG_LLM_MODEL, else AGENTIC_LLM_MODEL, else the backend's own default.
Nothing here holds a key: query-master reads it from the environment at call time.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

_get_client = _settings = None
MODEL: str | None = None


def bind(query_master: Path, model: str | None = None) -> str:
    """Import query-master's client and settings; returns 'backend model' for the report."""
    global _get_client, _settings, MODEL
    root = str(Path(query_master).resolve())
    if root not in sys.path:
        sys.path.insert(0, root)
    from app.core.config import settings
    from app.services.llm.client import get_llm_client
    _get_client, _settings = get_llm_client, settings
    MODEL = model or os.environ.get("EKG_LLM_MODEL") or settings.AGENTIC_LLM_MODEL or None
    return describe()


def describe() -> str:
    backend = _settings.LLM_BACKEND
    default = _settings.OPENROUTER_MODEL if backend == "openrouter" else "backend default"
    return f"{backend} {MODEL or default}"


async def ask(prompt: str, name: str, schema: dict, max_tokens: int = 700) -> dict:
    """One structured call. Returns the parsed JSON object, or {"error": ...} as query-master's client does."""
    client = _get_client(model=MODEL)  # OpenRouter: the next key in the round robin
    return await client.explain(prompt, model=MODEL, json_schema={"name": name, "strict": True, "schema": schema},
                                priority="bulk", max_tokens=max_tokens)
