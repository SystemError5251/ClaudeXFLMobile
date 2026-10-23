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
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterable

from . import __version__, config
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

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status

    @property
    def api_unusable(self) -> bool:
        """The API itself refuses (no credits, bad key): another way to reach
        Claude, such as Claude Code, may still work."""
        text = str(self).lower()
        return self.status in (401, 402, 403) or (self.status == 400 and "credit" in text)


def _error_message(status: int, body: bytes) -> str:
    try:
        detail = json.loads(body.decode("utf-8", "replace"))["error"]["message"]
    except (ValueError, KeyError, TypeError):
        detail = body.decode("utf-8", "replace").strip()[:200] or "no details"
    if status == 401:
        return "the Claude API key was rejected (check ANTHROPIC_API_KEY)"
    if status in (400, 402) and "credit" in detail.lower():
        return "the Claude API account has no credits left (top up at platform.claude.com)"
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
        self.models_url = self.url.rsplit("/", 1)[0] + "/models"
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
                    raise ClaudeError(message, status=exc.code) from exc
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


    def check(self) -> None:
        """Make a free request (list one model) to see whether the key works."""
        request = urllib.request.Request(
            self.models_url + "?limit=1", headers=self._headers([]), method="GET")
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                response.read()
        except urllib.error.HTTPError as exc:
            raise ClaudeError(_error_message(exc.code, exc.read())) from exc
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            raise ClaudeError("could not reach the Claude API (no internet?)") from exc


def key_source() -> str | None:
    """Where the API credentials come from: "env", "saved" or None."""
    if os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"):
        return "env"
    return "saved" if config.load_api_key() else None


def api_client() -> HttpClient:
    """Credentials from ANTHROPIC_API_KEY / ANTHROPIC_AUTH_TOKEN, else the key
    saved in the terminal UI."""
    key = os.environ.get("ANTHROPIC_API_KEY")
    token = os.environ.get("ANTHROPIC_AUTH_TOKEN")
    if not key and not token:
        key = config.load_api_key()
    if not key and not token:
        raise ClaudeError("no Claude API key: set ANTHROPIC_API_KEY or save one in `psygen ui` (see README)")
    return HttpClient(api_key=key, auth_token=token)


# -- Claude Code as a second way in -------------------------------------------


def claude_code_path(cfg: "config.Config | None" = None) -> str | None:
    """The Claude Code command (``claude``) if it is installed."""
    cfg = cfg or config.Config.load()
    return shutil.which(cfg.claude_cli or "claude")


class CliClient:
    """Runs one request through Claude Code (``claude -p``), which uses the
    account Claude Code is logged in with instead of API credits. Same
    ``send`` interface as :class:`HttpClient`."""

    def __init__(self, command: str, timeout: float = 1200, cwd: Path | None = None):
        self.command = command
        self.timeout = timeout
        self.cwd = cwd

    def _args(self, body: dict, extras: bool) -> list[str]:
        args = [self.command, "-p", "--output-format", "json", "--no-session-persistence",
                "--tools", "", "--model", body.get("model", DEFAULT_MODEL)]
        if body.get("system"):
            args += ["--system-prompt", body["system"]]
        output = body.get("output_config", {})
        if extras:  # options older Claude Code builds may not know
            schema = output.get("format", {}).get("schema")
            if schema is not None:
                args += ["--json-schema", json.dumps(schema)]
            if output.get("effort"):
                args += ["--effort", output["effort"]]
        return args

    def _prompt(self, body: dict, extras: bool) -> str:
        prompt = "\n\n".join(m["content"] for m in body.get("messages", []) if m.get("role") == "user")
        if not extras:
            schema = body.get("output_config", {}).get("format", {}).get("schema")
            prompt += ("\n\nAnswer with one JSON object only, no other text, matching this JSON schema:\n"
                       + json.dumps(schema))
        return prompt

    def _run(self, body: dict, extras: bool) -> subprocess.CompletedProcess:
        cwd = self.cwd or config.cache_dir()
        cwd.mkdir(parents=True, exist_ok=True)
        try:
            return subprocess.run(
                self._args(body, extras), input=self._prompt(body, extras),
                capture_output=True, text=True, timeout=self.timeout, cwd=str(cwd),
            )
        except subprocess.TimeoutExpired as exc:
            raise ClaudeError("Claude Code took too long to answer") from exc
        except OSError as exc:
            raise ClaudeError(f"could not start Claude Code ({self.command}): {exc}") from exc

    def send(self, body: dict, *, stream: bool = False, betas: Iterable[str] = ()) -> dict:
        proc = self._run(body, extras=True)
        if proc.returncode != 0 and "unknown option" in (proc.stderr or "").lower():
            proc = self._run(body, extras=False)  # an older Claude Code
        try:
            reply = json.loads(proc.stdout)
        except ValueError:
            detail = (proc.stderr or proc.stdout).strip().splitlines()
            raise ClaudeError(f"Claude Code failed: {detail[-1] if detail else 'no output'}") from None
        if reply.get("is_error"):
            text = str(reply.get("result") or reply.get("subtype") or "unknown error")
            if "login" in text.lower() or "api key" in text.lower():
                raise ClaudeError("Claude Code isn't logged in: run `claude` once and log in")
            raise ClaudeError(f"Claude Code: {text[:200]}")
        answer = reply.get("structured_output")
        text = json.dumps(answer) if answer is not None else _strip_fences(str(reply.get("result", "")))
        return {"stop_reason": "end_turn", "content": [{"type": "text", "text": text}]}


def _strip_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else ""
        text = text.rsplit("```", 1)[0]
    return text.strip()


class FallbackClient:
    """The API first; Claude Code when the API refuses (no credits, bad key)."""

    def __init__(self, primary: HttpClient, fallback: CliClient, notice=None):
        self.primary = primary
        self.fallback = fallback
        self.notice = notice or (lambda _msg: None)
        self.use_fallback = False

    def send(self, body: dict, *, stream: bool = False, betas: Iterable[str] = ()) -> dict:
        if not self.use_fallback:
            try:
                return self.primary.send(body, stream=stream, betas=betas)
            except ClaudeError as exc:
                if not exc.api_unusable:
                    raise
                self.notice(f"{exc}; using Claude Code instead")
                self.use_fallback = True
        return self.fallback.send(body, stream=stream, betas=betas)


def make_client(notice=None, cfg: "config.Config | None" = None):
    """The way to reach Claude, by config ``claude_backend``:

    - ``auto`` (default): the API with your key, falling back to Claude Code
      when the API has no credits or rejects the key; Claude Code alone when
      there is no key.
    - ``api``: only the API. ``claude-code``: only Claude Code.
    """
    cfg = cfg or config.Config.load()
    backend = cfg.claude_backend
    cli = claude_code_path(cfg)
    if backend == "api":
        return api_client()
    if backend in ("claude-code", "cli"):
        if not cli:
            raise ClaudeError("Claude Code is not installed (see README: npm install -g @anthropic-ai/claude-code)")
        return CliClient(cli)
    if key_source():
        return FallbackClient(api_client(), CliClient(cli), notice) if cli else api_client()
    if cli:
        return CliClient(cli)
    raise ClaudeError(
        "no way to reach Claude: save an API key in `psygen ui` (or set ANTHROPIC_API_KEY), "
        "or install and log in to Claude Code (see README)"
    )


def can_reach_claude(cfg: "config.Config | None" = None) -> bool:
    cfg = cfg or config.Config.load()
    if cfg.claude_backend == "api":
        return key_source() is not None
    if cfg.claude_backend in ("claude-code", "cli"):
        return claude_code_path(cfg) is not None
    return key_source() is not None or claude_code_path(cfg) is not None


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
