"""Screens and the controller that switches between them (no curses here)."""

from __future__ import annotations

import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from .. import ai, android, config
from ..compose import GENRES, Track
from ..compose.theory import NOTE_NAMES, SCALES
from ..samples import SampleLibrary
from . import tasks
from .tasks import Job, Settings
from .widgets import CHANGED, Button, Choice, Field, Line, Static, Text, Toggle

SPINNER = "|/-\\"


class Screen:
    title = ""
    hints = "arrows move  enter select  esc back"

    def __init__(self, app: "Controller"):
        self.app = app
        self.selected = 0

    def items(self) -> list[Field]:
        return []

    def on_show(self) -> None:
        """Called when the screen appears, also when returning to it."""

    def on_action(self, action: str) -> None:
        pass

    def on_back(self) -> None:
        self.app.pop()

    def on_key(self, key: str) -> None:
        pass

    def tick(self) -> bool:
        return False

    # -- shared behaviour ---------------------------------------------------

    def selectables(self, items: list[Field] | None = None) -> list[Field]:
        return [f for f in (items if items is not None else self.items()) if f.selectable]

    def move(self, delta: int) -> None:
        count = len(self.selectables())
        if count:
            self.selected = max(0, min(count - 1, self.selected + delta))

    def handle(self, key: str) -> None:
        sel = self.selectables()
        if self.selected >= len(sel):
            self.selected = max(0, len(sel) - 1)
        if key in ("up", "down", "tab", "pageup", "pagedown"):
            step = {"up": -1, "down": 1, "tab": 1, "pageup": -5, "pagedown": 5}[key]
            self.move(step)
            return
        field = sel[self.selected] if sel else None
        result = field.handle(key) if field else None
        if result is None:
            if key == "back":
                self.on_back()
            else:
                self.on_key(key)
        elif result == "next":
            self.move(1)
        elif result != CHANGED:
            self.on_action(result)

    def tap(self, item: int) -> None:
        if item == self.selected:
            self.handle("enter")
        else:
            self.selected = item

    def render(self, width: int) -> list[Line]:
        lines: list[Line] = []
        index = 0
        for field in self.items():
            if field.selectable:
                chunk = field.lines(width, index == self.selected)
                for line in chunk:
                    line.item = index
                index += 1
            else:
                chunk = field.lines(width, False)
            lines.extend(chunk)
        return lines


# -- main screen -------------------------------------------------------------


class MainScreen(Screen):
    title = "New track"
    hints = "arrows move  left/right change  enter go  q quit"

    def __init__(self, app: "Controller"):
        super().__init__(app)
        s = app.settings
        self.genre = Choice("Genre", [(g, g) for g in GENRES], s.genre)
        self.bpm = Text("BPM", s.bpm, "auto")
        self.key = Choice("Key", [("", "auto"), *((n, n) for n in NOTE_NAMES)], s.key)
        self.scale = Choice("Scale", [("", "auto"), *((k, k.replace("_", " ")) for k in SCALES)], s.scale)
        self.minutes = Text("Length", s.minutes, "auto (minutes)")
        self.idea = Text("Idea", s.idea, "mood, story, references...")
        self.composer = Choice("Composer", [
            ("claude-high", "Claude, high effort"),
            ("claude-medium", "Claude, medium effort"),
            ("claude-low", "Claude, low effort"),
            ("offline", "offline template"),
        ], s.composer)
        self.output = Choice("Output", [
            ("all", "MIDI + stems + preview"),
            ("preview", "MIDI + preview"),
            ("midi", "MIDI only"),
        ], s.output)
        self.mp3 = Toggle("MP3", s.mp3, "also an MP3 preview")
        self.track_count = 0

    def on_show(self) -> None:
        self.track_count = len(tasks.saved_tracks())

    def collect(self) -> Settings:
        return Settings(
            genre=self.genre.value, bpm=self.bpm.value, key=self.key.value, scale=self.scale.value,
            minutes=self.minutes.value, idea=self.idea.value, composer=self.composer.value,
            output=self.output.value, mp3=self.mp3.value,
        )

    def items(self) -> list[Field]:
        app = self.app
        source = ai.key_source()
        cli = ai.claude_code_path(app.cfg)
        key_text = {"env": "set (environment)", "saved": "saved"}.get(source or "", "")
        if cli:
            key_text = f"{key_text} + Claude Code" if key_text else "none, using Claude Code"
        source = source or cli
        key_text = key_text or "missing"
        return [
            self.genre, self.bpm, self.key, self.scale, self.minutes, self.idea,
            self.composer, self.output, self.mp3,
            Button("Generate and export", "generate", "button"),
            Button("Generate in background", "background", "normal"),
            Static(),
            Button(f"Sample folders ({len(app.library)} samples)", "folders", "normal"),
            Button(f"API key: {key_text}", "apikey", "normal" if source else "warn"),
            Button(f"Earlier tracks ({self.track_count})", "tracks", "normal"),
            Button("Update psygen", "update", "normal"),
            Button("Quit", "quit", "normal"),
            Static(),
            Static(f"Exports go to {app.cfg.resolved_export_dir()}", "dim"),
        ]

    def on_back(self) -> None:
        pass  # esc on the main screen does nothing, so a stray tap can't quit

    def on_key(self, key: str) -> None:
        if key in ("q", "Q"):
            self.app.quit()

    def on_action(self, action: str) -> None:
        app = self.app
        if action == "generate":
            app.generate(self.collect())
        elif action == "background":
            app.generate_in_background(self.collect())
        elif action == "folders":
            app.push(FoldersScreen(app))
        elif action == "apikey":
            app.push(ApiKeyScreen(app))
        elif action == "tracks":
            app.push(TracksScreen(app))
        elif action == "update":
            app.update()
        elif action == "quit":
            app.quit()


# -- sample folders ----------------------------------------------------------


class FoldersScreen(Screen):
    title = "Sample folders"

    def __init__(self, app: "Controller"):
        super().__init__(app)
        self.new = Text("Add", "", "path, e.g. ~/storage/shared/Samples")
        self.use_claude = Toggle("Claude", False, "sort unclear samples (API)")
        self.confirm: int | None = None

    def items(self) -> list[Field]:
        cfg = self.app.cfg
        items: list[Field] = []
        if cfg.sample_dirs:
            items.append(Static("Scanned folders (enter removes one):", "dim"))
            for i, d in enumerate(cfg.sample_dirs):
                missing = "" if Path(d).expanduser().is_dir() else "  (missing)"
                label = f"remove {d}? enter again" if self.confirm == i else d + missing
                items.append(Button(label, f"remove:{i}", "warn" if missing or self.confirm == i else "normal"))
        else:
            auto = config.default_sample_dirs()
            if auto:
                items.append(Static("Found automatically:", "dim"))
                items.extend(Static("  " + str(d)) for d in auto)
            else:
                items.append(Static(config.no_sample_dirs_hint(), "warn"))
        items += [self.new, self.use_claude, Button("Scan now", "scan"), Button("Back", "back", "normal"), Static()]
        lib = self.app.library
        if len(lib):
            counts = ", ".join(f"{c.value} {n}" for c, n in lib.counts().items())
            items.append(Static(f"{len(lib)} samples: {counts}", "dim"))
        else:
            items.append(Static("The library is empty. Scan to fill it.", "dim"))
        return items

    def handle(self, key: str) -> None:
        if key != "enter":
            self.confirm = None
        if key == "enter" and self.selectables()[self.selected] is self.new:
            self.on_action("add")
            return
        super().handle(key)

    def on_action(self, action: str) -> None:
        app, cfg = self.app, self.app.cfg
        if action.startswith("remove:"):
            i = int(action.split(":", 1)[1])
            if self.confirm != i:
                self.confirm = i
                return
            self.confirm = None
            removed = cfg.sample_dirs.pop(i)
            app.save_config()
            app.say(f"Removed {removed}. Scan to update the library.", "ok")
            self.move(0)
        elif action == "add":
            text = self.new.value.strip()
            if not text:
                app.say("Type a folder path first.", "warn")
                return
            path = Path(text).expanduser()
            if not path.is_dir():
                app.say(f"Not a folder: {path}", "error")
                return
            if not cfg.sample_dirs:
                # Keep the auto-detected folders: adding one switches auto-detection off.
                for d in config.default_sample_dirs():
                    cfg.add_sample_dir(d)
            added = cfg.add_sample_dir(path)
            app.save_config()
            self.new.edit.set("")
            app.say(f"Added {path}. Choose Scan now." if added else f"{path} is already in the list.", "ok")
        elif action == "scan":
            use_claude = self.use_claude.value
            if use_claude and not ai.can_reach_claude(cfg):
                app.say("Claude sorting needs an API key or Claude Code (see API key).", "error")
                return
            app.run_job(
                "Scanning samples",
                lambda report: tasks.scan_library(cfg, report, use_claude),
                on_done=self._scanned,
            )
        elif action == "back":
            app.pop()

    def _scanned(self, library: SampleLibrary) -> list[tuple[str, str]]:
        self.app.library = library
        lines = [(f"{len(library)} samples in the library", "title")]
        lines += [(f"  {c.value:<11} {n:>5}", "normal") for c, n in library.counts().items()]
        return lines


# -- API key -----------------------------------------------------------------


class ApiKeyScreen(Screen):
    title = "Claude API key"

    def __init__(self, app: "Controller"):
        super().__init__(app)
        self.new = Text("New key", "", "paste sk-ant-...", mask=True)

    def items(self) -> list[Field]:
        source = ai.key_source()
        if source == "env":
            status = Static("Using ANTHROPIC_API_KEY from the environment. It wins over a saved key.", "ok")
        elif source == "saved":
            status = Static(f"Using the key saved in {config.api_key_path()}.", "ok")
        else:
            status = Static("No key yet. Create one at platform.claude.com under API keys "
                            "and paste it here (long-press, Paste).", "warn")
        cli = ai.claude_code_path(self.app.cfg)
        if cli:
            backup = Static(f"Claude Code found ({cli}). psygen uses it when there is no key or "
                            "the API has no credits left. Log in once by running claude.", "ok")
        else:
            backup = Static("Optional backup: install Claude Code (pkg install nodejs, then npm install "
                            "-g @anthropic-ai/claude-code) to compose with your Claude account when "
                            "the API has no credits.", "dim")
        items: list[Field] = [status, backup, Static(), self.new, Button("Save key", "save")]
        if source:
            items.append(Button("Test key", "test", "normal"))
        if config.load_api_key():
            items.append(Button("Remove saved key", "remove", "normal"))
        items += [Button("Back", "back", "normal"), Static(),
                  Static("The key is stored readable only by you (chmod 600). Testing it lists "
                         "one model, which costs nothing.", "dim")]
        return items

    def handle(self, key: str) -> None:
        if key == "enter" and self.selectables()[self.selected] is self.new:
            self.on_action("save")
            return
        super().handle(key)

    def on_action(self, action: str) -> None:
        app = self.app
        if action == "save":
            key = self.new.value.strip()
            if not key or any(c.isspace() for c in key):
                app.say("Paste the whole key first (no spaces).", "error")
                return
            try:
                config.save_api_key(key)
            except OSError as exc:
                app.say(f"Could not save the key: {exc}", "error")
                return
            self.new.edit.set("")
            note = "" if key.startswith("sk-ant-") else " It doesn't look like an sk-ant- key."
            app.say("Key saved." + note + " Choose Test key to check it.", "ok" if not note else "warn")
        elif action == "test":
            app.say("Testing the key...", "dim")
            app.background(Job("key test", lambda report: ai.api_client().check()), self._tested)
        elif action == "remove":
            config.delete_api_key()
            app.say("Saved key removed.", "ok")
            self.move(0)
        elif action == "back":
            app.pop()

    def _tested(self, job: Job) -> None:
        if job.state == "done":
            self.app.say("The key works.", "ok")
        else:
            self.app.say(f"Key test failed: {job.error}", "error")


# -- earlier tracks ----------------------------------------------------------


class TracksScreen(Screen):
    title = "Earlier tracks"

    def __init__(self, app: "Controller"):
        super().__init__(app)
        self.paths: list[Path] = []

    def on_show(self) -> None:
        self.paths = tasks.saved_tracks()[:100]

    def items(self) -> list[Field]:
        if not self.paths:
            return [Static("No composed tracks yet.", "dim"), Button("Back", "back", "normal")]
        items: list[Field] = [Static("Enter exports a track again, with the Output setting "
                                     "from the main screen.", "dim")]
        for i, p in enumerate(self.paths):
            when = datetime.fromtimestamp(p.stat().st_mtime).strftime("%d.%m. %H:%M")
            items.append(Button(f"{when}  {p.stem}", f"export:{i}", "normal"))
        items.append(Button("Back", "back", "normal"))
        return items

    def on_action(self, action: str) -> None:
        app = self.app
        if action == "back":
            app.pop()
            return
        path = self.paths[int(action.split(":", 1)[1])]
        try:
            track = Track.load(path)
        except (OSError, ValueError, KeyError) as exc:
            app.say(f"Cannot read {path.name}: {exc}", "error")
            return
        settings = app.main.collect()
        app.run_job(
            f"Exporting {track.title}",
            lambda report: tasks.export_saved(track, settings, app.cfg, app.library, report),
            on_done=lambda export: tasks.summary(export, track),
        )


# -- job progress ------------------------------------------------------------


class RunScreen(Screen):

    def __init__(self, app: "Controller", job: Job, on_done: Callable[[Any], list[tuple[str, str]]]):
        super().__init__(app)
        self.job = job
        self.title = job.title
        self.on_done = on_done
        self.started = time.monotonic()
        self.finished: float | None = None
        self.result_lines: list[tuple[str, str]] = []
        self.frame = 0

    @property
    def hints(self) -> str:
        return "esc cancel" if self.job.state == "running" else "enter or esc: back to menu"

    def tick(self) -> bool:
        changed = self.job.poll()
        if self.job.state != "running" and self.finished is None:
            self.finished = time.monotonic()
            if self.job.state == "done":
                try:
                    self.result_lines = self.on_done(self.job.result)
                except Exception as exc:  # noqa: BLE001 - keep the UI alive
                    self.result_lines = [(f"Could not show the result: {exc}", "error")]
            self.selected = 0
            changed = True
        if self.job.state == "running":
            self.frame += 1
            changed = True  # spinner and clock
        return changed

    def items(self) -> list[Field]:
        job = self.job
        elapsed = int((self.finished or time.monotonic()) - self.started)
        clock = f"{elapsed // 60}:{elapsed % 60:02d}"
        if job.state == "running":
            word = "cancelling" if job.cancelling else "working"
            head = Static(f"{SPINNER[self.frame // 2 % 4]} {word}  {clock}", "title")
        elif job.state == "done":
            head = Static(f"Done in {clock}", "ok")
        elif job.state == "cancelled":
            head = Static("Cancelled", "warn")
        else:
            head = Static(f"Failed: {job.error}", "error")
        items: list[Field] = [head]
        items += [Static("  " + msg, "dim") for msg in job.log]
        if self.result_lines:
            items.append(Static())
            items += [Static(text, style) for text, style in self.result_lines]
        items.append(Static())
        if job.state == "running":
            items.append(Button("Cancel", "cancel", "normal"))
        else:
            items.append(Button("Back to menu", "back", "button"))
        return items

    def on_back(self) -> None:
        if self.job.state == "running":
            self.on_action("cancel")
        else:
            self.app.pop()

    def on_action(self, action: str) -> None:
        if action == "cancel":
            self.job.cancel()
            self.app.say("Cancelling after the current step...", "warn")
        elif action == "back":
            self.app.pop()


# -- controller --------------------------------------------------------------


class Controller:
    """The UI's state and navigation, driven by key names. The curses front
    end (``app.py``) only draws what :meth:`render` returns."""

    def __init__(self, cfg: config.Config | None = None, library: SampleLibrary | None = None, client=None):
        self.cfg = cfg or config.Config.load()
        self.library = library if library is not None else SampleLibrary.load(config.index_path())
        self.client = client  # stand-in for the Claude API in tests
        self.settings = Settings.from_config(self.cfg)
        self.status: tuple[str, str] = ("", "dim")
        self.running = True
        self.main = MainScreen(self)
        self.stack: list[Screen] = [self.main]
        self.side_jobs: list[tuple[Job, Callable[[Job], None]]] = []
        self.main.on_show()

    @property
    def screen(self) -> Screen:
        return self.stack[-1]

    def push(self, screen: Screen) -> None:
        self.stack.append(screen)
        self.status = ("", "dim")
        screen.on_show()

    def pop(self) -> None:
        if len(self.stack) > 1:
            self.stack.pop()
            self.screen.on_show()

    def quit(self) -> None:
        self.running = False

    def say(self, text: str, style: str = "normal") -> None:
        self.status = (text, style)

    def save_config(self) -> None:
        try:
            self.cfg.save()
        except OSError as exc:
            self.say(f"Could not save settings: {exc}", "error")

    # -- input --------------------------------------------------------------

    def handle(self, key: str) -> None:
        if key not in ("resize",):
            if self.status[1] != "dim" or self.status[0]:
                self.status = ("", "dim")
            self.screen.handle(key)

    def tap(self, item: int) -> None:
        self.status = ("", "dim")
        self.screen.tap(item)

    def tick(self) -> bool:
        changed = self.screen.tick()
        for entry in list(self.side_jobs):
            job, done = entry
            job.poll()
            if job.state != "running":
                self.side_jobs.remove(entry)
                done(job)
                changed = True
        return changed

    @property
    def busy(self) -> bool:
        return bool(self.side_jobs) or isinstance(self.screen, RunScreen) and self.screen.job.state == "running"

    def render(self, width: int) -> list[Line]:
        return self.screen.render(width)

    # -- work ---------------------------------------------------------------

    def run_job(self, title: str, fn: Callable[[tasks.Report], Any],
                on_done: Callable[[Any], list[tuple[str, str]]]) -> Job:
        job = Job(title, fn).start()
        self.push(RunScreen(self, job, on_done))
        return job

    def background(self, job: Job, done: Callable[[Job], None]) -> None:
        self.side_jobs.append((job.start(), done))

    def update(self) -> Job:
        from ..update import UpdateError, update

        def work(report: tasks.Report):
            try:
                return update(report=report)
            except UpdateError as exc:
                raise ValueError(str(exc)) from exc

        def done(result) -> list[tuple[str, str]]:
            lines = [(result.message(), "ok" if result.updated else "normal")]
            lines += [(f"  {c}", "dim") for c in result.commits[:20]]
            if result.updated:
                lines.append(("Choose Quit and start psygen again.", "warn"))
            return lines

        return self.run_job("Updating psygen", work, on_done=done)

    def _ready(self, settings: Settings) -> bool:
        problems = settings.validate()
        if problems:
            self.say(problems[0], "error")
            return False
        if not settings.offline and self.client is None and not ai.can_reach_claude(self.cfg):
            self.say("No API key and no Claude Code. Open API key, or set Composer to offline template.", "error")
            return False
        self.settings = settings
        settings.remember(self.cfg)
        self.save_config()
        return True

    def generate_in_background(self, settings: Settings) -> bool:
        if not self._ready(settings):
            return False
        try:
            android.start_background(settings.cli_args())
        except OSError as exc:
            self.say(f"Could not start the background run: {exc}", "error")
            return False
        self.say("Generating in the background. FL Studio Mobile opens when the track is ready; "
                 "you can leave psygen now.", "ok")
        return True

    def generate(self, settings: Settings) -> Job | None:
        if not self._ready(settings):
            return None

        def done(outcome: tasks.Outcome) -> list[tuple[str, str]]:
            if outcome.library is not None:
                self.library = outcome.library
            lines = tasks.summary(outcome)
            if self.cfg.open_flm_after_export and android.open_flm():
                lines.append(("Opened FL Studio Mobile: the track is in My Samples/psygen.", "ok"))
            return lines

        return self.run_job(
            "Generating",
            lambda report: tasks.generate(settings, self.cfg, self.library, report, client=self.client),
            on_done=done,
        )
