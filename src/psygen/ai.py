"""Claude API access.

All calls go through :func:`ask_json`, which asks Claude for JSON that matches
a schema. Later steps (track planning, pattern writing) build on the same
helper. The ``anthropic`` package is imported lazily so the offline parts of
psygen keep working when it isn't installed or no API key is set.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterable

from .samples.categorize import Category

if TYPE_CHECKING:  # pragma: no cover
    from .samples.library import Sample

DEFAULT_MODEL = "claude-opus-5-5"
FALLBACK_BETA = "server-side-fallback-2026-07-01"
CLASSIFY_BATCH = 150


class ClaudeError(Exception):
    """Claude could not be reached or did not return a usable answer."""


def make_client():
    try:
        import anthropic
    except ImportError as exc:
        raise ClaudeError("the anthropic package is not installed (pip install anthropic)") from exc
    try:
        return anthropic.Anthropic()
    except anthropic.AnthropicError as exc:
        raise ClaudeError(f"no Claude API credentials: {exc}") from exc


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
    import anthropic

    client = client or make_client()
    try:
        response = client.beta.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": prompt}],
            output_config={
                "effort": effort,
                "format": {"type": "json_schema", "schema": schema},
            },
            # If a safety classifier declines, the API retries on its
            # recommended fallback model instead of returning the refusal.
            betas=[FALLBACK_BETA],
            fallbacks="default",
        )
    except anthropic.AuthenticationError as exc:
        raise ClaudeError("the Claude API key was rejected (check ANTHROPIC_API_KEY)") from exc
    except anthropic.RateLimitError as exc:
        raise ClaudeError("Claude API rate limit reached, try again shortly") from exc
    except anthropic.APIStatusError as exc:
        raise ClaudeError(f"Claude API error {exc.status_code}: {exc.message}") from exc
    except anthropic.APIConnectionError as exc:
        raise ClaudeError("could not reach the Claude API (no internet?)") from exc

    if response.stop_reason == "refusal":
        raise ClaudeError("Claude declined the request")
    if response.stop_reason == "max_tokens":
        raise ClaudeError("Claude's answer was cut off (max_tokens)")
    text = next((b.text for b in response.content if b.type == "text"), None)
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
