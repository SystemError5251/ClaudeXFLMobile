import shutil
import subprocess

import pytest

from psygen import cli, update
from psygen.update import UpdateError

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")


def git(cwd, *args):
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True)


def commit(repo, name, text, message):
    (repo / name).write_text(text)
    git(repo, "add", name)
    git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", message)


@pytest.fixture
def repos(tmp_path):
    """An "upstream" clone to publish from and the user's install checkout."""
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
    upstream = tmp_path / "upstream"
    subprocess.run(["git", "clone", "-q", str(origin), str(upstream)], check=True, capture_output=True)
    git(upstream, "checkout", "-q", "-b", "main")
    commit(upstream, "pyproject.toml", "[project]\nname='x'\n", "first")
    git(upstream, "push", "-q", "-u", "origin", "main")
    install = tmp_path / "install"
    subprocess.run(["git", "clone", "-q", str(origin), str(install)], check=True, capture_output=True)
    return upstream, install


def test_up_to_date(repos):
    _, install = repos
    result = update.update(install)
    assert not result.updated and not result.commits
    assert "up to date" in result.message()


def test_pulls_new_commits_without_pip(repos, monkeypatch):
    upstream, install = repos
    commit(upstream, "a.py", "1", "add a")
    commit(upstream, "b.py", "2", "add b")
    git(upstream, "push", "-q")
    assert len(update.update(install, check_only=True).commits) == 2
    assert not (install / "a.py").exists()

    ran = []
    monkeypatch.setattr(update.subprocess, "run", _spy(ran))
    messages = []
    result = update.update(install, report=messages.append)
    assert result.updated and not result.reinstalled
    assert [c.split(" ", 1)[1] for c in result.commits] == ["add b", "add a"]
    assert (install / "b.py").exists() and "Restart psygen" in result.message()
    assert not any("pip" in cmd for cmd in ran)
    assert "pulling 2 new commits" in messages


def test_reinstalls_when_pyproject_changes(repos, monkeypatch):
    upstream, install = repos
    commit(upstream, "pyproject.toml", "[project]\nname='y'\n", "deps")
    git(upstream, "push", "-q")
    ran = []
    monkeypatch.setattr(update.subprocess, "run", _spy(ran, fake_pip=True))
    result = update.update(install)
    assert result.reinstalled
    assert any("pip" in cmd and str(install) in cmd for cmd in ran)


def test_local_changes_are_left_alone(repos):
    upstream, install = repos
    commit(upstream, "a.py", "1", "add a")
    git(upstream, "push", "-q")
    (install / "pyproject.toml").write_text("edited")
    with pytest.raises(UpdateError, match="local changes"):
        update.update(install)
    assert (install / "pyproject.toml").read_text() == "edited"


def test_diverged_branch(repos):
    upstream, install = repos
    commit(upstream, "a.py", "1", "theirs")
    git(upstream, "push", "-q")
    commit(install, "mine.py", "1", "mine")
    with pytest.raises(UpdateError, match="own commits"):
        update.update(install)


def test_not_a_checkout(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(update, "checkout_root", lambda: None)
    with pytest.raises(UpdateError, match="git clone"):
        update.update()
    assert cli.main(["update"]) == 1
    assert "not installed from a git checkout" in capsys.readouterr().err


def test_cli_update_check(repos, monkeypatch, capsys):
    upstream, install = repos
    commit(upstream, "a.py", "1", "add a")
    git(upstream, "push", "-q")
    monkeypatch.setattr(update, "checkout_root", lambda: install)
    assert cli.main(["update", "--check"]) == 0
    out = capsys.readouterr().out
    assert "add a" in out and "1 new commits available" in out


def test_ui_update_button(repos, monkeypatch, tmp_path, isolated_home):
    from test_ui import make_app, select, text_of, wait_job

    upstream, install = repos
    commit(upstream, "a.py", "1", "add a")
    git(upstream, "push", "-q")
    monkeypatch.setattr(update, "checkout_root", lambda: install)
    app = make_app(tmp_path)
    select(app, "Update psygen")
    app.handle("enter")
    run = wait_job(app)
    assert run.job.state == "done", run.job.error
    assert "Restart psygen" in text_of(app) and "start psygen again" in text_of(app)

    (install / "pyproject.toml").write_text("edited")
    app.handle("enter")
    app.update()
    run = wait_job(app)
    assert run.job.state == "failed" and "local changes" in run.job.error


def _spy(ran, fake_pip=False):
    real = subprocess.run

    def run(cmd, *args, **kwargs):
        ran.append(" ".join(cmd))
        if fake_pip and "pip" in cmd:
            return subprocess.CompletedProcess(cmd, 0, "", "")
        return real(cmd, *args, **kwargs)

    return run
