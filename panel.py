"""Terminal panel that shows live details about one agent pane."""

import json
import os
import re
import select
import shutil
import signal
import socket
import subprocess
import sys
import termios
import threading
import time
import tty

from common import (MOUSE_OFF, MOUSE_ON, PLUGIN_ID, STATE_DIR, WHEEL_DOWN, WHEEL_UP, Buttons,
                    herdr, mouse, short_path, split_keys, load_panels, save_panels, snapshot)
from skills import UpdateCache, inventory
from transcript import Transcript, find_successor, find_transcript

SELF = os.environ.get("HERDR_PANE_ID")
TARGET = os.environ.get("AGENT_PANEL_TARGET")
CONFIG_DIR = os.environ.get("HERDR_PLUGIN_CONFIG_DIR", "")
PANEL_WIDTH = 46  # columns we try to resize to on open
SNAPSHOT_EVERY = 2.0

RESET = "\x1b[0m"
BOLD = "\x1b[1m"
DIM = "\x1b[2m"


def fg(n):
    return "\x1b[38;5;%dm" % n


BORDER = fg(240)
MUTED = fg(245)
FAINT = fg(238)
CATEGORY_COLORS = {"Messages": 69, "System tools": 209, "MCP tools": 43, "System prompt": 179}
STATUS_COLORS = {"working": 179, "blocked": 203, "done": 78, "idle": 245}
MODE_LABELS = {"bypassPermissions": "bypass", "acceptEdits": "accept edits", "plan": "plan"}
ANSI = re.compile(r"\x1b\[[0-9;]*m")


CARDS = ("Session", "Environment", "Context", "Connectors", "Skills", "Outputs")
FOLDS_FILE = os.path.join(STATE_DIR, "folds.json")


def load_folds():
    """Folded cards are remembered across panels and restarts."""
    try:
        with open(FOLDS_FILE) as f:
            return set(json.load(f)) & set(CARDS)
    except (OSError, ValueError, TypeError):
        return set()


def save_folds(folds):
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        with open(FOLDS_FILE, "w") as f:
            json.dump(sorted(folds), f)
    except OSError:
        pass


def load_config():
    try:
        with open(os.path.join(CONFIG_DIR, "config.json")) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def context_window(model, used, config):
    model = model or ""
    per_model = config.get("context_windows") or {}
    if model in per_model:
        return per_model[model]
    if config.get("context_window"):
        return config["context_window"]
    if "[1m]" in model or re.match(r"claude-(opus|sonnet|fable)-5", model) or used > 200_000:
        return 1_000_000
    return 200_000


def human(n):
    if n >= 1_000_000:
        return ("%.1fM" % (n / 1e6)).replace(".0M", "M")
    if n >= 1000:
        return "%.1fk" % (n / 1000)
    return str(n)


def visible_len(s):
    return len(ANSI.sub("", s))


def clip(s, width):
    """Truncate a string with ANSI codes to `width` visible columns."""
    if visible_len(s) <= width:
        return s
    out, n, i = [], 0, 0
    while i < len(s) and n < width - 1:
        m = ANSI.match(s, i)
        if m:
            out.append(m.group())
            i = m.end()
            continue
        out.append(s[i])
        n += 1
        i += 1
    return "".join(out) + "…" + RESET


def tilde(path):
    home = os.path.expanduser("~")
    return "~" + path[len(home):] if path and path.startswith(home) else (path or "")


def row(left, right, width):
    """Left text and right-aligned text on one line of `width` columns."""
    space = width - visible_len(right) - 1
    left = clip(left, max(space, 1))
    return left + " " * (width - visible_len(left) - visible_len(right)) + right


class GitInfo:
    def __init__(self):
        self.cache = {}

    def branch(self, cwd):
        now = time.time()
        hit = self.cache.get(cwd)
        if hit and now - hit[0] < 10:
            return hit[1]
        try:
            out = subprocess.run(
                ["git", "-C", cwd, "branch", "--show-current"],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, timeout=2,
            )
            branch = out.stdout.strip() if out.returncode == 0 else None
            if out.returncode == 0 and not branch:
                branch = "detached"
        except (OSError, subprocess.SubprocessError):
            branch = None
        self.cache[cwd] = (now, branch)
        return branch


class Panel:
    def __init__(self):
        self.pane = None
        self.session_id = None
        self.transcript = None
        self.git = GitInfo()
        self.config = load_config()
        self.scroll = 0
        self.last_frame = None
        self.last_snapshot = 0
        self.missing_since = None
        self.dirty = True
        self.outputs = []
        self.selected = 0
        self.selected_row = None
        self.follow_selection = False
        self.message = None
        self.last_successor_check = 0
        self.data_file = None
        self.skill_cache = None
        self.folds = load_folds()
        self.footer = None
        self.targets = []
        self.updates = UpdateCache(os.path.join(STATE_DIR, "skill-updates.json"))

    def select(self, delta):
        if self.outputs:
            self.selected = max(0, min(self.selected + delta, len(self.outputs) - 1))
            self.follow_selection = True

    def preview(self, index=None):
        index = self.selected if index is None else index
        if not 0 <= index < len(self.outputs):
            return
        self.selected = index
        path = self.outputs[index]
        if not os.path.exists(path) and path not in self.transcript.diffs:
            self.message = ("File no longer exists", time.time())
            return
        self.data_file = os.path.join(STATE_DIR, "preview-%s.json" % (SELF or TARGET).replace(":", "_"))
        self.write_preview_data()
        try:
            herdr(
                "plugin", "pane", "open",
                "--plugin", PLUGIN_ID,
                "--entrypoint", "preview",
                "--focus",
                "--env", "AGENT_PANEL_FILE=" + path,
                "--env", "AGENT_PANEL_DATA=" + self.data_file,
            )
        except RuntimeError as err:
            self.message = (str(err).splitlines()[-1][:60], time.time())

    def write_preview_data(self):
        """Hand the popup every output file and its diffs so it can switch between them."""
        t = self.transcript
        if not self.data_file or not t:
            return
        files = [
            {"path": path, "kind": info["kind"], "diffs": t.diffs.get(path, [])}
            for path, info in t.recent_outputs()
        ]
        tmp = self.data_file + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"cwd": (self.pane or {}).get("cwd"), "files": files}, f)
        os.replace(tmp, self.data_file)

    # ---- data ----------------------------------------------------------

    def refresh(self):
        now = time.time()
        if now - self.last_snapshot >= SNAPSHOT_EVERY:
            self.last_snapshot = now
            try:
                snap = snapshot()
            except (RuntimeError, ValueError, KeyError):
                snap = None
            if snap is not None:
                pane = next((p for p in snap["panes"] if p["pane_id"] == TARGET), None)
                if pane is None:
                    self.missing_since = self.missing_since or now
                else:
                    self.missing_since = None
                    if pane != self.pane:
                        self.pane = pane
                        self.dirty = True
                    self._attach_session((pane.get("agent_session") or {}).get("value"))
        if self.transcript and self.transcript.poll():
            self.dirty = True
            self.write_preview_data()
        self._follow_continuation(now)

    def _follow_continuation(self, now):
        """Switch to the newer file when the session continued in a new transcript."""
        t = self.transcript
        if not t or now - self.last_successor_check < 10:
            return
        self.last_successor_check = now
        try:
            if now - os.path.getmtime(t.path) < 10:
                return
        except OSError:
            return
        successor = find_successor(t.path, t.last_uuid)
        if successor:
            self.transcript = Transcript(successor)
            self.transcript.poll()
            self.dirty = True

    def _attach_session(self, session_id):
        if session_id == self.session_id and self.transcript:
            return
        path = find_transcript(session_id) if session_id else None
        if path:
            self.session_id = session_id
            self.transcript = Transcript(path)
            self.dirty = True

    def target_gone(self):
        return self.missing_since is not None and time.time() - self.missing_since > 4

    # ---- rendering -----------------------------------------------------

    def card(self, title, body, width, summary=""):
        """Lines of a card as (text, click action). Body lines may be (text, action) too.

        Clicking the title row folds the card down to its title and summary.
        """
        inner = width - 4
        folded = title in self.folds
        arrow = "▸" if folded else "▾"
        label = BOLD + title + RESET
        right = (" " + MUTED + summary + RESET + " ") if folded and summary else ""
        dashes = max(width - visible_len(title) - visible_len(right) - 8, 0)
        top = BORDER + "╭─ " + RESET + MUTED + arrow + RESET + " " + label + " " + BORDER + "─" * dashes
        top += RESET + right + BORDER + "─╮" + RESET
        lines = [(top, ("fold", title))]
        if not folded:
            for line in body:
                line, action = line if isinstance(line, tuple) else (line, None)
                line = clip(line, inner)
                pad = " " * (inner - visible_len(line))
                lines.append((BORDER + "│ " + RESET + line + pad + BORDER + " │" + RESET, action))
        lines.append((BORDER + "╰" + "─" * (width - 2) + "╯" + RESET, ("fold", title) if folded else None))
        return lines

    def toggle_fold(self, title=None):
        """Fold or unfold one card, or with no title fold all / unfold all."""
        if title:
            self.folds ^= {title}
        else:
            self.folds = set() if self.folds else set(CARDS)
        save_folds(self.folds)
        self.last_frame = None

    def session_card(self, width):
        p, t = self.pane or {}, self.transcript
        title = (t and t.title) or p.get("terminal_title_stripped") or p.get("pane_id") or "agent"
        status = p.get("agent_status") or "unknown"
        dot = fg(STATUS_COLORS.get(status, 245)) + "●" + RESET
        body = [BOLD + title + RESET]
        bits = [dot + " " + status]
        if t and (t.model_name or t.model):
            bits.append(t.model_name or t.model)
        elif p.get("agent"):
            bits.append(p["agent"])
        if t and t.permission_mode and t.permission_mode != "default":
            bits.append(MODE_LABELS.get(t.permission_mode, t.permission_mode))
        body.append((MUTED + " · " + RESET).join(bits))
        if t:
            body.append(MUTED + "%d prompts · %d tool calls" % (t.turns, t.tool_calls) + RESET)
        return self.card("Session", body, width, summary=status)

    def environment_card(self, width):
        p = self.pane or {}
        cwd = p.get("foreground_cwd") or p.get("cwd") or ""
        inner = width - 4
        body = [row(MUTED + "Folder" + RESET, tilde(cwd), inner)]
        branch = self.git.branch(cwd) if cwd else None
        if branch:
            body.append(row(MUTED + "Branch" + RESET, branch, inner))
        body.append(row(MUTED + "Machine" + RESET, socket.gethostname().split(".")[0], inner))
        body.append(row(MUTED + "Pane" + RESET, p.get("pane_id") or TARGET or "?", inner))
        summary = os.path.basename(cwd) or cwd
        t = self.transcript
        if t:
            body.append("")
            if t.sandbox:
                runs = fg(78) + "● sandbox" + RESET + MUTED + " · " + t.sandbox + RESET
                summary += " · sandbox"
            else:
                runs = fg(179) + "● locally" + RESET + MUTED + " · no sandbox" + RESET
                summary += " · local"
            body.append(row(MUTED + "Runs" + RESET, runs, inner))
            total = t.commands_sandboxed + t.commands_outside
            if total:
                commands = fg(78) + "%d in" % t.commands_sandboxed + RESET
                if t.commands_outside:
                    commands += MUTED + " · " + RESET + fg(179) + "%d outside" % t.commands_outside + RESET
                body.append(row(MUTED + "Commands" + RESET, commands, inner))
            if t.sandbox_denials:
                body.append(row(MUTED + "Blocked" + RESET, fg(203) + "%d sandbox denial%s" % (
                    t.sandbox_denials, "" if t.sandbox_denials == 1 else "s") + RESET, inner))
        return self.card("Environment", body, width, summary=summary)

    def context_card(self, width):
        t = self.transcript
        inner = width - 4
        if not t or not t.usage:
            return self.card("Context", [MUTED + "Waiting for the first response…" + RESET], width)
        used = t.context_used()
        window = context_window(t.model, used, self.config)
        parts = t.context_breakdown()
        pct = 100.0 * used / window if window else 0
        body = [row(BOLD + "%d%% used" % round(pct) + RESET, MUTED + "%s / %s" % (human(used), human(window)) + RESET, inner)]

        bar, filled = "", 0
        for name, tokens in parts:
            cells = int(round(inner * tokens / window)) if window else 0
            cells = min(cells, inner - filled)
            bar += fg(CATEGORY_COLORS[name]) + "━" * cells
            filled += cells
        bar += FAINT + "━" * (inner - filled) + RESET
        body.append(bar)
        body.append("")

        rows = [(n, v, CATEGORY_COLORS[n]) for n, v in parts]
        rows.append(("Free space", max(window - used, 0), 238))
        for name, tokens, color in rows:
            right = "%7s %6.1f%%" % (human(tokens), 100.0 * tokens / window if window else 0)
            body.append(row(fg(color) + "■" + RESET + " " + name, MUTED + right + RESET, inner))

        u = t.usage
        body.append("")
        body.append(row(MUTED + "Cached" + RESET, MUTED + human(u.get("cache_read_input_tokens") or 0) + RESET, inner))
        body.append(row(MUTED + "Last output" + RESET, MUTED + human(u.get("output_tokens") or 0) + RESET, inner))
        body.append(FAINT + "Category split is estimated" + RESET)
        return self.card("Context", body, width, summary="%d%% used" % round(pct))

    def connectors_card(self, width):
        t = self.transcript
        conns = t.connectors() if t else []
        ready = [c for c in conns if c[2] == "ready"]
        pending = len(conns) - len(ready)
        if not conns:
            return self.card("Connectors", [MUTED + "No MCP servers" + RESET], width)
        inner = width - 4
        body = []
        limit = self.config.get("max_connectors", 8)
        for label, origin, _, calls in ready[:limit]:
            badge = FAINT + "[" + label[:1].upper() + "]" + RESET
            right = MUTED + "%d call%s" % (calls, "" if calls == 1 else "s") + RESET if calls else ""
            left = badge + " " + label + (FAINT + " · claude.ai" + RESET if origin == "claude.ai" else "")
            body.append(row(left, right, inner))
        if len(ready) > limit:
            body.append(MUTED + "+%d more" % (len(ready) - limit) + RESET)
        if pending:
            body.append(row(MUTED + "Not connected" + RESET, fg(179) + str(pending) + RESET, inner))
        return self.card("Connectors", body, width, summary="%d ready" % len(ready))

    def skill_items(self):
        """Inventory of this session's skills, re-read from disk at most every 30s."""
        t = self.transcript
        cwd = (self.pane or {}).get("cwd") or ""
        key = (cwd, frozenset(t.skills))
        if self.skill_cache is None or self.skill_cache[0] != key or time.time() - self.skill_cache[1] > 30:
            self.skill_cache = (key, time.time(), inventory(t.skills, cwd))
        return self.skill_cache[2]

    def skills_card(self, width):
        t = self.transcript
        if not t or not t.skills:
            return self.card("Skills", [MUTED + "No skills listed" + RESET], width)
        items = self.skill_items()
        inner = width - 4
        limit = self.config.get("max_skills", 5)
        body = []
        outdated = [i for i in items if self.updates.status(i) == "update"]
        if outdated:
            body.append((row(fg(179) + BOLD + "↑ %d update%s available" % (
                len(outdated), "" if len(outdated) == 1 else "s") + RESET, fg(179) + "manage ›" + RESET, inner),
                ("skills", None)))
            body.append("")
        for group, title, color in (("local", "Local", 78), ("global", "Global", 69)):
            entries = [i for i in items if i["group"] == group]
            body.append(row(fg(color) + BOLD + title + RESET, MUTED + str(len(entries)) + RESET, inner))
            if not entries:
                body.append(FAINT + "  none" + RESET)
            # Outdated first, then the ones this agent used, then standalone before plugin skills.
            entries.sort(key=lambda i: (self.updates.status(i) != "update",
                                        -t.skill_calls.get(i["name"], 0), ":" in i["name"], i["name"]))
            for item in entries[:limit]:
                calls = t.skill_calls.get(item["name"], 0)
                if self.updates.status(item) == "update":
                    right = fg(179) + "↑ update" + RESET
                elif calls:
                    right = MUTED + "%d call%s" % (calls, "" if calls == 1 else "s") + RESET
                else:
                    right = ""
                tag = FAINT + "  " + item["detail"] + RESET if item["detail"] and item["detail"] != item["label"] else ""
                body.append((row("  " + item["label"] + tag, right, inner), ("skills", item["name"])))
                where = short_path(item["path"]) if item["path"] else "managed by your org" if item["manager"] == "synced" else ""
                if where:
                    body.append((FAINT + "    " + where + RESET, ("skills", item["name"])))
            if len(entries) > limit:
                body.append((MUTED + "  +%d more" % (len(entries) - limit) + RESET, ("skills", None)))
            body.append("")
        builtin = [i for i in items if i["group"] == "builtin"]
        used = sum(t.skill_calls.get(i["name"], 0) for i in builtin)
        right = MUTED + str(len(builtin)) + (" · %d call%s" % (used, "" if used == 1 else "s") if used else "") + RESET
        body.append((row(MUTED + "Built-in" + RESET, right, inner), ("skills", None)))
        summary = ("↑ %d update%s" % (len(outdated), "" if len(outdated) == 1 else "s")
                   if outdated else "%d skills" % len(items))
        return self.card("Skills", body, width, summary=summary)

    def open_skills(self, name=None):
        t = self.transcript
        if not t or not t.skills:
            return
        data = os.path.join(STATE_DIR, "skills-%s.json" % (SELF or TARGET).replace(":", "_"))
        tmp = data + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"cwd": (self.pane or {}).get("cwd"), "names": sorted(t.skills),
                       "calls": t.skill_calls}, f)
        os.replace(tmp, data)
        try:
            herdr("plugin", "pane", "open", "--plugin", PLUGIN_ID, "--entrypoint", "skills",
                  "--focus", "--env", "AGENT_PANEL_SKILLS=" + data,
                  "--env", "AGENT_PANEL_SKILL=" + (name or ""))
        except RuntimeError as err:
            self.message = (str(err).splitlines()[-1][:60], time.time())

    def check_updates_forever(self):
        """Background thread: ask GitHub about npx-skills installs once their cached answer is an hour old."""
        while True:
            items = self.skill_cache[2] if self.skill_cache else []
            if not items:
                time.sleep(2)  # skills aren't loaded yet
                continue
            before = dict(self.updates.data)
            self.updates.refresh(items)
            if self.updates.data != before:
                self.dirty = True
                self.last_frame = None
            time.sleep(60)

    def outputs_card(self, width):
        t = self.transcript
        items = t.recent_outputs() if t else []
        if not items:
            return self.card("Outputs", [MUTED + "No files written yet" + RESET], width)
        inner = width - 4
        cwd = (self.pane or {}).get("cwd") or ""
        body = []
        limit = self.config.get("max_outputs", 8)
        self.outputs = [path for path, _ in items[:limit]]
        if "Outputs" in self.folds:
            self.selected_row = None
        self.selected = min(self.selected, len(self.outputs) - 1)
        for i, (path, info) in enumerate(items[:limit]):
            icon = fg(78) + "+" + RESET if info["kind"] == "created" else fg(179) + "~" + RESET
            rel = os.path.relpath(path, cwd) if cwd and path.startswith(cwd + os.sep) else tilde(path)
            name = os.path.basename(rel)
            folder = os.path.dirname(rel)
            if i == self.selected:
                self.selected_row = len(body)
                pointer = fg(69) + BOLD + "›" + RESET
                name = BOLD + fg(69) + name + RESET
            else:
                pointer = FAINT + (str(i + 1) if i < 9 else " ") + RESET
            left = pointer + " " + icon + " " + name + (FAINT + "  " + folder + RESET if folder else "")
            right = MUTED + "×%d" % info["count"] + RESET if info["count"] > 1 else ""
            body.append((row(left, right, inner), ("output", i)))
        if len(items) > limit:
            body.append(MUTED + "+%d more" % (len(items) - limit) + RESET)
        return self.card("Outputs", body, width, summary="%d file%s" % (len(items), "" if len(items) == 1 else "s"))

    def render(self):
        cols, rows = shutil.get_terminal_size((PANEL_WIDTH, 40))
        width = max(cols - 1, 24)
        lines = []  # (text, click action)
        for card in (self.session_card, self.environment_card, self.context_card,
                     self.connectors_card, self.skills_card):
            lines += card(width)
        outputs_top = len(lines) + 1  # first body line, after the card's top border
        self.selected_row = None
        lines += self.outputs_card(width)
        if self.transcript is None and self.pane is not None:
            lines.append((MUTED + " No Claude transcript for this pane." + RESET, None))
        if self.message and time.time() - self.message[1] < 4:
            lines.append((" " + fg(203) + self.message[0] + RESET, None))
        self.footer = Buttons(gap=FAINT + " · " + RESET)
        self.footer.add("unfold all" if self.folds else "fold all", ("fold", None), MUTED)
        self.footer.add("skills", ("skills", None), MUTED)
        self.footer.add("close", ("close", None), MUTED)
        lines.append((" " + self.footer.text(), ("footer", None)))

        if self.follow_selection and self.selected_row is not None:
            line = outputs_top + self.selected_row
            if line >= self.scroll + rows:
                self.scroll = line - rows + 2
            elif line < self.scroll:
                self.scroll = line - 1
            self.follow_selection = False
        self.scroll = max(0, min(self.scroll, len(lines) - rows))
        self.targets = [action for _, action in lines]
        view = [text for text, _ in lines[self.scroll:self.scroll + rows]]
        frame = "\x1b[H" + "\n".join(l + "\x1b[K" for l in view) + "\x1b[J"
        if frame != self.last_frame:
            sys.stdout.write(frame)
            sys.stdout.flush()
            self.last_frame = frame
        self.dirty = False

    def click(self, col, row):
        """Run whatever sits under a click. Returns False when the panel should close."""
        index = self.scroll + row - 1
        if not 0 <= index < len(self.targets) or not self.targets[index]:
            return True
        kind, arg = self.targets[index]
        if kind == "footer":
            kind, arg = self.footer.hit(col - 1) or (None, None)
        if kind == "fold":
            self.toggle_fold(arg)
        elif kind == "output":
            self.preview(arg)
        elif kind == "skills":
            self.open_skills(arg)
        elif kind == "close":
            return False
        return True


def fit_width():
    """Shrink our split so the panel is about PANEL_WIDTH columns wide."""
    if not SELF:
        return
    try:
        snap = snapshot()
    except (RuntimeError, ValueError, KeyError):
        return
    for layout in snap["layouts"]:
        rect = next((p["rect"] for p in layout["panes"] if p["pane_id"] == SELF), None)
        if rect is None:
            continue
        # Smallest horizontal split whose right edge is ours: that's the one we sit in.
        splits = [
            s["rect"] for s in layout["splits"]
            if s["direction"] == "right"
            and s["rect"]["x"] < rect["x"]
            and s["rect"]["x"] + s["rect"]["width"] == rect["x"] + rect["width"]
        ]
        if not splits:
            return
        split = min(splits, key=lambda r: r["width"])
        # --amount is a change in split ratio, not columns.
        delta = (rect["width"] - PANEL_WIDTH) / float(split["width"])
        if delta > 0.01:
            herdr("pane", "resize", "--pane", SELF, "--direction", "right",
                  "--amount", "%.3f" % delta, check=False)
        return


def register(add):
    if not SELF:
        return
    panels = load_panels()
    if add:
        panels[SELF] = TARGET
    else:
        panels.pop(SELF, None)
        try:
            os.remove(os.path.join(STATE_DIR, "preview-%s.json" % SELF.replace(":", "_")))
        except OSError:
            pass
    save_panels(panels)


def handle_key(panel, key):
    """Returns False when the panel should close."""
    event = mouse(key)
    if event:
        kind, col, row = event
        if kind == WHEEL_UP:
            panel.scroll = max(panel.scroll - 3, 0)
        elif kind == WHEEL_DOWN:
            panel.scroll += 3
        else:
            return panel.click(col, row)
        panel.last_frame = None
    elif key in ("j", "\x1b[B"):
        panel.select(1)
    elif key in ("k", "\x1b[A"):
        panel.select(-1)
    elif key in ("J", "\x1b[6~"):
        panel.scroll += 3
    elif key in ("K", "\x1b[5~"):
        panel.scroll = max(panel.scroll - 3, 0)
    elif key in ("\r", "\n", "o"):
        panel.preview()
    elif key in "123456789" and len(key) == 1:
        panel.preview(int(key) - 1)
    elif key == "s":
        panel.open_skills()
    elif key == "z":
        panel.toggle_fold()
    elif key == "r":
        panel.config = load_config()
        panel.last_frame = None
    return True


def main():
    if not TARGET:
        print("AGENT_PANEL_TARGET is not set; open this panel with the toggle action.")
        time.sleep(3)
        return 1
    register(True)
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd) if os.isatty(fd) else None
    panel = Panel()

    def on_resize(*_):
        panel.last_frame = None
        panel.dirty = True

    signal.signal(signal.SIGWINCH, on_resize)
    for sig in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, lambda *_: sys.exit(0))
    sys.stdout.write("\x1b[?1049h\x1b[?25l" + MOUSE_ON)
    try:
        if old:
            tty.setcbreak(fd)
        fit_width()
        threading.Thread(target=panel.check_updates_forever, daemon=True).start()
        while not panel.target_gone():
            panel.refresh()
            panel.render()
            ready, _, _ = select.select([sys.stdin], [], [], 0.5)
            if ready:
                keys = split_keys(os.read(fd, 1024).decode(errors="ignore"))
                if "q" in keys or "\x1b" in keys:
                    break
                if not all([handle_key(panel, key) for key in keys]):
                    break
    finally:
        if old:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)
        sys.stdout.write(MOUSE_OFF + "\x1b[?25h\x1b[?1049l")
        sys.stdout.flush()
        register(False)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        pass
