"""Ask query-master's graph agent over HTTP, the way the web app does, and read its event stream.

    stream_ask("http://127.0.0.1:7111", question, as_of=..., asker=..., service_token=...)   # internal route
    stream_ask("http://127.0.0.1:7111", question, user_token=...)                            # as a signed-in user

With a service token it calls POST /api/v1/ekg/internal/query/ask/stream (the asker is named per question);
with a user token, POST /api/v1/ekg/search/stream (the user asks as themselves). Either way the events are
the graph search's: routing, tool_start, tool_done, synthesizing, done, error. Returns the same shape as
app.ekg.query.ask() so the evaluation grades both paths alike.
"""
from __future__ import annotations

import json
from typing import Callable, Optional

import httpx


async def stream_ask(base: str, question: str, as_of: Optional[str] = None, asker: str = "public",
                     service_token: Optional[str] = None, user_token: Optional[str] = None,
                     max_steps: int = 12, on_event: Optional[Callable[[dict], None]] = None,
                     timeout: float = 600) -> dict:
    if user_token:
        url = f"{base.rstrip('/')}/api/v1/ekg/search/stream"
        headers = {"Authorization": f"Bearer {user_token}"}
        body = {"query": question, "as_of": as_of, "max_steps": max_steps}
    elif service_token:
        url = f"{base.rstrip('/')}/api/v1/ekg/internal/query/ask/stream"
        headers = {"X-Internal-Token": service_token}
        body = {"question": question, "asker": asker, "as_of": as_of, "max_steps": max_steps}
    else:
        raise ValueError("a service token (QUERY_MASTER_SERVICE_TOKEN) or a user token is needed")
    done, error = None, None
    async with httpx.AsyncClient(timeout=timeout) as client:
        async with client.stream("POST", url, headers=headers, json=body) as r:
            if r.status_code != 200:
                text = (await r.aread()).decode("utf-8", "replace")[:300]
                raise RuntimeError(f"HTTP {r.status_code} from {url}: {text}")
            async for line in r.aiter_lines():
                if not line.startswith("data: "):
                    continue
                ev = json.loads(line[6:])
                if on_event:
                    on_event(ev)
                if ev.get("type") == "done":
                    done = ev
                elif ev.get("type") == "error":
                    error = ev.get("error")
    if done is None:
        raise RuntimeError(error or "the stream ended without an answer")
    return {"question": question, "answer": done.get("answer") or "", "asker": done.get("asker"),
            "steps": done.get("steps"), "tool_calls": done.get("tool_calls") or [], "usage": done.get("usage") or {},
            "stopped": done.get("stopped"), "model": done.get("model"), "via": url}
