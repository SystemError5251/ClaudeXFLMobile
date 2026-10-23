"""The curses front end: reads keys and taps, draws what the controller says.

curses ships with Python (Termux's ``python`` package included), so the UI
needs nothing to compile. Tapping a line in Termux selects it; tapping the
selected line again acts like enter.
"""

from __future__ import annotations

import locale
import os
import shutil
import subprocess
import sys
import time

from .widgets import Line

POLL_MS = 120

# style name -> (color pair number, curses attribute names)
STYLES = {
    "normal": (0, ()),
    "title": (1, ("A_BOLD",)),
    "dim": (2, ()),
    "ok": (3, ()),
    "warn": (4, ()),
    "error": (5, ("A_BOLD",)),
    "button": (1, ("A_BOLD",)),
    "selected": (6, ("A_BOLD",)),
    "bar": (6, ()),
}


def key_name(ch) -> str | None:
    """Turn what ``get_wch`` returns into the key names the screens use."""
    import curses

    if isinstance(ch, str):
        if ch in ("\n", "\r"):
            return "enter"
        if ch == "\x1b":
            return "back"
        if ch == "\t":
            return "tab"
        if ch in ("\x7f", "\x08"):
            return "backspace"
        return ch if ch.isprintable() else None
    names = {
        curses.KEY_UP: "up", curses.KEY_DOWN: "down", curses.KEY_LEFT: "left",
        curses.KEY_RIGHT: "right", curses.KEY_ENTER: "enter", curses.KEY_BACKSPACE: "backspace",
        curses.KEY_DC: "delete", curses.KEY_HOME: "home", curses.KEY_END: "end",
        curses.KEY_PPAGE: "pageup", curses.KEY_NPAGE: "pagedown", curses.KEY_BTAB: "up",
        curses.KEY_RESIZE: "resize", curses.KEY_MOUSE: "mouse",
    }
    return names.get(ch)


class CursesApp:
    def __init__(self, controller):
        self.c = controller
        self.scroll = 0
        self.last_focus: tuple | None = None
        self.content_top = 1
        self.shown: list[Line] = []

    # -- drawing ------------------------------------------------------------

    def attr(self, style: str) -> int:
        import curses

        pair, flags = STYLES.get(style, STYLES["normal"])
        value = curses.color_pair(pair) if self.colors and pair else 0
        if style in ("selected", "bar") and not self.colors:
            value |= curses.A_REVERSE
        for flag in flags:
            value |= getattr(curses, flag)
        return value

    def put(self, win, y: int, text: str, style: str, width: int) -> None:
        import curses

        try:
            win.addnstr(y, 0, text.ljust(width) if style in ("selected", "bar") else text, width, self.attr(style))
        except curses.error:  # writing the bottom-right cell raises; harmless
            pass

    def draw(self, win) -> None:
        import curses

        height, width = win.getmaxyx()
        win.erase()
        if height < 5 or width < 20:
            self.put(win, 0, "Window too small", "warn", width)
            win.refresh()
            return
        c = self.c
        self.put(win, 0, f" psygen | {c.screen.title}", "bar", width)
        lines = c.render(width - 1)
        rows = height - 3
        focus = next((i for i, line in enumerate(lines) if line.item == c.screen.selected), None)
        key = (id(c.screen), c.screen.selected, focus)
        if focus is not None and key != self.last_focus:
            if focus < self.scroll:
                self.scroll = max(0, focus - 1)
            elif focus >= self.scroll + rows:
                self.scroll = focus - rows + 1
            self.last_focus = key
        self.scroll = max(0, min(self.scroll, max(0, len(lines) - rows)))
        self.shown = lines[self.scroll:self.scroll + rows]
        cursor = None
        for y, line in enumerate(self.shown):
            self.put(win, y + 1, line.text, line.style, width - 1)
            if line.cursor is not None:
                cursor = (y + 1, min(line.cursor, width - 2))
        if self.scroll > 0:
            self.put_corner(win, 1, width, "^")
        if self.scroll + rows < len(lines):
            self.put_corner(win, rows, width, "v")
        status, style = c.status
        self.put(win, height - 2, status, style, width)
        self.put(win, height - 1, c.screen.hints, "dim", width)
        try:
            if cursor:
                curses.curs_set(1)
                win.move(*cursor)
            else:
                curses.curs_set(0)
        except curses.error:
            pass
        win.refresh()

    def put_corner(self, win, y: int, width: int, mark: str) -> None:
        import curses

        try:
            win.addstr(y, width - 1, mark, self.attr("dim"))
        except curses.error:
            pass

    # -- input --------------------------------------------------------------

    def tap(self, y: int) -> None:
        index = y - 1
        if 0 <= index < len(self.shown) and self.shown[index].item is not None:
            self.c.tap(self.shown[index].item)

    def run(self, win) -> None:
        import curses

        self.colors = False
        try:
            if curses.has_colors():
                curses.start_color()
                curses.use_default_colors()
                for pair, (fg, bg) in {
                    1: (curses.COLOR_CYAN, -1), 2: (curses.COLOR_WHITE, -1), 3: (curses.COLOR_GREEN, -1),
                    4: (curses.COLOR_YELLOW, -1), 5: (curses.COLOR_RED, -1), 6: (curses.COLOR_BLACK, curses.COLOR_CYAN),
                }.items():
                    curses.init_pair(pair, fg, bg)
                self.colors = True
        except curses.error:
            self.colors = False
        try:
            curses.mousemask(curses.BUTTON1_CLICKED | curses.BUTTON1_PRESSED)
            curses.mouseinterval(0)
        except (curses.error, AttributeError):
            pass
        win.keypad(True)
        win.timeout(POLL_MS)
        dirty = True
        while self.c.running:
            if dirty:
                self.draw(win)
            try:
                ch = win.get_wch()
            except curses.error:  # timeout, no key
                ch = None
            except KeyboardInterrupt:
                ch = "\x1b"
            key = key_name(ch) if ch is not None else None
            dirty = self.c.tick()
            if key is None:
                continue
            dirty = True
            if key == "mouse":
                try:
                    _, _, y, _, state = curses.getmouse()
                except curses.error:
                    continue
                if state & (curses.BUTTON1_CLICKED | curses.BUTTON1_PRESSED):
                    self.tap(y)
            elif key == "resize":
                if hasattr(curses, "update_lines_cols"):
                    curses.update_lines_cols()
            else:
                self.c.handle(key)


def first_start_storage_setup(cfg, run=subprocess.run, sleep=time.sleep, wait_seconds: float = 60) -> None:
    """On the first start in Termux, run ``termux-setup-storage`` so psygen
    can read samples and write exports in internal storage. Only once: the
    config remembers it, whatever the user answers in Android's dialog."""
    from .. import config

    if cfg.storage_setup_done or not config.is_termux():
        return
    cfg.storage_setup_done = True
    try:
        cfg.save()
    except OSError:
        pass
    if config.storage_ready():
        return
    if shutil.which("termux-setup-storage") is None:
        print("termux-setup-storage not found; psygen can't reach internal storage.")
        return
    print("First start: psygen needs access to your internal storage (samples and FLM User Files).")
    print("Allow it in the Android dialog that opens now.")
    try:
        run(["termux-setup-storage"])
    except OSError as exc:
        print(f"Could not run termux-setup-storage: {exc}")
        return
    waited = 0.0
    while not config.storage_ready() and waited < wait_seconds:
        sleep(1)
        waited += 1
    if config.storage_ready():
        print("Storage access granted.")
    else:
        print("No storage access yet. Run termux-setup-storage yourself later, then restart Termux.")
        sleep(2)


def run() -> int:
    """Start the UI. Returns an exit code for the CLI."""
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        print("psygen ui needs a terminal. Use the other psygen commands in scripts.", file=sys.stderr)
        return 1
    try:
        import curses
    except ImportError:
        print("Python's curses module is missing (on Termux: pkg install python ncurses).", file=sys.stderr)
        return 1
    from .. import config
    from .screens import Controller

    cfg = config.Config.load()
    first_start_storage_setup(cfg)
    locale.setlocale(locale.LC_ALL, "")
    os.environ.setdefault("ESCDELAY", "25")  # esc reacts at once instead of after 1 s
    controller = Controller(cfg)
    curses.wrapper(CursesApp(controller).run)
    return 0
