"""Shared helpers for talking to herdr and tracking open panels."""

import json
import os
import re
import subprocess

HERDR = os.environ.get("HERDR_BIN_PATH", "herdr")
PLUGIN_ID = os.environ.get("HERDR_PLUGIN_ID", "braborec.agent-panel")
STATE_DIR = os.environ.get("HERDR_PLUGIN_STATE_DIR") or os.path.expanduser(
    "~/.local/state/herdr-agent-panel"
)
PANELS_FILE = os.path.join(STATE_DIR, "panels.json")


def herdr(*args, check=True):
    proc = subprocess.run(
        [HERDR, *args], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
    )
    if check and proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or "herdr %s failed" % " ".join(args))
    return proc.stdout


def snapshot():
    data = json.loads(herdr("api", "snapshot"))
    return data["result"]["snapshot"]


def load_panels():
    """Map of panel pane id -> agent pane id it is attached to."""
    try:
        with open(PANELS_FILE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_panels(panels):
    os.makedirs(STATE_DIR, exist_ok=True)
    tmp = PANELS_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(panels, f)
    os.replace(tmp, PANELS_FILE)


KEY = re.compile(r"\x1b\[<\d+;\d+;\d+[Mm]|\x1b\[[0-9;?]*[~A-Za-z]|\x1bO[A-Za-z]|\x1b|.", re.S)
MOUSE = re.compile(r"\x1b\[<(\d+);(\d+);(\d+)([Mm])")
MOUSE_ON = "\x1b[?1000h\x1b[?1006h"  # report presses, releases and the wheel, SGR encoded
MOUSE_OFF = "\x1b[?1000l\x1b[?1006l"
WHEEL_UP, WHEEL_DOWN = "wheel-up", "wheel-down"


def split_keys(data):
    """Split one terminal read into individual keys and escape sequences."""
    return KEY.findall(data)


LONG_ID = re.compile(r"^[0-9a-f-]{20,}(_[0-9a-f-]{20,})?$")


def short_path(path):
    """~ for home, and long generated folder names (synced plugin ids) shortened to …"""
    home = os.path.expanduser("~")
    if path.startswith(home):
        path = "~" + path[len(home):]
    return "/".join("…" if LONG_ID.match(part) else part for part in path.split("/"))


def mouse(key):
    """Turn an SGR mouse report into ("click", col, row) or a wheel event, 1-based cells.

    Releases, drags and other buttons return None.
    """
    m = MOUSE.match(key)
    if not m:
        return None
    button, col, row, kind = int(m.group(1)), int(m.group(2)), int(m.group(3)), m.group(4)
    if button & 64:  # wheel
        return (WHEEL_DOWN if button & 1 else WHEEL_UP, col, row)
    if kind == "M" and button & 3 == 0 and not button & 32:
        return ("click", col, row)
    return None


class Buttons:
    """A line of clickable labels. Remembers which columns belong to which action."""

    def __init__(self, gap="  "):
        self.parts = []
        self.ranges = []  # (start col, end col, action), 1-based inclusive
        self.col = 1
        self.gap = gap

    def add(self, label, action, style=""):
        if self.parts:
            self.parts.append(self.gap)
            self.col += len(self.gap)
        self.parts.append(style + label + ("\x1b[0m" if style else ""))
        self.ranges.append((self.col, self.col + len(label) - 1, action))
        self.col += len(label)

    def text(self):
        return "".join(self.parts)

    def hit(self, col):
        return next((action for start, end, action in self.ranges if start <= col <= end), None)
