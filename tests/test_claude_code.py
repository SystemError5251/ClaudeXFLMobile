import json
import os
import sys
import textwrap

import pytest

from psygen import ai, config

pytestmark = pytest.mark.skipif(os.name != "posix", reason="fake claude is a shell script")

SCHEMA = {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}


def fake_claude(tmp_path, reply, old=False):
    """A stand-in `claude` command that records its arguments and stdin."""
    log = tmp_path / "calls.jsonl"
    script = tmp_path / "claude"
    script.write_text(textwrap.dedent(f"""\
        #!{sys.executable}
        import json, sys
        args, prompt = sys.argv[1:], sys.stdin.read()
        with open({str(log)!r}, "a") as fh:
            fh.write(json.dumps({{"args": args, "prompt": prompt}}) + "\\n")
        if {old!r} and "--json-schema" in args:
            sys.stderr.write("error: unknown option '--json-schema'\\n")
            sys.exit(1)
        print(json.dumps({reply!r}))
        """))
    script.chmod(0o755)

    def calls():
        return [json.loads(line) for line in log.read_text().splitlines()]

    return str(script), calls


def test_cli_client_structured_output(tmp_path):
    cmd, calls = fake_claude(tmp_path, {"type": "result", "is_error": False, "result": "x",
                                        "structured_output": {"name": "Neon Mandala"}})
    client = ai.CliClient(cmd, cwd=tmp_path)
    answer = ai.ask_json("be a producer", "name a track", SCHEMA, client=client,
                         model="claude-opus-5-5", effort="high", max_tokens=64000)
    assert answer == {"name": "Neon Mandala"}
    args = calls()[0]["args"]
    assert args[:2] == ["-p", "--output-format"] and "--no-session-persistence" in args
    assert args[args.index("--tools") + 1] == ""
    assert args[args.index("--model") + 1] == "claude-opus-5-5"
    assert args[args.index("--system-prompt") + 1] == "be a producer"
    assert json.loads(args[args.index("--json-schema") + 1]) == SCHEMA
    assert args[args.index("--effort") + 1] == "high"
    assert calls()[0]["prompt"] == "name a track"


def test_cli_client_older_claude_code(tmp_path):
    reply = {"type": "result", "is_error": False, "result": '```json\n{"name": "Goa Gate"}\n```'}
    cmd, calls = fake_claude(tmp_path, reply, old=True)
    answer = ai.ask_json("sys", "name a track", SCHEMA, client=ai.CliClient(cmd, cwd=tmp_path))
    assert answer == {"name": "Goa Gate"}
    retry = calls()[1]
    assert "--json-schema" not in retry["args"] and "--effort" not in retry["args"]
    assert '"required": ["name"]' in retry["prompt"]


@pytest.mark.parametrize("result, match", [
    ("Invalid API key · Please run /login", "isn't logged in"),
    ("Credit balance too low", "Claude Code: Credit"),
])
def test_cli_client_errors(tmp_path, result, match):
    cmd, _ = fake_claude(tmp_path, {"type": "result", "is_error": True, "result": result})
    with pytest.raises(ai.ClaudeError, match=match):
        ai.ask_json("sys", "hi", SCHEMA, client=ai.CliClient(cmd, cwd=tmp_path))


def test_cli_client_garbage_and_missing_command(tmp_path):
    script = tmp_path / "claude"
    script.write_text("#!/bin/sh\necho boom >&2\nexit 3\n")
    script.chmod(0o755)
    with pytest.raises(ai.ClaudeError, match="Claude Code failed: boom"):
        ai.CliClient(str(script), cwd=tmp_path).send({"messages": []})
    with pytest.raises(ai.ClaudeError, match="could not start"):
        ai.CliClient(str(tmp_path / "nope"), cwd=tmp_path).send({"messages": []})


class Primary:
    def __init__(self, *errors):
        self.errors = list(errors)
        self.calls = 0

    def send(self, body, *, stream=False, betas=()):
        self.calls += 1
        if self.errors:
            raise self.errors.pop(0)
        return {"stop_reason": "end_turn", "content": [{"type": "text", "text": '{"name": "api"}'}]}


class Backup:
    calls = 0

    def send(self, body, *, stream=False, betas=()):
        self.calls += 1
        return {"stop_reason": "end_turn", "content": [{"type": "text", "text": '{"name": "cli"}'}]}


def test_fallback_on_no_credits_only():
    notes = []
    no_credits = ai.ClaudeError(ai._error_message(400, json.dumps(
        {"error": {"message": "Your credit balance is too low to access the Anthropic API."}}).encode()), status=400)
    assert "no credits left" in str(no_credits) and no_credits.api_unusable
    primary, backup = Primary(no_credits), Backup()
    client = ai.FallbackClient(primary, backup, notes.append)
    assert ai.ask_json("s", "p", SCHEMA, client=client) == {"name": "cli"}
    assert ai.ask_json("s", "p", SCHEMA, client=client) == {"name": "cli"}
    assert primary.calls == 1 and backup.calls == 2  # no second try on the API
    assert "using Claude Code instead" in notes[0]

    busy = ai.ClaudeError("overloaded", status=529)
    assert not busy.api_unusable
    with pytest.raises(ai.ClaudeError, match="overloaded"):
        ai.FallbackClient(Primary(busy), Backup()).send({})
    assert ai.ClaudeError("bad key", status=401).api_unusable


def test_make_client_picks_the_way_in(monkeypatch, isolated_home):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    cfg = config.Config()
    with pytest.raises(ai.ClaudeError, match="no way to reach Claude"):
        ai.make_client(cfg=cfg)
    assert not ai.can_reach_claude(cfg)

    monkeypatch.setattr(ai, "claude_code_path", lambda cfg=None: "/usr/bin/claude")
    assert isinstance(ai.make_client(cfg=cfg), ai.CliClient) and ai.can_reach_claude(cfg)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    assert isinstance(ai.make_client(cfg=cfg), ai.FallbackClient)
    assert isinstance(ai.make_client(cfg=config.Config(claude_backend="api")), ai.HttpClient)
    assert isinstance(ai.make_client(cfg=config.Config(claude_backend="claude-code")), ai.CliClient)

    monkeypatch.setattr(ai, "claude_code_path", lambda cfg=None: None)
    assert isinstance(ai.make_client(cfg=cfg), ai.HttpClient)
    with pytest.raises(ai.ClaudeError, match="not installed"):
        ai.make_client(cfg=config.Config(claude_backend="claude-code"))


def test_ui_works_with_claude_code_only(tmp_path, isolated_home, monkeypatch):
    from test_ui import make_app, text_of, wait_job
    from psygen.ui import Settings

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    from test_composer import ANSWER

    cmd, calls = fake_claude(tmp_path, {"type": "result", "is_error": False, "result": "",
                                        "structured_output": ANSWER})
    monkeypatch.setattr(ai, "claude_code_path", lambda cfg=None: cmd)
    app = make_app(tmp_path)
    assert "API key: none, using Claude Code" in text_of(app)
    app.generate(Settings(output="midi"))
    run = wait_job(app)
    assert run.job.state == "done", run.job.error
    assert "Claude Code does the composing" in text_of(app) and "Forest Gate" in text_of(app)
    assert len(calls()) == 1
