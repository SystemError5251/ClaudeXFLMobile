import json
import random

import pytest

from psygen import ai
from psygen.compose import ComposeRequest, Role, SectionKind, Track, compose, composer
from psygen.compose.theory import scale_pitch_classes
from psygen.samples import Category, SampleLibrary
from psygen.samples.library import Sample
from test_ai import fake_client


def lib():
    def s(path, cat, loop=False, conf=0.9):
        return Sample(path=path, category=cat, confidence=conf, source="name", is_loop=loop)

    return SampleLibrary([
        s("/s/kick1.wav", Category.KICK),
        s("/s/kick2.wav", Category.KICK, conf=0.5),
        s("/s/kickloop.wav", Category.KICK, loop=True),
        s("/s/bass.wav", Category.BASS),
        s("/s/hat.wav", Category.HAT_CLOSED),
        s("/s/riser.wav", Category.FX, loop=True),
        s("/s/lead.wav", Category.SYNTH),
    ])


ANSWER = {
    "title": "Forest Gate",
    "idea": "Dark rolling night forest.",
    "bpm": 147,
    "key": "F#",
    "scale": "phrygian",
    "parts": [
        {"role": "kick", "sample": 1},
        {"role": "bass", "sample": 1},  # id 1 is a kick: must not be used for bass
        {"role": "lead", "sample": -1},
        {"role": "banjo", "sample": 3},
    ],
    "patterns": [
        {"id": "kick_a", "role": "kick", "bars": 1, "notes": [[0, 0, 1, 120], [4, 0, 1, 120], [8, 0, 1, 120], [12, 0, 1, 120]]},
        {"id": "bass_a", "role": "bass", "bars": 1, "notes": [[s, 0, 1, 90] for s in range(16) if s % 4]},
        {"id": "lead_a", "role": "lead", "bars": 2, "notes": [[0, 4, 4, 100], [8, 1, 4, 100], [16, 0, 8, 100], "junk", [1]]},
        {"id": "x", "role": "banjo", "bars": 1, "notes": []},
    ],
    "sections": [
        {"name": "Intro", "kind": "intro", "bars": 32, "energy": 3, "patterns": ["kick_a"]},
        {"name": "Drop", "kind": "drop", "bars": 96, "energy": 9, "patterns": ["kick_a", "bass_a", "lead_a"]},
        {"name": "Melt", "kind": "breakdown", "bars": 32, "energy": 4, "patterns": ["lead_a"]},
        {"name": "Drop 2", "kind": "drop", "bars": 96, "energy": 10, "patterns": ["kick_a", "bass_a", "lead_a"]},
        {"name": "Outro", "kind": "outro", "bars": 32, "energy": 2, "patterns": ["kick_a", "bass_a"]},
        {"name": "?", "kind": "chorus", "bars": 8, "energy": 1, "patterns": []},
    ],
}


def test_shortlist_prefers_confident_one_shots():
    short = composer.shortlist_samples(lib(), random.Random(0))
    by_role = {}
    for i, (role, sample) in short.items():
        by_role.setdefault(role, []).append(sample.path)
    assert by_role[Role.KICK] == ["/s/kick1.wav", "/s/kick2.wav"]  # no loops for drums
    assert by_role[Role.FX] == ["/s/riser.wav"]  # loops allowed for FX
    assert by_role[Role.LEAD] == ["/s/lead.wav"]  # via the synth category
    assert composer.shortlist_samples(None, random.Random(0)) == {}


def test_prompt_mentions_request_and_samples():
    short = composer.shortlist_samples(lib(), random.Random(0))
    prompt = composer.build_prompt(ComposeRequest(genre="goa", idea="sunrise", key="Am", minutes=8), short)
    assert "goa" in prompt and "sunrise" in prompt and "Key: A." in prompt
    assert "about 288 bars" in prompt  # 8 min at 145 BPM, rounded to 8-bar phrases
    assert "kick1.wav" in prompt and "No samples for:" in prompt


def test_schema_is_structured_output_safe():
    def walk(node):
        if isinstance(node, dict):
            assert not {"minimum", "maximum", "minLength", "maxLength"} & node.keys()
            if node.get("type") == "object":
                assert node["additionalProperties"] is False
                assert set(node["required"]) == set(node["properties"])
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(composer._schema())


def test_compose_end_to_end():
    client, messages = fake_client(("end_turn", json.dumps(ANSWER)))
    result = compose(ComposeRequest(genre="psytrance", idea="night forest", seed=3), lib(), client=client)
    track = result.track

    call = messages.calls[0]
    assert call["_streamed"] and call["output_config"]["effort"] == "high"
    assert call["output_config"]["format"]["schema"] == composer._schema()

    assert (track.title, track.bpm, track.root, track.scale) == ("Forest Gate", 147, 6, "phrygian")
    assert [s.name for s in track.sections] == ["Intro", "Drop", "Melt", "Drop 2", "Outro"]
    assert track.parts[Role.KICK].sample == "/s/kick1.wav"
    assert track.parts[Role.BASS].sample == "/s/bass.wav"  # wrong id replaced from the library
    assert track.parts[Role.LEAD].sample == "/s/lead.wav"  # -1 filled from the library
    assert len(track.patterns["lead_a"].notes) == 3
    allowed = scale_pitch_classes(6, "phrygian")
    for n in track.timeline(Role.LEAD):
        assert n.pitch % 12 in allowed and 60 <= n.pitch <= 91
    assert result.fixes == []


def test_compose_without_library_or_sections():
    answer = dict(ANSWER, sections=[], bpm=200)
    client, _ = fake_client(("end_turn", json.dumps(answer)))
    result = compose(ComposeRequest(genre="progressive", key="D"), None, client=client)
    track = result.track
    assert track.root == 2  # the requested key wins
    assert track.bpm == 140
    assert all(p.sample is None for p in track.parts.values())
    assert track.sections[0].kind is SectionKind.INTRO
    assert "no arrangement was given, using the genre template" in result.fixes


def test_compose_rejects_non_object():
    client, _ = fake_client(("end_turn", "[1, 2]"))
    with pytest.raises(ai.ClaudeError):
        compose(ComposeRequest(), None, client=client)


def test_unknown_genre():
    with pytest.raises(ValueError):
        compose(ComposeRequest(genre="dubstep"), None, client=object())


def test_cli_compose_offline_and_show(isolated_home, capsys, tmp_path):
    from psygen import cli, config

    assert cli.main(["compose", "--offline", "-g", "goa", "--key", "E", "--minutes", "7"]) == 0
    out = capsys.readouterr().out
    assert "Goa sketch in E" in out and "Saved" in out
    saved = list(config.tracks_dir().glob("*.json"))
    assert len(saved) == 1
    track = Track.load(saved[0])
    assert track.genre == "goa" and track.root == 4
    assert cli.main(["show", str(saved[0])]) == 0
    assert "Arrangement:" in capsys.readouterr().out
    assert cli.main(["show", str(tmp_path / "missing.json")]) == 1


def test_cli_compose_claude(monkeypatch, isolated_home, capsys, tmp_path):
    from psygen import cli

    client, _ = fake_client(("end_turn", json.dumps(ANSWER)))
    real = composer.compose
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setattr(cli, "compose", lambda req, lib, **kw: real(req, lib, **{**kw, "client": client}))
    out_path = tmp_path / "t.json"
    assert cli.main(["compose", "-i", "forest", "-o", str(out_path)]) == 0
    assert "Forest Gate" in capsys.readouterr().out
    assert Track.load(out_path).title == "Forest Gate"


def test_cli_compose_claude_failure(monkeypatch, isolated_home, capsys):
    from psygen import cli

    def boom(*a, **kw):
        raise ai.ClaudeError("no key")

    monkeypatch.setattr(cli, "compose", boom)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    assert cli.main(["compose"]) == 1
    assert "no key" in capsys.readouterr().err
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    assert cli.main(["compose"]) == 1
    assert "no way to reach Claude" in capsys.readouterr().err
