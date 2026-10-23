import json
from types import SimpleNamespace

import pytest

from psygen import ai
from psygen.samples import Category
from psygen.samples.library import Sample


class FakeMessages:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        stop, text = reply
        content = [SimpleNamespace(type="thinking", thinking=""), SimpleNamespace(type="text", text=text)]
        return SimpleNamespace(stop_reason=stop, content=content)

    def stream(self, **kwargs):
        response = self.create(**kwargs)
        self.calls[-1]["_streamed"] = True

        class Stream:
            def __enter__(self):
                return SimpleNamespace(get_final_message=lambda: response)

            def __exit__(self, *exc):
                return False

        return Stream()


def fake_client(*replies):
    messages = FakeMessages(replies)
    return SimpleNamespace(beta=SimpleNamespace(messages=messages)), messages


def test_ask_json_request_shape():
    client, messages = fake_client(("end_turn", '{"ok": true}'))
    assert ai.ask_json("sys", "hi", {"type": "object"}, client=client) == {"ok": True}
    call = messages.calls[0]
    assert call["model"] == "claude-opus-5-5"
    assert call["output_config"]["format"] == {"type": "json_schema", "schema": {"type": "object"}}
    assert call["fallbacks"] == "default"
    assert call["betas"] == [ai.FALLBACK_BETA]


def test_ask_json_streams_long_answers():
    client, messages = fake_client(("end_turn", '{"a": 1}'), ("end_turn", '{"b": 2}'))
    assert ai.ask_json("sys", "hi", {}, client=client, max_tokens=64000) == {"a": 1}
    assert ai.ask_json("sys", "hi", {}, client=client) == {"b": 2}
    assert messages.calls[0].get("_streamed") and not messages.calls[1].get("_streamed")
    assert messages.calls[0]["fallbacks"] == "default"


@pytest.mark.parametrize("stop, text", [("refusal", ""), ("max_tokens", '{"a"'), ("end_turn", "not json")])
def test_ask_json_bad_answers(stop, text):
    client, _ = fake_client((stop, text))
    with pytest.raises(ai.ClaudeError):
        ai.ask_json("sys", "hi", {}, client=client)


def test_ask_json_connection_error():
    import anthropic
    import httpx2

    err = anthropic.APIConnectionError(request=httpx2.Request("POST", "https://api.anthropic.com"))
    client, _ = fake_client(err)
    with pytest.raises(ai.ClaudeError, match="internet"):
        ai.ask_json("sys", "hi", {}, client=client)


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
