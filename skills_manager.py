"""Popup that lists a session's skills and updates or removes them with `npx skills`."""

import json
import os
import re
import select
import shutil
import signal
import sys
import termios
import threading
import time
import tty

from common import MOUSE_OFF, MOUSE_ON, STATE_DIR, WHEEL_DOWN, WHEEL_UP, Buttons, mouse, short_path, split_keys
from skills import UpdateCache, inventory, npx_skills

DATA = os.environ.get("AGENT_PANEL_SKILLS", "")

RESET = "\x1b[0m"
BOLD = "\x1b[1m"


def fg(n):
    return "\x1b[38;5;%dm" % n


MUTED = fg(245)
FAINT = fg(240)
YELLOW = fg(179)
GREEN = fg(78)
RED = fg(203)
BLUE = fg(69)
BUTTON = "\x1b[48;5;236m" + fg(252)
DANGER = "\x1b[48;5;88m" + fg(231) + BOLD
ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
GROUPS = (("local", "Local", GREEN), ("global", "Global", BLUE), ("builtin", "Built-in", MUTED))
MANAGERS = {
    "skills": "npx skills",
    "plugin": "Claude Code plugin · manage with /plugin",
    "synced": "synced from your claude.ai org · managed by your admin",
    None: "",
}


def visible(text):
    return len(ANSI.sub("", text))


def clip(text, width):
    return text if len(text) <= width else text[:max(width - 1, 0)] + "…"


class Manager:
    def __init__(self):
        with open(DATA) as f:
            data = json.load(f)
        self.cwd = data.get("cwd") or os.path.expanduser("~")
        self.names = data.get("names") or []
        self.calls = data.get("calls") or {}
        self.updates = UpdateCache(os.path.join(STATE_DIR, "skill-updates.json"))
        self.removed = set()
        self.selected = 0
        self.top = 0
        self.confirm = None  # item waiting for y/N before removal
        self.log = []
        self.status = ""
        self.checking = False
        self.line_items = []
        self.buttons = Buttons()
        self.badge_col = None
        self.rows_on_screen = 0
        self.reload()
        wanted = os.environ.get("AGENT_PANEL_SKILL")
        self.selected = next((n for n, i in enumerate(self.items) if i["name"] == wanted), 0)

    def reload(self):
        items = [i for i in inventory(self.names, self.cwd) if i["name"] not in self.removed]
        order = {g: n for n, (g, _, _) in enumerate(GROUPS)}
        items.sort(key=lambda i: (order[i["group"]], self.updates.status(i) != "update",
                                  i["manager"] != "skills", i["name"]))
        self.items = items
        self.selected = min(self.selected, len(items) - 1)

    @property
    def current(self):
        return self.items[self.selected] if self.items else None

    # ---- update checks -------------------------------------------------

    def check(self, force=False):
        if self.checking:
            return
        self.checking = True
        self.status = "Checking GitHub for updates…"

        def run():
            self.updates.refresh(self.items, force=force)
            self.checking = False
            outdated = sum(1 for i in self.items if self.updates.status(i) == "update")
            self.status = ("%d update%s available" % (outdated, "" if outdated == 1 else "s")
                           if outdated else "All npx skills installs are up to date")
            self.reload()
            os.kill(os.getpid(), signal.SIGWINCH)  # wake the loop to redraw

        threading.Thread(target=run, daemon=True).start()

    # ---- actions -------------------------------------------------------

    def run(self, action, items):
        for item in items:
            self.status = "Running npx skills %s %s…" % (action, item["name"])
            self.draw()
            ok, output = npx_skills(action, item)
            lines = [l for l in output.splitlines() if l.strip()]
            self.log = lines[-6:]
            if not ok:
                self.status = "%s failed for %s" % (action.capitalize(), item["name"])
                break
            self.status = "%s %s" % ("Removed" if action == "remove" else "Updated", item["name"])
            if action == "remove":
                # The same name can still exist in another scope; only hide it when nothing is left.
                left = inventory([item["name"]], self.cwd)[0]
                if left["group"] == "builtin":
                    self.removed.add(item["name"])
                else:
                    self.status += " · a %s copy is still installed" % left["group"]
        self.reload()

    def require_npx(self, item):
        if item["manager"] == "skills":
            return True
        if item["group"] == "builtin":
            self.status = "%s is built into Claude Code" % item["label"]
        else:
            self.status = "%s isn't installed with npx skills (%s)" % (item["label"], MANAGERS[item["manager"]])
        return False

    def click(self, col, row):
        """Returns "close" when the popup should close."""
        if row == self.rows_on_screen:
            kind, arg = self.buttons.hit(col - 1) or (None, None)
            if kind == "key":
                self.handle(arg)
            elif kind == "close":
                return "close"
            return None
        index = self.top + row - 3  # the list starts on the third screen row
        if self.confirm or not 0 <= index < len(self.line_items) or self.line_items[index] is None:
            return None
        n, badge_col = self.line_items[index]
        self.selected = n
        if badge_col and col >= badge_col and self.updates.status(self.items[n]) == "update":
            self.run("update", [self.items[n]])
        return None

    def handle(self, key):
        item = self.current
        event = mouse(key)
        if event:
            kind, col, row = event
            if kind == WHEEL_UP:
                self.selected = max(self.selected - 1, 0)
            elif kind == WHEEL_DOWN:
                self.selected = min(self.selected + 1, len(self.items) - 1)
            else:
                return self.click(col, row)
            return None
        if key.startswith("\x1b[<"):
            return None  # mouse release or drag
        if self.confirm:
            if key in ("y", "Y"):
                target, self.confirm = self.confirm, None
                self.run("remove", [target])
            else:
                self.confirm = None
                self.status = "Cancelled"
            return
        if key in ("j", "\x1b[B"):
            self.selected = min(self.selected + 1, len(self.items) - 1)
        elif key in ("k", "\x1b[A"):
            self.selected = max(self.selected - 1, 0)
        elif key == "g":
            self.selected = 0
        elif key == "G":
            self.selected = len(self.items) - 1
        elif key == "u" and item and self.require_npx(item):
            self.run("update", [item])
        elif key == "U":
            outdated = [i for i in self.items if self.updates.status(i) == "update"]
            if outdated:
                self.run("update", outdated)
            else:
                self.status = "Nothing to update"
        elif key == "x" and item and self.require_npx(item):
            self.confirm = item
        elif key == "r":
            self.check(force=True)

    # ---- drawing -------------------------------------------------------

    def item_lines(self, item, selected, width):
        state = self.updates.status(item)
        if item["name"] in self.removed:
            badge = RED + "removed" + RESET
        elif state == "update":
            badge = YELLOW + BOLD + "↑ update available" + RESET
        elif state == "current":
            badge = GREEN + "✓ up to date" + RESET
        elif state == "unknown":
            badge = MUTED + "not checked yet" + RESET
        else:
            badge = ""
        pointer = BLUE + BOLD + "›" + RESET if selected else " "
        name = (BOLD + BLUE if selected else "") + item["label"] + RESET
        tag = FAINT + "  " + item["detail"] + RESET if item["detail"] and item["detail"] != item["label"] else ""
        calls = self.calls.get(item["name"], 0)
        used = MUTED + "  · %d call%s" % (calls, "" if calls == 1 else "s") + RESET if calls else ""
        first = " %s %s%s%s" % (pointer, name, tag, used)
        pad = max(width - visible(first) - visible(badge) - 2, 1)
        lines = [first + " " * pad + badge]
        self.badge_col = width - visible(badge) if badge else None

        bits = []
        if item["path"]:
            bits.append(short_path(item["path"]))
        lock = item.get("lock") or {}
        if item["manager"] == "skills":
            bits.append("%s · %s" % (lock.get("source", "?"), "global" if item["scope"] == "-g" else "project"))
            if lock.get("updatedAt"):
                bits.append("updated " + lock["updatedAt"][:10])
        elif item["group"] == "builtin":
            bits.append("bundled with Claude Code")
        else:
            bits.append(MANAGERS[item["manager"]] or "copied in by hand · not tracked by npx skills")
        lines.append(FAINT + "     " + clip(" · ".join(bits), width - 6) + RESET)
        return lines

    def draw(self):
        cols, rows = shutil.get_terminal_size((100, 30))
        outdated = sum(1 for i in self.items if self.updates.status(i) == "update")
        head = " %sSkills%s  %s%s · %d skills" % (BOLD, RESET, MUTED, short_path(self.cwd), len(self.items))
        if outdated:
            head += RESET + "  " + YELLOW + BOLD + "↑ %d update%s available" % (outdated, "" if outdated == 1 else "s")
        out = ["\x1b[H", head + RESET + "\x1b[K\r\n\x1b[K\r\n"]

        body = []
        self.line_items = []  # for each body line: (item index, column where its badge starts) or None
        selected_line = 0
        last_group = None
        for n, item in enumerate(self.items):
            if item["group"] != last_group:
                last_group = item["group"]
                title, color = next((t, c) for g, t, c in GROUPS if g == last_group)
                count = sum(1 for i in self.items if i["group"] == last_group)
                if body:
                    body.append("")
                    self.line_items.append(None)
                body.append(" %s%s%s %s%d%s" % (color, BOLD + title, RESET, MUTED, count, RESET))
                self.line_items.append(None)
            if n == self.selected:
                selected_line = len(body)
            lines = self.item_lines(item, n == self.selected, cols)
            body.extend(lines)
            self.line_items += [(n, self.badge_col)] + [(n, None)] * (len(lines) - 1)

        footer_rows = 2 + len(self.log)
        view_rows = max(rows - 2 - footer_rows, 3)
        if selected_line < self.top:
            self.top = max(selected_line - 1, 0)
        elif selected_line + 1 >= self.top + view_rows:
            self.top = selected_line + 2 - view_rows
        for line in body[self.top:self.top + view_rows]:
            out.append(line + "\x1b[K\r\n")
        out.append("\x1b[J\x1b[%d;1H" % (rows - footer_rows + 1))
        for line in self.log:
            out.append(FAINT + "   " + clip(line, cols - 4) + RESET + "\x1b[K\r\n")
        self.buttons = Buttons(gap=" ")
        if self.confirm:
            item = self.confirm
            out.append(RED + BOLD + " Remove %s from %s? " % (item["name"], short_path(item["path"] or "?"))
                       + RESET + "y/N" + "\x1b[K\r\n")
            self.buttons.add(" Yes, remove ", ("key", "y"), DANGER)
            self.buttons.add(" Cancel ", ("key", "n"), BUTTON)
        else:
            out.append(" " + (YELLOW if "update" in self.status or "Running" in self.status else MUTED)
                       + clip(self.status, cols - 2) + RESET + "\x1b[K\r\n")
        if not self.confirm:
            self.buttons.add(" update ", ("key", "u"), BUTTON)
            self.buttons.add(" update all ", ("key", "U"), BUTTON)
            self.buttons.add(" remove ", ("key", "x"), BUTTON)
            self.buttons.add(" recheck ", ("key", "r"), BUTTON)
            self.buttons.add(" close ", ("close", None), BUTTON)
        out.append(" " + self.buttons.text() + "  " + MUTED + "j/k · u · U · x · r · q" + RESET + "\x1b[K")
        self.rows_on_screen = rows
        sys.stdout.write("".join(out))
        sys.stdout.flush()


def main():
    if not DATA or not os.path.exists(DATA):
        print("Open this from the agent panel with s.")
        select.select([sys.stdin], [], [], 3)
        return 1
    manager = Manager()
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    signal.signal(signal.SIGWINCH, lambda *_: None)
    sys.stdout.write("\x1b[?1049h\x1b[?25l" + MOUSE_ON)
    try:
        tty.setcbreak(fd)
        opened = time.time()
        manager.check()
        while True:
            manager.draw()
            try:
                ready, _, _ = select.select([fd], [], [], 1.0)
            except InterruptedError:
                continue
            if not ready:
                continue
            data = os.read(fd, 1024).decode(errors="ignore")
            if time.time() - opened < 0.4:
                continue  # herdr sends a stray Esc as the popup takes focus
            keys = split_keys(data)
            if not manager.confirm and ("q" in keys or "\x1b" in keys):
                break
            if "close" in [manager.handle(key) for key in keys]:
                break
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)
        sys.stdout.write(MOUSE_OFF + "\x1b[?25h\x1b[?1049l")
        sys.stdout.flush()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        pass
