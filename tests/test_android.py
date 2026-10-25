import os
import sys
import time

import pytest

from psygen import android, cli, config
from psygen.ui import Settings

pytestmark = pytest.mark.skipif(os.name != "posix", reason="fake commands are shell scripts")


@pytest.fixture
def termux(tmp_path, monkeypatch):
    """Pretend to be Termux, with fake `am` and Termux:API commands that log calls."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "calls.log"
    for name in ("am", "termux-notification", "termux-wake-lock", "termux-wake-unlock"):
        script = bin_dir / name
        script.write_text(f'#!/bin/sh\necho "{name} $*" >> "{log}"\necho Starting: Intent\n')
        script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setattr(config, "is_termux", lambda: True)
    return lambda: log.read_text().splitlines() if log.exists() else []


def test_open_flm_and_notify(termux):
    assert android.open_flm()
    call = termux()[0]
    assert call.startswith("am start") and "package=com.imageline.FLM" in call
    assert "category=android.intent.category.LAUNCHER" in call
    assert android.notify("t", "x") and "--title t" in termux()[1]
    assert android.wake_lock(True) and termux()[2] == "termux-wake-lock "


def test_quiet_off_termux(monkeypatch):
    monkeypatch.setattr(config, "is_termux", lambda: False)
    assert not android.open_flm() and not android.notify("a", "b") and not android.wake_lock(True)


def test_am_error_counts_as_failure(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "am").write_text("#!/bin/sh\necho 'Error: Activity not started'\n")
    (bin_dir / "am").chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setattr(config, "is_termux", lambda: True)
    assert not android.open_flm()


def test_cli_export_opens_flm_unless_told_not_to(termux, tmp_path, isolated_home, capsys):
    out = tmp_path / "out"
    assert cli.main(["compose", "--offline", "--export", "--midi-only", "--dest", str(out)]) == 0
    assert "Opened FL Studio Mobile" in capsys.readouterr().out
    assert any(c.startswith("am start") for c in termux())
    n = len(termux())
    assert cli.main(["compose", "--offline", "--export", "--midi-only", "--dest", str(out), "--no-open-flm"]) == 0
    assert len(termux()) == n


def test_cli_background_detaches_with_export_and_notify(monkeypatch, capsys):
    seen = []
    monkeypatch.setattr(android, "start_background", lambda argv: seen.append(argv) or "/tmp/log")
    assert cli.main(["compose", "-g", "goa", "--background"]) == 0
    assert seen == [["compose", "-g", "goa", "--export", "--notify"]]
    assert "background" in capsys.readouterr().out


def test_background_child_holds_wake_lock_and_notifies(termux, tmp_path, isolated_home, monkeypatch):
    monkeypatch.setenv("PSYGEN_BACKGROUND", "1")
    out = tmp_path / "out"
    assert cli.main(["compose", "--offline", "--export", "--midi-only", "--notify", "--dest", str(out)]) == 0
    calls = termux()
    assert calls[0] == "termux-wake-lock "
    assert any(c.startswith("termux-wake-unlock") for c in calls)
    assert "track ready" in calls[-1]


def test_real_background_run(tmp_path, isolated_home):
    """Spawn a real detached psygen and wait for its export."""
    out = tmp_path / "out"
    android.start_background(["compose", "--offline", "--export", "--midi-only", "--dest", str(out)])
    deadline = time.time() + 60
    while time.time() < deadline and not list(out.glob("*/info.txt")):
        time.sleep(0.2)
    assert list(out.glob("*/info.txt")), android.background_log().read_text()
    for _ in range(50):  # the log is complete once the process exits
        if "Done:" in android.background_log().read_text():
            break
        time.sleep(0.1)
    assert "Done:" in android.background_log().read_text()


def test_settings_as_command_line():
    s = Settings(genre="goa", bpm="146,5", key="F#", idea=" sunrise ", composer="offline",
                 output="preview", mp3=True)
    assert s.cli_args() == ["compose", "-g", "goa", "--bpm", "146.5", "--key", "F#", "--idea", "sunrise",
                            "--offline", "--export", "--no-stems", "--mp3"]
    assert cli.build_parser().parse_args(s.cli_args()).bpm == 146.5
    assert Settings(output="midi", mp3=True).cli_args()[-1] == "--midi-only"


def test_ui_background_button(tmp_path, isolated_home, monkeypatch):
    from test_ui import make_app, press, select

    seen = []
    monkeypatch.setattr(android, "start_background", lambda argv: seen.append(argv))
    app = make_app(tmp_path)
    select(app, "Composer")
    press(app, "left")  # offline
    select(app, "Generate in background")
    press(app, "enter")
    assert seen and seen[0][:2] == ["compose", "-g"] and "--offline" in seen[0]
    assert "FL Studio Mobile opens" in app.status[0]


def test_ui_generate_opens_flm(termux, tmp_path, isolated_home):
    from test_ui import make_app, text_of, wait_job

    app = make_app(tmp_path)
    app.generate(Settings(composer="offline", output="midi"))
    run = wait_job(app)
    assert run.job.state == "done", run.job.error
    assert "Opened FL Studio Mobile" in text_of(app)
    app.cfg.open_flm_after_export = False
    n = len(termux())
    app.pop()
    app.generate(Settings(composer="offline", output="midi"))
    wait_job(app)
    assert len(termux()) == n
