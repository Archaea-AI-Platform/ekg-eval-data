"""query-master's own LLM client, bound from a checkout: the same backends, key rotation and retries the product uses.

The backend comes from query-master's settings, read from the environment (LLM_BACKEND, OPENROUTER_API_KEYS,
RunPod settings ...). The model is EKG_LLM_MODEL, else AGENTIC_LLM_MODEL, else the backend's own default.
Nothing here holds a key: query-master reads it from the environment at call time.

No call carries an output cap of its own: `ask` passes no max_tokens, so query-master's client takes its
uncapped path (a streamed reply is limited only by the model's window, or by the provider's own hard limit).
`window_tokens()` is the one place the window comes from, `reply_limit()` the one place the provider's cap on
one reply comes from (OpenRouter lists it as max_completion_tokens; a hard limit no request can lift);
fill_budget.sizes() turns both into a budget.
"""

from __future__ import annotations

import asyncio
import contextvars
import os
import sys
from pathlib import Path

from . import fill_budget

_get_client = _settings = _context = None
MODEL: str | None = None
_REPLY_LIMIT: tuple[int, str] = (0, "")  # what window discovery learned about the provider's reply cap
# the token usage of the call in flight in this task: query-master's client reads `usage` off every 200 response
# (LLMClient._record_token_usage); bind() wraps that method so that it also adds the numbers here, per call
_CALL_USAGE: contextvars.ContextVar = contextvars.ContextVar("ekg_call_usage", default=None)


def _count_usage():
    """Wrap query-master's usage hook (once) so that each call's tokens reach the caller: prompt, completion
    (thinking included), reasoning when the provider breaks it out, and cost when it reports one. Several
    attempts of one call (a retry, a re-ask) add up. Nothing in query-master changes: its own recording runs too."""
    from app.services.llm import client as qm_client
    base = qm_client.LLMClient
    if getattr(base, "_ekg_counts_usage", False):
        return
    original = base._record_token_usage

    def record(self, resp):
        try:
            box = _CALL_USAGE.get()
            if box is not None:
                u = (resp.json() or {}).get("usage") or {}
                for k in ("prompt_tokens", "completion_tokens"):
                    box[k] = box.get(k, 0) + int(u.get(k) or 0)
                reasoning = (u.get("completion_tokens_details") or {}).get("reasoning_tokens")
                if reasoning:
                    box["reasoning_tokens"] = box.get("reasoning_tokens", 0) + int(reasoning)
                if isinstance(u.get("cost"), (int, float)):
                    box["cost"] = round(box.get("cost", 0.0) + float(u["cost"]), 8)
                box["attempts"] = box.get("attempts", 0) + 1
        except Exception:   # best effort, never in the way of the call
            pass
        return original(self, resp)
    base._record_token_usage = record
    base._ekg_counts_usage = True


def bind(query_master: Path, model: str | None = None) -> str:
    """Import query-master's client, settings and context-window module; returns 'backend model' for the report."""
    global _get_client, _settings, _context, MODEL
    root = str(Path(query_master).resolve())
    if root not in sys.path:
        sys.path.insert(0, root)
    from app.core.config import settings
    from app.services.llm import context_window
    from app.services.llm.client import get_llm_client
    _get_client, _settings, _context = get_llm_client, settings, context_window
    _count_usage()
    MODEL = model or os.environ.get("EKG_LLM_MODEL") or settings.AGENTIC_LLM_MODEL or None
    # sizing follows query-master's own figures, so a change there changes it here
    fill_budget.PROMPT_SHARE = context_window.compact_at()
    fill_budget.CHARS_PER_TOKEN = context_window.DEFAULT_CHARS_PER_TOKEN
    return describe()


def describe() -> str:
    backend = _settings.LLM_BACKEND
    default = _settings.OPENROUTER_MODEL if backend == "openrouter" else "backend default"
    return f"{backend} {MODEL or default}"


async def _discover() -> tuple[int, str]:
    global _REPLY_LIMIT
    backend = (_settings.LLM_BACKEND or "").strip().lower()
    if backend == "openrouter":
        # OpenRouter's public model list (no key, not a model call); context_window keeps it for the process.
        # It gives the window and the provider's largest reply; LLM_CONTEXT_WINDOW_TOKENS overrides the window only.
        forced = _context.forced_window()
        try:
            import httpx
            async with httpx.AsyncClient() as http:
                window, largest_reply = await _context.openrouter_limits(http, MODEL or _settings.OPENROUTER_MODEL)
        except Exception:
            if not forced:
                raise
            window, largest_reply = forced, 0
        if largest_reply:
            _REPLY_LIMIT = (int(largest_reply), "OpenRouter's model list (max_completion_tokens)")
        return (forced, "LLM_CONTEXT_WINDOW_TOKENS") if forced else (window, "OpenRouter's model list")
    forced = _context.forced_window()
    if forced:
        return forced, "LLM_CONTEXT_WINDOW_TOKENS"
    return _context.window_tokens(), f"query-master context_window ({backend or 'runpod'})"


def window_tokens(override: int | None = None) -> tuple[int, str]:
    """(the served model's context window in tokens, where the number came from); 0 when nobody knows.

    Order: `override` (the --window-tokens flag) or EKG_LLM_WINDOW_TOKENS, then query-master's context_window:
    LLM_CONTEXT_WINDOW_TOKENS, OpenRouter's model list for a hosted model, what the pod reports or is
    configured with. 0 makes fill_budget.sizes() use its safe fallback."""
    if override:
        return int(override), "--window-tokens"
    if (env := os.environ.get("EKG_LLM_WINDOW_TOKENS", "").strip()).isdigit() and int(env) > 0:
        return int(env), "EKG_LLM_WINDOW_TOKENS"
    if _context is None:
        return 0, ""
    try:
        return asyncio.run(_discover())
    except Exception as e:  # no network, no httpx: unknown, never fatal
        return 0, f"unknown ({type(e).__name__})"


def reply_limit(override: int | None = None) -> tuple[int, str]:
    """(the provider's cap on one reply in tokens, where it came from); (0, "") when nobody says.

    Order: `override` (the --max-reply-tokens flag) or EKG_LLM_MAX_REPLY_TOKENS, then what window_tokens()
    learned from the backend (OpenRouter's max_completion_tokens). Call it after window_tokens()."""
    if override:
        return int(override), "--max-reply-tokens"
    if (env := os.environ.get("EKG_LLM_MAX_REPLY_TOKENS", "").strip()).isdigit() and int(env) > 0:
        return int(env), "EKG_LLM_MAX_REPLY_TOKENS"
    return _REPLY_LIMIT


async def ask(prompt: str, name: str, schema: dict) -> dict:
    """One structured call. Returns the parsed JSON object, or {"error": ...} as query-master's client does; either
    carries the call's token usage under "_usage" when the provider reported it (not part of the answer)."""
    client = _get_client(model=MODEL)  # OpenRouter: the next key in the round robin
    box = {}
    token = _CALL_USAGE.set(box)
    try:
        res = await client.explain(prompt, model=MODEL, json_schema={"name": name, "strict": True, "schema": schema},
                                   priority="bulk")
    finally:
        _CALL_USAGE.reset(token)
    if isinstance(res, dict) and box:
        res = {**res, "_usage": box}
    return res
