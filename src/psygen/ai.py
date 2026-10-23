"""Claude API access.

All calls go through :func:`ask_json`, which asks Claude for JSON that matches
a schema. Requests go straight to the Messages API over HTTPS with the
standard library: the official ``anthropic`` SDK needs Rust-built wheels
(pydantic-core, jiter) that don't build on Termux, so psygen doesn't use it.
The offline parts of psygen keep working without an API key.
"""

from __future__ import annotations

import json
import os
import random
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterable

from . import __version__
from .samples.categorize import Category

if TYPE_CHECKING:  # pragma: no cover
    from .samples.library import Sample

DEFAULT_MODEL = "claude-opus-5-5"
API_URL = "https://api.anthropic.com/v1/messages"
API_VERSION = "2026-10-21"
FALLBACK_BETA = "server-side-fallback-2026-07-01"
CLASSIFY_BATCH = 150
STREAM_ABOVE = 16000
TIMEOUT_SECONDS = 600
RETRIES = 2  # for rate limits, overloads, server and connection errors
RETRY_STATUS = frozenset({408, 409, 429, 500, 502, 503, 504, 529})


class ClaudeError(Exception):
    """Claude could not be reached or did not return a usable answer."""


def _error_message(status: int, body: bytes) -> str:
    try:
        detail = json.loads(body.decode("utf-8", "replace"))["error"]["message"]
    except (ValueError, KeyError, TypeError):
        detail = body.decode("utf-8", "replace").strip()[:200] or "no details"
    if status == 401:
        return "the Claude API key was rejected (check ANTHROPIC_API_KEY)"
    if status == 429:
        return "Claude API rate limit reached, try again shortly"
    if status == 529:
        return "the Claude API is overloaded, try again shortly"
    return f"Claude API error {status}: {detail}"


def read_sse(lines: Iterable[bytes]) -> dict:
    """Assemble a streamed Messages response into the shape a non-streaming
    request returns (only the fields psygen reads: content and stop_reason)."""
    blocks: dict[int, dict] = {}
    message: dict[str, Any] = {"content": [], "stop_reason": None}
    for raw in lines:
        line = raw.decode("utf-8", "replace").rstrip("\r\n")
        if not line.startswith("data:"):
            continue
        try:
            event = json.loads(line[5:].strip())
        except ValueError:
            continue
        kind = event.get("type")
        if kind == "message_start":
            message.update({k: v for k, v in event.get("message", {}).items() if k != "content"})
        elif kind == "content_block_start":
            blocks[event["index"]] = dict(event.get("content_block", {}))
        elif kind == "content_block_delta":
            delta = event.get("delta", {})
            if delta.get("type") == "text_delta":
                block = blocks.setdefault(event["index"], {"type": "text", "text": ""})
                block["text"] = block.get("text", "") + delta.get("text", "")
        elif kind == "message_delta":
            message.update(event.get("delta", {}))
        elif kind == "error":
            err = event.get("error", {})
            raise ClaudeError(f"Claude API error during streaming: {err.get('message', err.get('type', '?'))}")
    message["content"] = [blocks[i] for i in sorted(blocks)]
    return message


class HttpClient:
    """Minimal Messages API client on urllib."""

    def __init__(self, api_key: str | None = None, auth_token: str | None = None,
                 url: str | None = None, timeout: float = TIMEOUT_SECONDS, retries: int = RETRIES):
        self.api_key = api_key
        self.auth_token = auth_token
        base = os.environ.get("ANTHROPIC_BASE_URL")
        self.url = url or (base.rstrip("/") + "/v1/messages" if base else API_URL)
        self.timeout = timeout
        self.retries = retries

    def _headers(self, betas: list[str]) -> dict[str, str]:
        headers = {
            "content-type": "application/json",
            "anthropic-version": API_VERSION,
            "user-agent": f"psygen/{__version__}",
        }
        if self.api_key:
            headers["x-api-key"] = self.api_key
        else:
            headers["authorization"] = f"Bearer {self.auth_token}"
            betas = [*betas, "oauth-2025-04-20"]
        if betas:
            headers["anthropic-beta"] = ",".join(betas)
        return headers

    def send(self, body: dict, *, stream: bool = False, betas: Iterable[str] = ()) -> dict:
        payload = dict(body, stream=True) if stream else body
        data = json.dumps(payload).encode("utf-8")
        headers = self._headers(list(betas))
        for attempt in range(self.retries + 1):
            request = urllib.request.Request(self.url, data=data, headers=headers, method="POST")
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    if stream:
                        return read_sse(response)
                    return json.loads(response.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                message = _error_message(exc.code, exc.read())
                if exc.code not in RETRY_STATUS or attempt == self.retries:
                    raise ClaudeError(message) from exc
                wait = exc.headers.get("retry-after") if exc.headers else None
            except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
                if attempt == self.retries:
                    raise ClaudeError("could not reach the Claude API (no internet?)") from exc
                wait = None
            except ValueError as exc:
                raise ClaudeError("the Claude API sent an unreadable response") from exc
            try:
                delay = float(wait) if wait else 0.0
            except ValueError:
                delay = 0.0
            time.sleep(min(60.0, delay or (2 ** attempt + random.random())))
        raise ClaudeError("could not reach the Claude API")  # pragma: no cover


def make_client() -> HttpClient:
    key = os.environ.get("ANTHROPIC_API_KEY")
    token = os.environ.get("ANTHROPIC_AUTH_TOKEN")
    if not key and not token:
        raise ClaudeError("no Claude API key: set ANTHROPIC_API_KEY (see README)")
    return HttpClient(api_key=key, auth_token=token)


def ask_json(
    system: str,
    prompt: str,
    schema: dict,
    *,
    client=None,
    model: str = DEFAULT_MODEL,
    effort: str = "medium",
    max_tokens: int = 16000,
) -> Any:
    """Send one request and return the parsed JSON answer."""
    client = client or make_client()
    body = {
        "model": model,
        "max_tokens": max_tokens,
        "system": system,
        "messages": [{"role": "user", "content": prompt}],
        "output_config": {
            "effort": effort,
            "format": {"type": "json_schema", "schema": schema},
        },
        # If a safety classifier declines, the API retries on its
        # recommended fallback model instead of returning the refusal.
        "fallbacks": "default",
    }
    # Long answers (whole tracks) stream so the connection never sits idle
    # long enough for a phone network or proxy to drop it.
    response = client.send(body, stream=max_tokens > STREAM_ABOVE, betas=[FALLBACK_BETA])

    stop = response.get("stop_reason")
    if stop == "refusal":
        raise ClaudeError("Claude declined the request")
    if stop == "max_tokens":
        raise ClaudeError("Claude's answer was cut off (max_tokens)")
    text = next((b.get("text") for b in response.get("content", []) if b.get("type") == "text"), None)
    if text is None:
        raise ClaudeError("Claude returned no text")
    try:
        return json.loads(text)
    except ValueError as exc:
        raise ClaudeError("Claude returned invalid JSON") from exc


_CLASSIFY_SYSTEM = (
    "You sort audio samples for a psytrance, goa and progressive trance producer. "
    "For each sample you get its path (last folders and filename) and, when "
    "available, a few measured audio features. Pick the best category and say "
    "whether it is a loop (a rhythmic or musical phrase meant to repeat) or a "
    "one-shot. Categories: " + ", ".join(c.value for c in Category) + ". "
    "Use 'drums' for mixed drum loops and kits, 'atmos' for textures and "
    "drones, 'fx' for risers, impacts, sweeps and noise. Use 'unknown' only "
    "when nothing points anywhere."
)

_CLASSIFY_SCHEMA = {
    "type": "object",
    "properties": {
        "results": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "integer"},
                    "category": {"type": "string", "enum": [c.value for c in Category]},
                    "is_loop": {"type": "boolean"},
                },
                "required": ["id", "category", "is_loop"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["results"],
    "additionalProperties": False,
}

_FEATURE_KEYS = ("duration", "centroid_hz", "low_ratio", "high_ratio", "decay_s", "onsets", "pitch_hz")


def _describe(index: int, sample: "Sample") -> dict:
    parts = Path(sample.path).parts[-3:]
    entry: dict[str, Any] = {"id": index, "path": "/".join(parts)}
    if sample.features:
        entry["features"] = {k: sample.features.get(k) for k in _FEATURE_KEYS}
    elif sample.duration is not None:
        entry["features"] = {"duration": round(sample.duration, 3)}
    return entry


def classify_samples(
    samples: Iterable["Sample"],
    *,
    client=None,
    model: str = DEFAULT_MODEL,
    batch_size: int = CLASSIFY_BATCH,
) -> int:
    """Let Claude categorize samples in place. Returns how many changed."""
    pending = list(samples)
    if not pending:
        return 0
    client = client or make_client()
    changed = 0
    for start in range(0, len(pending), batch_size):
        batch = pending[start:start + batch_size]
        listing = [_describe(i, s) for i, s in enumerate(batch)]
        answer = ask_json(
            _CLASSIFY_SYSTEM,
            "Categorize these samples:\n" + json.dumps(listing, separators=(",", ":")),
            _CLASSIFY_SCHEMA,
            client=client,
            model=model,
            effort="low",
        )
        for item in answer.get("results", []):
            i = item.get("id")
            if not isinstance(i, int) or not 0 <= i < len(batch):
                continue
            try:
                category = Category(item.get("category"))
            except ValueError:
                continue
            if category is Category.UNKNOWN:
                continue
            sample = batch[i]
            sample.category = category
            sample.is_loop = bool(item.get("is_loop", sample.is_loop))
            sample.confidence = 0.6
            sample.source = "claude"
            changed += 1
    return changed
