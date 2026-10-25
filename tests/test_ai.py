import io
import json
import urllib.error

import pytest

from psygen import ai
from psygen.samples import Category
from psygen.samples.library import Sample


class FakeClient:
    """Stands in for ai.HttpClient: records request bodies, returns canned replies."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def send(self, body, *, stream=False, betas=()):
        self.calls.append(dict(body, _streamed=stream, _betas=list(betas)))
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        stop, text = reply
        return {"stop_reason": stop, "content": [{"type": "thinking", "thinking": ""}, {"type": "text", "text": text}]}


def fake_client(*replies):
    client = FakeClient(replies)
    return client, client


def test_ask_json_request_shape():
    client, messages = fake_client(("end_turn", '{"ok": true}'))
    assert ai.ask_json("sys", "hi", {"type": "object"}, client=client) == {"ok": True}
    call = messages.calls[0]
    assert call["model"] == "claude-opus-5-5"
    assert call["output_config"]["format"] == {"type": "json_schema", "schema": {"type": "object"}}
    assert call["fallbacks"] == "default"
    assert call["_betas"] == [ai.FALLBACK_BETA]
    assert "stream" not in call


def test_ask_json_streams_long_answers():
    client, messages = fake_client(("end_turn", '{"a": 1}'), ("end_turn", '{"b": 2}'))
    assert ai.ask_json("sys", "hi", {}, client=client, max_tokens=64000) == {"a": 1}
    assert ai.ask_json("sys", "hi", {}, client=client) == {"b": 2}
    assert messages.calls[0]["_streamed"] and not messages.calls[1]["_streamed"]


@pytest.mark.parametrize("stop, text", [("refusal", ""), ("max_tokens", '{"a"'), ("end_turn", "not json")])
def test_ask_json_bad_answers(stop, text):
    client, _ = fake_client((stop, text))
    with pytest.raises(ai.ClaudeError):
        ai.ask_json("sys", "hi", {}, client=client)


def test_make_client_needs_a_key(monkeypatch, isolated_home):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    with pytest.raises(ai.ClaudeError, match="ANTHROPIC_API_KEY"):
        ai.make_client()
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    assert ai.make_client().api_key == "sk-test"


# -- the urllib client ---------------------------------------------------------

SSE = b"""event: message_start
data: {"type":"message_start","message":{"id":"msg_1","model":"claude-opus-5-5","content":[],"stop_reason":null}}

event: content_block_start
data: {"type":"content_block_start","index":0,"content_block":{"type":"thinking","thinking":""}}

event: content_block_start
data: {"type":"content_block_start","index":1,"content_block":{"type":"text","text":""}}

event: ping
data: {"type":"ping"}

event: content_block_delta
data: {"type":"content_block_delta","index":1,"delta":{"type":"text_delta","text":"{\\"a\\": "}}

event: content_block_delta
data: {"type":"content_block_delta","index":1,"delta":{"type":"text_delta","text":"1}"}}

event: message_delta
data: {"type":"message_delta","delta":{"stop_reason":"end_turn"},"usage":{"output_tokens":5}}

event: message_stop
data: {"type":"message_stop"}
"""


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def fake_urlopen(monkeypatch, *replies):
    seen = []

    def urlopen(request, timeout=None):
        seen.append(request)
        reply = replies[len(seen) - 1]
        if isinstance(reply, Exception):
            raise reply
        return FakeResponse(reply)

    monkeypatch.setattr(ai.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(ai.time, "sleep", lambda s: None)
    return seen


def test_read_sse_assembles_the_message():
    msg = ai.read_sse(io.BytesIO(SSE))
    assert msg["stop_reason"] == "end_turn" and msg["model"] == "claude-opus-5-5"
    assert [b["type"] for b in msg["content"]] == ["thinking", "text"]
    assert json.loads(msg["content"][1]["text"]) == {"a": 1}


def test_read_sse_error_event():
    data = b'event: error\ndata: {"type":"error","error":{"type":"overloaded_error","message":"Overloaded"}}\n'
    with pytest.raises(ai.ClaudeError, match="Overloaded"):
        ai.read_sse(io.BytesIO(data))


def test_http_client_streams_with_headers(monkeypatch):
    seen = fake_urlopen(monkeypatch, SSE)
    client = ai.HttpClient(api_key="sk-test")
    assert ai.ask_json("sys", "hi", {}, client=client, max_tokens=64000) == {"a": 1}
    request = seen[0]
    assert request.full_url == ai.API_URL
    headers = {k.lower(): v for k, v in request.header_items()}
    assert headers["x-api-key"] == "sk-test"
    assert headers["anthropic-version"] == ai.API_VERSION
    assert headers["anthropic-beta"] == ai.FALLBACK_BETA
    body = json.loads(request.data)
    assert body["stream"] is True and body["fallbacks"] == "default"


def test_http_client_plain_request_and_auth_token(monkeypatch):
    reply = json.dumps({"stop_reason": "end_turn", "content": [{"type": "text", "text": '{"b": 2}'}]}).encode()
    seen = fake_urlopen(monkeypatch, reply)
    client = ai.HttpClient(auth_token="tok")
    assert ai.ask_json("sys", "hi", {}, client=client) == {"b": 2}
    headers = {k.lower(): v for k, v in seen[0].header_items()}
    assert headers["authorization"] == "Bearer tok" and "x-api-key" not in headers
    assert "oauth-2025-04-20" in headers["anthropic-beta"]
    assert "stream" not in json.loads(seen[0].data)


def _http_error(code, message="boom"):
    body = json.dumps({"type": "error", "error": {"type": "x", "message": message}}).encode()
    return urllib.error.HTTPError(ai.API_URL, code, message, {}, io.BytesIO(body))


def test_http_client_retries_then_succeeds(monkeypatch):
    ok = json.dumps({"stop_reason": "end_turn", "content": [{"type": "text", "text": "{}"}]}).encode()
    seen = fake_urlopen(monkeypatch, _http_error(529), urllib.error.URLError("reset"), ok)
    assert ai.HttpClient(api_key="k").send({"model": "m"}) == {"stop_reason": "end_turn", "content": [{"type": "text", "text": "{}"}]}
    assert len(seen) == 3


@pytest.mark.parametrize("error, match", [
    (_http_error(401), "key was rejected"),
    (_http_error(400, "max_tokens too large"), "400: max_tokens too large"),
])
def test_http_client_errors_are_not_retried(monkeypatch, error, match):
    seen = fake_urlopen(monkeypatch, error, error)
    with pytest.raises(ai.ClaudeError, match=match):
        ai.HttpClient(api_key="k").send({})
    assert len(seen) == 1


def test_http_client_gives_up_offline(monkeypatch):
    err = urllib.error.URLError("no route")
    seen = fake_urlopen(monkeypatch, err, err, err)
    with pytest.raises(ai.ClaudeError, match="internet"):
        ai.HttpClient(api_key="k").send({})
    assert len(seen) == ai.RETRIES + 1


def _sample(path, **kw):
    return Sample(path=path, category=Category.UNKNOWN, confidence=0.0, source="none", **kw)


def test_classify_samples_updates_in_batches():
    samples = [_sample("/s/a/track 07.wav", features={"duration": 0.4, "centroid_hz": 90.0}), _sample("/s/b/thing.wav"), _sample("/s/b/x.wav")]
    first = {"results": [{"id": 0, "category": "kick", "is_loop": False}, {"id": 1, "category": "pad", "is_loop": True}]}
    second = {"results": [{"id": 0, "category": "unknown", "is_loop": False}, {"id": 9, "category": "kick", "is_loop": False}]}
    client, messages = fake_client(("end_turn", json.dumps(first)), ("end_turn", json.dumps(second)))

    changed = ai.classify_samples(samples, client=client, batch_size=2)

    assert changed == 2
    assert samples[0].category is Category.KICK and samples[0].source == "claude"
    assert samples[1].category is Category.PAD and samples[1].is_loop
    assert samples[2].category is Category.UNKNOWN
    assert len(messages.calls) == 2
    sent = messages.calls[0]["messages"][0]["content"]
    assert "a/track 07.wav" in sent and "centroid_hz" in sent


def test_classify_nothing_makes_no_call():
    assert ai.classify_samples([], client=object()) == 0


def test_cli_scan_ai(monkeypatch, tmp_path, isolated_home, capsys):
    from psygen import cli
    from conftest import kick, write_wav

    write_wav(tmp_path / "S" / "Kicks" / "k1.wav", kick())
    write_wav(tmp_path / "S" / "mystery.wav", kick())
    seen = []

    def fake_classify(samples, model):
        seen.extend(s.name for s in samples)
        for s in samples:
            s.category, s.source = Category.KICK, "claude"
        return len(samples)

    monkeypatch.setattr(ai, "classify_samples", fake_classify)
    assert cli.main(["scan", str(tmp_path / "S"), "--ai"]) == 0
    assert seen == ["mystery.wav"]
    assert "Claude categorized 1 samples" in capsys.readouterr().out


def test_cli_scan_ai_failure_is_not_fatal(monkeypatch, tmp_path, isolated_home, capsys):
    from psygen import cli
    from conftest import kick, write_wav

    write_wav(tmp_path / "S" / "mystery.wav", kick())

    def boom(samples, model):
        raise ai.ClaudeError("no key")

    monkeypatch.setattr(ai, "classify_samples", boom)
    assert cli.main(["scan", str(tmp_path / "S"), "--ai"]) == 0
    assert "no key" in capsys.readouterr().err
