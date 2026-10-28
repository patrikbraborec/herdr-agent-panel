"""Popup file viewer with line numbers and light syntax highlighting."""

import json
import os
import re
import select
import shutil
import signal
import sys
import termios
import time
import tty
from datetime import datetime

from common import MOUSE_OFF, MOUSE_ON, WHEEL_DOWN, WHEEL_UP, Buttons, mouse, split_keys

PATH = os.environ.get("AGENT_PANEL_FILE", "")
DATA = os.environ.get("AGENT_PANEL_DATA", "")

RESET = "\x1b[0m"
BOLD = "\x1b[1m"


def fg(n):
    return "\x1b[38;5;%dm" % n


GUTTER = fg(240)
MUTED = fg(245)
COLORS = {
    "comment": fg(244),
    "string": fg(114),
    "keyword": fg(176),
    "number": fg(209),
    "key": fg(75),
    "deco": fg(179),
    "heading": BOLD + fg(75),
    "code": fg(179),
}

STRING = r"""(?P<string>"(?:[^"\\]|\\.)*"?|'(?:[^'\\]|\\.)*'?)"""
JS_STRING = r"""(?P<string>"(?:[^"\\]|\\.)*"?|'(?:[^'\\]|\\.)*'?|`(?:[^`\\]|\\.)*`?)"""
NUMBER = r"(?P<number>\b\d[\d_.]*\b)"


def words(*ws):
    return r"(?P<keyword>\b(?:%s)\b)" % "|".join(ws)


PY_KW = words("def", "class", "return", "if", "elif", "else", "for", "while", "in", "not", "and",
              "or", "is", "import", "from", "as", "with", "try", "except", "finally", "raise",
              "pass", "break", "continue", "lambda", "yield", "None", "True", "False", "self",
              "async", "await", "global", "nonlocal")
JS_KW = words("const", "let", "var", "function", "return", "if", "else", "for", "while", "of",
              "in", "new", "class", "extends", "import", "from", "export", "default", "async",
              "await", "try", "catch", "finally", "throw", "null", "undefined", "true", "false",
              "this", "type", "interface", "typeof", "switch", "case", "break")
SH_KW = words("if", "then", "else", "elif", "fi", "for", "in", "do", "done", "while", "case",
              "esac", "function", "return", "local", "export", "echo")

LANGS = {
    "python": [r"(?P<comment>#.*)", r"(?P<deco>^\s*@[\w.]+)", STRING, PY_KW, NUMBER],
    "js": [r"(?P<comment>//.*)", JS_STRING, JS_KW, NUMBER],
    "shell": [r"(?P<comment>(?<!\S)#.*)", STRING, r"(?P<key>\$\{?\w+\}?)", SH_KW, NUMBER],
    "config": [r"(?P<comment>(?<!\S)#.*)", r"(?P<deco>^\s*\[\[?[^\]]*\]\]?)",
               r"(?P<key>^\s*[\w.\"-]+(?=\s*[=:]))", STRING, words("true", "false", "null"), NUMBER],
    "json": [r'(?P<key>"(?:[^"\\]|\\.)*"(?=\s*:))', STRING, words("true", "false", "null"), NUMBER],
    "markdown": [r"(?P<heading>^#{1,6} .*)", r"(?P<code>`[^`]*`)", r"(?P<keyword>\*\*[^*]+\*\*)"],
}
EXTENSIONS = {
    ".py": "python", ".js": "js", ".mjs": "js", ".cjs": "js", ".ts": "js", ".tsx": "js",
    ".jsx": "js", ".sh": "shell", ".bash": "shell", ".zsh": "shell", ".toml": "config",
    ".yaml": "config", ".yml": "config", ".ini": "config", ".cfg": "config", ".env": "config",
    ".json": "json", ".md": "markdown", ".mdx": "markdown",
}
PATTERNS = {name: re.compile("|".join(parts)) for name, parts in LANGS.items()}


def highlight(text, lang):
    pattern = PATTERNS.get(lang)
    if not pattern:
        return text
    return pattern.sub(lambda m: COLORS[m.lastgroup] + m.group() + RESET, text)


def tilde(path):
    home = os.path.expanduser("~")
    return "~" + path[len(home):] if path.startswith(home) else path


ADD = "\x1b[48;5;22m" + fg(157)
DEL = "\x1b[48;5;52m" + fg(217)
HUNK = fg(75)
CHANGE = BOLD + fg(179)
TAB_ON = "\x1b[7m" + BOLD  # reverse video marks the open file
TAB_OFF = MUTED
BUTTON = "\x1b[48;5;236m" + fg(252)


def load(path):
    with open(path, "rb") as f:
        data = f.read()
    if b"\0" in data[:8192]:
        return None
    return data.decode("utf-8", errors="replace").expandtabs(4).splitlines()


def load_files():
    """Files the panel handed us, newest first, each with its recorded diffs."""
    try:
        with open(DATA) as f:
            files = json.load(f).get("files") or []
    except (OSError, ValueError):
        files = []
    if not any(f["path"] == PATH for f in files):
        files.insert(0, {"path": PATH, "kind": "edited", "diffs": []})
    return files


def clock(timestamp):
    try:
        when = datetime.strptime(timestamp[:19], "%Y-%m-%dT%H:%M:%S")
        offset = datetime.now() - datetime.utcnow()
        return (when + offset).strftime("%H:%M:%S")
    except (TypeError, ValueError):
        return ""


class Viewer:
    """Rows are (line number, style, text). Style is None for plain code."""

    def __init__(self, files, path):
        self.files = files
        self.index = next((i for i, f in enumerate(files) if f["path"] == path), 0)
        self.mode = "diff" if self.current["diffs"] else "file"
        self.top = 0
        self.left = 0
        self.build()

    @property
    def current(self):
        return self.files[self.index]

    def build(self):
        path = self.current["path"]
        self.lang = EXTENSIONS.get(os.path.splitext(path)[1].lower())
        self.marks = []  # row index of each change header, for { and }
        self.rows = []
        if self.mode == "file":
            try:
                lines = load(path)
            except OSError:
                lines = []
                self.rows.append(("", MUTED, "File no longer exists."))
            if lines is None:
                self.rows.append(("", MUTED, "Can't preview binary files."))
            for n, line in enumerate(lines or []):
                self.rows.append((str(n + 1), None, line))
            return
        diffs = self.current["diffs"]
        if not diffs:
            self.rows.append(("", MUTED, "No recorded changes. Press d to view the file."))
        for i, change in enumerate(diffs):
            if self.rows:
                self.rows.append(("", None, ""))
            self.marks.append(len(self.rows))
            label = "%s %d/%d · %s · %s" % (
                "Change", i + 1, len(diffs), change["kind"], clock(change.get("time")))
            self.rows.append(("", CHANGE, label))
            for hunk in change["hunks"]:
                old, new = hunk.get("oldStart", 0), hunk.get("newStart", 0)
                self.rows.append(("", HUNK, "@@ -%d,%d +%d,%d @@" % (
                    old, hunk.get("oldLines", 0), new, hunk.get("newLines", 0))))
                for line in hunk.get("lines") or []:
                    sign, text = line[:1], line[1:].expandtabs(4)
                    if sign == "+":
                        self.rows.append((str(new), ADD, "+" + text))
                        new += 1
                    elif sign == "-":
                        self.rows.append((str(old), DEL, "-" + text))
                        old += 1
                    else:
                        self.rows.append((str(new), None, " " + text))
                        old += 1
                        new += 1

    def switch_file(self, step):
        if not step:
            return
        self.index = (self.index + step) % len(self.files)
        self.mode = "diff" if self.current["diffs"] else "file"
        self.top = self.left = 0
        self.build()

    def reload(self, files):
        path = self.current["path"]
        self.files = files
        self.index = next((i for i, f in enumerate(files) if f["path"] == path), 0)
        self.build()

    def tabs(self, cols):
        """File tabs around the current one, as clickable buttons."""
        labels = [" %s " % os.path.basename(f["path"]) for f in self.files]
        lo = hi = self.index
        used = len(labels[lo]) + 4  # room for the ‹ › scroll markers
        while True:
            grew = False
            if hi + 1 < len(labels) and used + len(labels[hi + 1]) + 1 <= cols:
                hi += 1
                used += len(labels[hi]) + 1
                grew = True
            if lo > 0 and used + len(labels[lo - 1]) + 1 <= cols:
                lo -= 1
                used += len(labels[lo]) + 1
                grew = True
            if not grew:
                break
        tabs = Buttons(gap=" ")
        tabs.add("‹" if lo > 0 else " ", ("file", -1) if lo > 0 else None, MUTED)
        for i in range(lo, hi + 1):
            style = TAB_ON if i == self.index else TAB_OFF
            tabs.add(labels[i], ("goto", i), style)
        if hi + 1 < len(labels):
            tabs.add("›", ("file", 1), MUTED)
        return tabs

    def footer(self):
        buttons = Buttons(gap=" ")
        buttons.add(" ◂ file ", ("file", -1), BUTTON)
        buttons.add(" file ▸ ", ("file", 1), BUTTON)
        if self.mode == "diff" and len(self.marks) > 1:
            buttons.add(" ◂ change ", ("key", "{"), BUTTON)
            buttons.add(" change ▸ ", ("key", "}"), BUTTON)
        buttons.add(" show file " if self.mode == "diff" else " show diff ", ("key", "d"), BUTTON)
        buttons.add(" edit ", ("edit", None), BUTTON)
        buttons.add(" close ", ("close", None), BUTTON)
        return buttons

    def draw(self):
        cols, rows = shutil.get_terminal_size((80, 24))
        body_rows = rows - 4
        f = self.current
        changes = len(f["diffs"])
        title = " %s%s%s  %s%s · %s · %d change%s%s" % (
            BOLD, os.path.basename(f["path"]), RESET, GUTTER, tilde(os.path.dirname(f["path"])),
            self.mode, changes, "" if changes == 1 else "s", RESET)
        self.tab_bar = self.tabs(cols - 1)
        out = ["\x1b[H", title, "\x1b[K\r\n", " " + self.tab_bar.text() + "\x1b[K\r\n\x1b[K\r\n"]

        digits = max(len(r[0]) for r in self.rows) if self.rows else 1
        text_width = max(cols - digits - 3, 10)
        self.top = max(0, min(self.top, len(self.rows) - body_rows))
        for number, style, text in self.rows[self.top:self.top + body_rows]:
            text = text[self.left:self.left + text_width]
            gutter = GUTTER + number.rjust(digits) + " │ " + RESET
            if style is None:
                body = highlight(text, self.lang)
            elif style in (ADD, DEL):
                body = style + text.ljust(text_width) + RESET
            else:
                body = style + text + RESET
            out.append(gutter + body + "\x1b[K\r\n")
        self.buttons = self.footer()
        out.append("\x1b[J\x1b[%d;1H" % rows)
        out.append(" " + self.buttons.text() + "  " + MUTED + "tab · { } · d · e · q" + RESET + "\x1b[K")
        self.rows_on_screen = rows
        sys.stdout.write("".join(out))
        sys.stdout.flush()

    def click(self, col, row):
        """Returns "close", "edit" or None."""
        if row == 2:
            action = self.tab_bar.hit(col - 1)
        elif row == self.rows_on_screen:
            action = self.buttons.hit(col - 1)
        else:
            return None
        kind, arg = action or (None, None)
        if kind == "goto":
            self.switch_file(arg - self.index)
        elif kind == "file":
            self.switch_file(arg)
        elif kind == "key":
            self.handle(arg)
        elif kind in ("close", "edit"):
            return kind
        return None

    def handle(self, key):
        rows = shutil.get_terminal_size((80, 24)).lines - 4
        event = mouse(key)
        if event:
            kind, col, row = event
            if kind == WHEEL_UP:
                self.top = max(self.top - 3, 0)
            elif kind == WHEEL_DOWN:
                self.top += 3
            else:
                return self.click(col, row)
        elif key in ("j", "\x1b[B", "\r"):
            self.top += 1
        elif key in ("k", "\x1b[A"):
            self.top = max(self.top - 1, 0)
        elif key in (" ", "f", "\x1b[6~"):
            self.top += rows - 1
        elif key in ("b", "\x1b[5~"):
            self.top = max(self.top - rows + 1, 0)
        elif key == "g":
            self.top = 0
        elif key == "G":
            self.top = len(self.rows)
        elif key in ("l", "\x1b[C"):
            self.left += 8
        elif key in ("h", "\x1b[D"):
            self.left = max(self.left - 8, 0)
        elif key in ("\t", "n", "]"):
            self.switch_file(1)
        elif key in ("\x1b[Z", "p", "["):
            self.switch_file(-1)
        elif key == "d":
            self.mode = "file" if self.mode == "diff" else "diff"
            self.top = 0
            self.build()
        elif key == "}":
            self.top = next((m for m in self.marks if m > self.top), self.top)
        elif key == "{":
            self.top = next((m for m in reversed(self.marks) if m < self.top), 0)


def mtime(path):
    try:
        return os.path.getmtime(path)
    except OSError:
        return None


def main():
    if not PATH:
        print("Nothing to preview.")
        select.select([sys.stdin], [], [], 3)
        return 1
    viewer = Viewer(load_files(), PATH)
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    signal.signal(signal.SIGWINCH, lambda *_: viewer.draw())
    edit = False
    seen = (mtime(DATA), mtime(viewer.current["path"]))
    sys.stdout.write("\x1b[?1049h\x1b[?25l" + MOUSE_ON)
    try:
        tty.setcbreak(fd)
        opened = time.time()
        while True:
            viewer.draw()
            ready, _, _ = select.select([fd], [], [], 1.0)
            if not ready:
                # Follow the agent: pick up new changes while the popup is open.
                now = (mtime(DATA), mtime(viewer.current["path"]))
                if now != seen:
                    seen = now
                    viewer.reload(load_files())
                continue
            data = os.read(fd, 1024).decode(errors="ignore")
            if time.time() - opened < 0.4:
                continue  # herdr sends a stray Esc as the popup takes focus
            keys = split_keys(data)
            if "q" in keys or "\x1b" in keys:
                break
            results = [viewer.handle(key) for key in keys]
            if "e" in keys or "edit" in results:
                edit = True
                break
            if "close" in results:
                break
            seen = (mtime(DATA), mtime(viewer.current["path"]))
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)
        sys.stdout.write(MOUSE_OFF + "\x1b[?25h\x1b[?1049l")
        sys.stdout.flush()
    if edit:
        editor = os.environ.get("VISUAL") or os.environ.get("EDITOR") or "vi"
        os.execvp("sh", ["sh", "-c", editor + ' "$1"', "sh", viewer.current["path"]])
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        pass
