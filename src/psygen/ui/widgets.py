"""Form fields that know nothing about curses.

Keys arrive as names: a single printable character, or one of ``up``,
``down``, ``left``, ``right``, ``enter``, ``back``, ``backspace``,
``delete``, ``home``, ``end``, ``tab``, ``pageup``, ``pagedown``.
A field's :meth:`Field.handle` returns ``None`` when it ignored the key,
:data:`CHANGED` when it used it, or an action name for the screen.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

CHANGED = "changed"
LABEL_WIDTH = 10


@dataclass
class Line:
    text: str
    style: str = "normal"  # normal, title, dim, ok, warn, error, selected, button
    item: int | None = None  # index of the selectable item on this line
    cursor: int | None = None  # column of the text cursor, when editing here


class LineEdit:
    """A one-line text editor."""

    def __init__(self, text: str = "", mask: bool = False, limit: int = 500):
        self.text = text
        self.cursor = len(text)
        self.mask = mask
        self.limit = limit

    def set(self, text: str) -> None:
        self.text = text
        self.cursor = len(text)

    def handle(self, key: str) -> bool:
        t, c = self.text, self.cursor
        if key == "left":
            self.cursor = max(0, c - 1)
        elif key == "right":
            self.cursor = min(len(t), c + 1)
        elif key == "home":
            self.cursor = 0
        elif key == "end":
            self.cursor = len(t)
        elif key == "backspace":
            if c > 0:
                self.text, self.cursor = t[: c - 1] + t[c:], c - 1
        elif key == "delete":
            self.text = t[:c] + t[c + 1:]
        elif len(key) == 1 and key.isprintable():
            if len(t) < self.limit:
                self.text, self.cursor = t[:c] + key + t[c:], c + 1
        else:
            return False
        return True

    def view(self, width: int) -> tuple[str, int]:
        """The visible slice of the text and the cursor column inside it."""
        shown = "*" * len(self.text) if self.mask else self.text
        width = max(1, width)
        start = max(0, self.cursor - width + 1)
        return shown[start:start + width], self.cursor - start


class Field:
    selectable = True

    def __init__(self, label: str):
        self.label = label

    def handle(self, key: str) -> str | None:
        return None

    def lines(self, width: int, selected: bool) -> list[Line]:
        return [Line(_row(self.label, self.value_text(), width), "selected" if selected else "normal")]

    def value_text(self) -> str:
        return ""


def _row(label: str, value: str, width: int) -> str:
    if not label:
        return value[:width]
    return f"{label:<{LABEL_WIDTH}} {value}"[:width]


class Static(Field):
    """Text that can't be selected (headings, hints, status)."""

    selectable = False

    def __init__(self, text: str = "", style: str = "normal", label: str = ""):
        super().__init__(label)
        self.text = text
        self.style = style

    def lines(self, width: int, selected: bool) -> list[Line]:
        if self.label:
            return [Line(_row(self.label, self.text, width), self.style)]
        return [Line(chunk, self.style) for chunk in wrap(self.text, width)]


class Choice(Field):
    """Cycles through options with left/right (or enter/space)."""

    def __init__(self, label: str, options: Sequence[tuple[str, str]], value: str = ""):
        super().__init__(label)
        self.options = list(options)
        values = [v for v, _ in self.options]
        self.index = values.index(value) if value in values else 0

    @property
    def value(self) -> str:
        return self.options[self.index][0]

    def value_text(self) -> str:
        return f"< {self.options[self.index][1]} >"

    def handle(self, key: str) -> str | None:
        if key in ("right", "enter", " "):
            self.index = (self.index + 1) % len(self.options)
        elif key == "left":
            self.index = (self.index - 1) % len(self.options)
        else:
            return None
        return CHANGED


class Toggle(Field):
    def __init__(self, label: str, value: bool = False, text: str = ""):
        super().__init__(label)
        self.value = value
        self.text = text

    def value_text(self) -> str:
        return ("[x] " if self.value else "[ ] ") + self.text

    def handle(self, key: str) -> str | None:
        if key in ("enter", " ", "left", "right"):
            self.value = not self.value
            return CHANGED
        return None


class Text(Field):
    """Typing goes straight into the field while it is selected."""

    def __init__(self, label: str, value: str = "", placeholder: str = "", mask: bool = False):
        super().__init__(label)
        self.edit = LineEdit(value, mask=mask)
        self.placeholder = placeholder

    @property
    def value(self) -> str:
        return self.edit.text

    def handle(self, key: str) -> str | None:
        if key == "enter":
            return "next"
        return CHANGED if self.edit.handle(key) else None

    def lines(self, width: int, selected: bool) -> list[Line]:
        offset = LABEL_WIDTH + 1 if self.label else 0
        room = max(1, width - offset - 1)
        if not self.edit.text:
            line = Line(_row(self.label, self.placeholder, width), "selected" if selected else "dim")
            if selected:
                line.cursor = offset
            return [line]
        shown, col = self.edit.view(room)
        line = Line(_row(self.label, shown, width), "selected" if selected else "normal")
        if selected:
            line.cursor = offset + col
        return [line]


class Button(Field):
    def __init__(self, label: str, action: str, style: str = "button"):
        super().__init__(label)
        self.action = action
        self.style = style

    def handle(self, key: str) -> str | None:
        return self.action if key == "enter" else None

    def lines(self, width: int, selected: bool) -> list[Line]:
        return [Line(f"[ {self.label} ]"[:width], "selected" if selected else self.style)]


def wrap(text: str, width: int) -> list[str]:
    """Word wrap that keeps empty lines and indentation, and cuts words
    longer than a line (long paths)."""
    width = max(1, width)
    out: list[str] = []
    for paragraph in text.split("\n"):
        body = paragraph.lstrip(" ")
        indent = " " * min(len(paragraph) - len(body), width // 2)
        room = width - len(indent)
        line = ""
        for word in body.split(" "):
            while len(word) > room:
                if line:
                    out.append(indent + line)
                    line = ""
                out.append(indent + word[:room])
                word = word[room:]
            if not line:
                line = word
            elif len(line) + 1 + len(word) <= room:
                line += " " + word
            else:
                out.append(indent + line)
                line = word
        out.append(indent + line if line else "")
    return out
