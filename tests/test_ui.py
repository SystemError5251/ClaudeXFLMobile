import json
import os
import stat
import sys
import time

import pytest

from psygen import ai, cli, config
from psygen.samples import SampleLibrary
from psygen.ui import Controller, Settings, tasks
from psygen.ui.screens import ApiKeyScreen, FoldersScreen, MainScreen, RunScreen, TracksScreen
from psygen.ui.widgets import Choice, LineEdit, Text, Toggle, wrap
from test_ai import fake_client
from test_composer import ANSWER


# -- widgets -----------------------------------------------------------------


def test_line_edit():
    e = LineEdit("psy")
    for key in ("home", "x", "end", "backspace", "left", "delete", "right", "ü", "\x01"):
        e.handle(key)
    assert e.text == "xpü" and e.cursor == 3
    e.set("abcdefghij")
    assert e.view(4) == ("hij", 3)
    e.handle("home")
    assert e.view(4) == ("abcd", 0)
    assert LineEdit("key", mask=True).view(10) == ("***", 3)


def test_choice_toggle_and_text_fields():
    c = Choice("Genre", [("psytrance", "psy"), ("goa", "goa"), ("progressive", "prog")], "goa")
    assert c.value == "goa"
    c.handle("right")
    assert c.value == "progressive"
    c.handle("right")
    c.handle("left")
    c.handle("left")
    assert c.value == "goa"
    assert c.handle("x") is None
    t = Toggle("MP3")
    t.handle(" ")
    assert t.value is True
    text = Text("BPM", "", "auto")
    assert text.lines(40, False)[0].style == "dim"
    text.handle("1")
    line = text.lines(40, True)[0]
    assert line.text.endswith("1") and line.cursor == 12
    assert text.handle("enter") == "next"


def test_wrap_keeps_indent_and_cuts_long_paths():
    assert wrap("  a bb ccc", 6) == ["  a bb", "  ccc"]
    assert wrap("/very/long/path", 6) == ["/very/", "long/p", "ath"]
    assert wrap("a\n\nb", 5) == ["a", "", "b"]


# -- settings ----------------------------------------------------------------


def test_settings_round_trip_and_validation():
    cfg = config.Config(compose_effort="medium")
    s = Settings.from_config(cfg)
    assert s.composer == "claude-medium" and s.genre == "psytrance"
    cfg.ui = {"genre": "goa", "key": "H", "bpm": 145, "composer": "offline", "mp3": True}
    s = Settings.from_config(cfg)
    assert (s.genre, s.key, s.bpm, s.composer, s.mp3) == ("goa", "", "", "offline", True)

    s = Settings(genre="goa", bpm="146,5", minutes="8", key="F#", idea="  sunrise ")
    assert s.validate() == []
    r = s.request()
    assert (r.bpm, r.minutes, r.key, r.idea) == (146.5, 8.0, "F#", "sunrise")
    assert Settings(bpm="fast").validate() and Settings(minutes="90").validate()

    opts = Settings(output="preview", mp3=True).export_options(cfg)
    assert (opts.stems, opts.preview, opts.mp3) == (False, True, True)
    opts = Settings(output="midi", mp3=True).export_options(cfg)
    assert (opts.stems, opts.preview, opts.mp3) == (False, False, False)

    Settings(composer="claude-low", genre="progressive").remember(cfg)
    assert cfg.compose_effort == "low" and cfg.ui["genre"] == "progressive"


# -- controller --------------------------------------------------------------


def make_app(tmp_path, library=None, client=None):
    cfg = config.Config(export_dir=str(tmp_path / "out"))
    return Controller(cfg, library if library is not None else SampleLibrary(), client=client)


def wait_job(app, timeout=30):
    end = time.time() + timeout
    while time.time() < end:
        app.tick()
        screen = app.screen
        if isinstance(screen, RunScreen) and screen.finished is not None:
            return screen
        time.sleep(0.02)
    raise AssertionError("job did not finish")


def press(app, *keys):
    for key in keys:
        app.handle(key)


def select(app, label):
    """Move the selection to the item whose text starts with ``label``."""
    for line in app.render(80):
        if line.item is not None and line.text.lstrip("[ ").startswith(label):
            app.screen.selected = line.item
            return
    raise AssertionError(f"{label!r} not on screen")


def text_of(app):
    return "\n".join(line.text for line in app.render(80))


def test_generate_offline_end_to_end(tmp_path, isolated_home):
    app = make_app(tmp_path)
    select(app, "Composer")
    press(app, "left")  # offline template
    select(app, "Output")
    press(app, "left")  # MIDI only, quick
    select(app, "BPM")
    press(app, "1", "4", "2")
    select(app, "Generate")
    press(app, "enter")
    run = wait_job(app)
    assert run.job.state == "done", run.job.error
    text = text_of(app)
    assert "Done in" in text and "142 BPM" in text and "midi/  " in text
    folder = run.job.result.export.folder
    assert folder.parent == tmp_path / "out" and (folder / "info.txt").exists()
    assert run.job.result.track_path.parent == config.tracks_dir()
    # choices are remembered for next time
    saved = json.loads(config.config_path().read_text())
    assert saved["ui"]["composer"] == "offline" and saved["ui"]["bpm"] == "142"
    press(app, "enter")
    assert app.screen is app.main
    assert "Earlier tracks (1)" in text_of(app)


def test_generate_with_claude(tmp_path, isolated_home):
    client, _ = fake_client(("end_turn", json.dumps(ANSWER)))
    app = make_app(tmp_path, client=client)
    select(app, "Output")
    press(app, "left")
    select(app, "Idea")
    for ch in "dark forest":
        app.handle(ch)
    select(app, "Generate")
    press(app, "enter")
    run = wait_job(app)
    assert run.job.state == "done", run.job.error
    assert "Forest Gate" in text_of(app) and "147 BPM, F# phrygian" in text_of(app)
    assert "dark forest" in client.calls[0]["messages"][0]["content"]
    assert client.calls[0]["output_config"]["effort"] == "high"


def test_claude_failure_is_shown(tmp_path, isolated_home):
    client, _ = fake_client(ai.ClaudeError("the Claude API is overloaded, try again shortly"))
    app = make_app(tmp_path, client=client)
    app.generate(Settings(output="midi"))
    run = wait_job(app)
    assert run.job.state == "failed" and "overloaded" in text_of(app)


def test_generate_needs_a_key_or_offline(tmp_path, isolated_home, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    app = make_app(tmp_path)
    assert app.generate(Settings()) is None
    assert "No API key and no Claude Code" in app.status[0]
    assert "API key: missing" in text_of(app)
    assert app.generate(Settings(bpm="abc", composer="offline")) is None
    assert "BPM" in app.status[0]
    assert isinstance(app.screen, MainScreen)


def test_cancel_stops_at_the_next_step(tmp_path, isolated_home):
    app = make_app(tmp_path)
    gate = []

    def slow(report):
        report("step 1")
        while not gate:
            time.sleep(0.01)
        report("step 2")
        return "never"

    app.run_job("Slow", slow, on_done=lambda r: [(r, "normal")])
    press(app, "back")  # esc cancels while running
    assert app.screen.job.cancelling
    gate.append(1)
    run = wait_job(app)
    assert run.job.state == "cancelled" and "step 2" not in run.job.log
    press(app, "back")
    assert app.screen is app.main


def test_unexpected_errors_are_logged(tmp_path, isolated_home):
    app = make_app(tmp_path)
    app.run_job("Boom", lambda report: 1 / 0, on_done=lambda r: [])
    run = wait_job(app)
    assert run.job.state == "failed" and "ZeroDivisionError" in run.job.error
    assert "ZeroDivisionError" in tasks.crash_log_path().read_text()


def test_quit_only_with_q_or_button(tmp_path, isolated_home):
    app = make_app(tmp_path)
    press(app, "back")
    assert app.running
    select(app, "Idea")
    press(app, "q")  # typing in a text field
    assert app.running and app.main.idea.value == "q"
    select(app, "Quit")
    press(app, "enter")
    assert not app.running


def test_tap_selects_then_activates(tmp_path, isolated_home):
    app = make_app(tmp_path)
    item = next(l.item for l in app.render(80) if l.text.startswith("[ Sample folders"))
    app.tap(item)
    assert isinstance(app.screen, MainScreen) and app.screen.selected == item
    app.tap(item)
    assert isinstance(app.screen, FoldersScreen)


# -- sub screens -------------------------------------------------------------


def test_folders_add_scan_remove(tmp_path, isolated_home):
    from conftest import kick, write_wav

    samples = tmp_path / "My Samples"
    write_wav(samples / "Kicks" / "k1.wav", kick())
    app = make_app(tmp_path)
    select(app, "Sample folders")
    press(app, "enter")
    assert isinstance(app.screen, FoldersScreen)
    select(app, "Add")
    for ch in str(tmp_path / "nope"):
        app.handle(ch)
    press(app, "enter")
    assert "Not a folder" in app.status[0]
    app.screen.new.edit.set(str(samples))
    press(app, "enter")
    assert app.cfg.sample_dirs == [str(samples)]
    assert config.Config.load().sample_dirs == [str(samples)]
    select(app, "Scan now")
    press(app, "enter")
    run = wait_job(app)
    assert run.job.state == "done", run.job.error
    assert len(app.library) == 1 and "kick" in text_of(app)
    assert len(SampleLibrary.load(config.index_path())) == 1
    press(app, "enter")
    assert isinstance(app.screen, FoldersScreen) and "1 samples: kick 1" in text_of(app)
    select(app, str(samples))
    press(app, "enter")
    assert "remove" in text_of(app) and app.cfg.sample_dirs  # asks first
    press(app, "enter")
    assert app.cfg.sample_dirs == []
    press(app, "back")
    assert "Sample folders (1 samples)" in text_of(app)


def test_scan_without_folders_fails_cleanly(tmp_path, isolated_home):
    app = make_app(tmp_path)
    app.push(FoldersScreen(app))
    select(app, "Scan now")
    press(app, "enter")
    run = wait_job(app)
    assert run.job.state == "failed" and "termux-setup-storage" in run.job.error


def test_no_folder_hint_with_storage(tmp_path, monkeypatch):
    shared = tmp_path / "shared"
    shared.mkdir()
    monkeypatch.setattr(config, "SHARED_STORAGE_ROOTS", (tmp_path / "nope",))
    assert "termux-setup-storage" in config.no_sample_dirs_hint()
    monkeypatch.setattr(config, "SHARED_STORAGE_ROOTS", (shared,))
    assert config.storage_ready() and "FLM User Files" in config.no_sample_dirs_hint()


def test_api_key_save_test_remove(tmp_path, isolated_home, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    app = make_app(tmp_path)
    app.push(ApiKeyScreen(app))
    assert "No key yet" in text_of(app)
    select(app, "New key")
    for ch in "sk-ant-test":
        app.handle(ch)
    assert "sk-ant" not in text_of(app)  # masked
    press(app, "enter")
    path = config.api_key_path()
    assert path.read_text().strip() == "sk-ant-test"
    if os.name == "posix":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert ai.key_source() == "saved" and ai.make_client().api_key == "sk-ant-test"

    checked = []
    monkeypatch.setattr(ai.HttpClient, "check", lambda self: checked.append(self.api_key))
    select(app, "Test key")
    press(app, "enter")
    for _ in range(200):
        if not app.side_jobs:
            break
        app.tick()
        time.sleep(0.01)
    assert checked == ["sk-ant-test"] and app.status == ("The key works.", "ok")

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-env")
    assert ai.make_client().api_key == "sk-env" and "environment" in text_of(app)
    select(app, "Remove saved key")
    press(app, "enter")
    assert not path.exists() and config.load_api_key() is None


def test_key_check_request(monkeypatch):
    seen = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b"{}"

    def fake(request, timeout):
        seen.append(request)
        return Response()

    monkeypatch.setattr(ai.urllib.request, "urlopen", fake)
    ai.HttpClient(api_key="sk-x").check()
    assert seen[0].full_url == "https://api.anthropic.com/v1/models?limit=1"
    assert seen[0].get_method() == "GET"


def test_reexport_an_earlier_track(tmp_path, isolated_home):
    app = make_app(tmp_path)
    app.generate(Settings(composer="offline", output="midi"))
    wait_job(app)
    press(app, "back")
    select(app, "Earlier tracks")
    press(app, "enter")
    assert isinstance(app.screen, TracksScreen) and "sketch" in text_of(app)
    app.screen.selected = 0
    press(app, "enter")
    run = wait_job(app)
    assert run.job.state == "done", run.job.error
    assert "Files are in:" in text_of(app)


# -- entry points ------------------------------------------------------------


def test_cli_without_terminal_explains(capsys, monkeypatch):
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False, raising=False)
    assert cli.main([]) == 1
    assert "needs a terminal" in capsys.readouterr().err
    assert cli.main(["ui"]) == 1


@pytest.mark.skipif(os.name != "posix", reason="needs a pseudo terminal")
def test_curses_ui_starts_and_quits(tmp_path, isolated_home):
    """Run the real UI in a pseudo terminal: it draws and quits on q."""
    import fcntl
    import pty
    import select as sel
    import struct
    import termios

    pid, fd = pty.fork()
    if pid == 0:  # child
        try:
            fcntl.ioctl(0, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 48, 0, 0))
            os.environ["TERM"] = "xterm"
            os.execv(sys.executable, [sys.executable, "-m", "psygen", "ui"])
        finally:
            os._exit(127)
    out = b""

    def pump(seconds, until=None):
        nonlocal out
        end = time.time() + seconds
        while time.time() < end:
            if until and until in out:
                return
            if sel.select([fd], [], [], 0.05)[0]:
                try:
                    out += os.read(fd, 65536)
                except OSError:
                    return

    pump(20, until=b"Generate and export")
    assert b"New track" in out and b"Generate and export" in out
    os.write(fd, b"\x1bOB\x1bOB")  # arrow down twice, onto Key (not a text field)
    pump(0.5)
    os.write(fd, b"q")
    end = time.time() + 10
    status = None
    while time.time() < end:
        done, status = os.waitpid(pid, os.WNOHANG)
        if done:
            break
        pump(0.1)
    else:
        os.kill(pid, 9)
        raise AssertionError("UI did not quit")
    assert os.waitstatus_to_exitcode(status) == 0


def test_first_start_runs_termux_setup_storage_once(tmp_path, isolated_home, monkeypatch):
    from psygen.ui import app as ui_app

    shared = tmp_path / "shared"
    monkeypatch.setattr(config, "SHARED_STORAGE_ROOTS", (shared,))
    monkeypatch.setattr(config, "is_termux", lambda: True)
    monkeypatch.setattr(ui_app.shutil, "which", lambda name: "/bin/" + name)
    ran = []

    def fake_run(cmd):
        ran.append(cmd)
        (shared / "Music").mkdir(parents=True)  # the user allowed access

    cfg = config.Config.load()
    ui_app.first_start_storage_setup(cfg, run=fake_run, sleep=lambda s: None)
    assert ran == [["termux-setup-storage"]]
    assert config.Config.load().storage_setup_done

    # never again, even if access is gone later
    import shutil as sh
    sh.rmtree(shared)
    ui_app.first_start_storage_setup(config.Config.load(), run=fake_run, sleep=lambda s: None)
    assert len(ran) == 1


def test_first_start_skips_when_storage_works_or_not_termux(tmp_path, isolated_home, monkeypatch):
    from psygen.ui import app as ui_app

    ran = []
    monkeypatch.setattr(config, "is_termux", lambda: False)
    ui_app.first_start_storage_setup(config.Config(), run=ran.append, sleep=lambda s: None)
    monkeypatch.setattr(config, "is_termux", lambda: True)
    monkeypatch.setattr(config, "SHARED_STORAGE_ROOTS", (tmp_path,))
    cfg = config.Config()
    ui_app.first_start_storage_setup(cfg, run=ran.append, sleep=lambda s: None)
    assert ran == [] and cfg.storage_setup_done
