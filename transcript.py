"""Incremental reader for Claude Code session transcripts (~/.claude/projects/*/<id>.jsonl)."""

import glob
import json
import os
import re

FILE_TOOLS = {"Write": "created", "Edit": "edited", "MultiEdit": "edited", "NotebookEdit": "edited"}
SANDBOX_KIND = re.compile(r"OS-level sandbox \(([^)]+)\)")
SLASH_COMMAND = re.compile(r"<command-name>/?([^<\s]+)</command-name>")
AUTH_TOOLS = {"authenticate", "complete_authentication"}


def find_transcript(session_id):
    roots = [os.environ.get("CLAUDE_CONFIG_DIR"), os.path.expanduser("~/.claude")]
    for root in filter(None, roots):
        hits = glob.glob(os.path.join(root, "projects", "*", session_id + ".jsonl"))
        if hits:
            return max(hits, key=os.path.getmtime)
    return None


def find_successor(path, last_uuid):
    """A session that was continued in a new file copies the old messages over.

    herdr keeps reporting the original session id, so find the newer transcript in
    the same project folder that contains the old file's last message.
    """
    if not last_uuid:
        return None
    try:
        since = os.path.getmtime(path)
    except OSError:
        return None
    needle = ('"uuid":"%s"' % last_uuid).encode()
    candidates = [
        p for p in glob.glob(os.path.join(os.path.dirname(path), "*.jsonl"))
        if p != path and os.path.getmtime(p) > since
    ]
    for candidate in sorted(candidates, key=os.path.getmtime, reverse=True):
        try:
            with open(candidate, "rb") as f:
                if needle in f.read():
                    return candidate
        except OSError:
            continue
    return None


def server_key(name):
    """Normalize 'claude.ai Slack' and 'claude_ai_Slack' to the same key."""
    return re.sub(r"[^a-z0-9]", "", name.lower())


def server_label(name):
    for prefix in ("claude_ai_", "claude.ai "):
        if name.startswith(prefix):
            return name[len(prefix):].replace("_", " "), "claude.ai"
    return name, "local"


class Transcript:
    def __init__(self, path):
        self.path = path
        self.offset = 0
        self.buf = b""
        self.title = None
        self.model = None
        self.model_name = None
        self.permission_mode = None
        self.usage = None
        self.turns = 0
        self.tool_calls = 0
        self.shape = None  # latest API request shape: system blocks + tools
        self.deferred = set()
        self.instr_servers = {}
        self.server_calls = {}
        self.outputs = {}  # path -> {"kind", "count", "seq"}
        self.diffs = {}  # path -> [{"time", "kind", "hunks"}] in the order they happened
        self.last_uuid = None
        self.skills = set()
        self.sandbox = None  # sandbox technology named at session start, e.g. "macOS Seatbelt"
        self.commands_sandboxed = 0
        self.commands_outside = 0
        self.sandbox_denials = 0
        self.skill_calls = {}
        self._seq = 0

    def poll(self):
        """Read any lines appended since the last poll. Returns True if anything changed."""
        try:
            size = os.path.getsize(self.path)
        except OSError:
            return False
        if size < self.offset:  # rewritten, start over
            self.__init__(self.path)
        if size == self.offset:
            return False
        with open(self.path, "rb") as f:
            f.seek(self.offset)
            chunk = f.read()
        self.offset += len(chunk)
        data = self.buf + chunk
        lines = data.split(b"\n")
        self.buf = lines.pop()
        for line in lines:
            try:
                self._ingest(json.loads(line))
            except (ValueError, TypeError, AttributeError):
                continue
        return True

    def _ingest(self, d):
        if d.get("uuid") and not d.get("isSidechain"):
            self.last_uuid = d["uuid"]
        t = d.get("type")
        if t == "ai-title":
            self.title = d.get("aiTitle") or self.title
        elif t == "permission-mode":
            self.permission_mode = d.get("permissionMode")
        elif t == "api-request-shape":
            self.shape = d.get("shape")
        elif t == "attachment":
            self._attachment(d.get("attachment") or {})
        elif t == "user" and not d.get("isSidechain"):
            if d.get("sandboxed") is True:
                self.commands_sandboxed += 1
            elif d.get("sandboxed") is False:
                self.commands_outside += 1
            self.sandbox_denials += len(d.get("sandboxViolations") or [])
            if isinstance(d.get("toolUseResult"), dict):
                self._file_change(d["toolUseResult"], d.get("timestamp"))
            content = (d.get("message") or {}).get("content")
            if isinstance(content, str):
                for name in SLASH_COMMAND.findall(content):
                    self._count_skill(name)
            if isinstance(content, str) or (
                isinstance(content, list) and any(c.get("type") == "text" for c in content)
            ):
                self.turns += 1
        elif t == "assistant" and not d.get("isSidechain"):
            msg = d.get("message") or {}
            if msg.get("usage"):
                self.usage = msg["usage"]
            self.model = msg.get("model") or self.model
            for block in msg.get("content") or []:
                if block.get("type") == "tool_use":
                    self._tool_use(block)

    def _attachment(self, a):
        kind = a.get("type")
        if kind == "model":
            ident = a.get("identity") or {}
            self.model_name = ident.get("marketingName")
            self.model = ident.get("modelId") or self.model
        elif kind == "deferred_tools_delta":
            self.deferred.update(a.get("addedNames") or [])
            self.deferred.difference_update(a.get("removedNames") or [])
        elif kind == "sandbox_instructions":
            m = SANDBOX_KIND.search(a.get("content") or "")
            self.sandbox = m.group(1) if m else "on"
        elif kind == "skill_listing":
            if a.get("isInitial"):
                self.skills = set()
            self.skills.update(a.get("names") or [])
            self.skills.difference_update(a.get("removedNames") or [])
        elif kind == "mcp_instructions_delta":
            for name in a.get("addedNames") or []:
                self.instr_servers[server_key(name)] = name
            for name in a.get("removedNames") or []:
                self.instr_servers.pop(server_key(name), None)

    def _tool_use(self, block):
        self.tool_calls += 1
        name = block.get("name") or ""
        args = block.get("input") or {}
        if name == "Skill" and args.get("skill"):
            self._count_skill(args["skill"])
        if name.startswith("mcp__"):
            server = name.split("__")[1]
            key = server_key(server)
            self.server_calls[key] = self.server_calls.get(key, 0) + 1
        kind = FILE_TOOLS.get(name)
        path = args.get("file_path") or args.get("notebook_path")
        if kind and path:
            self._seq += 1
            entry = self.outputs.setdefault(path, {"kind": kind, "count": 0})
            entry["count"] += 1
            entry["seq"] = self._seq

    def _count_skill(self, name):
        self.skill_calls[name] = self.skill_calls.get(name, 0) + 1

    def _file_change(self, result, timestamp):
        path = result.get("filePath")
        hunks = result.get("structuredPatch")
        if not path or not isinstance(hunks, list):
            return
        if result.get("type") == "create":
            lines = (result.get("content") or "").splitlines()
            hunks = [{"oldStart": 0, "oldLines": 0, "newStart": 1, "newLines": len(lines),
                      "lines": ["+" + line for line in lines]}]
            kind = "created"
        elif result.get("type") == "update":
            kind = "rewritten"
        else:
            kind = "edited"
        if hunks:
            self.diffs.setdefault(path, []).append({"time": timestamp, "kind": kind, "hunks": hunks})

    # ---- derived views -------------------------------------------------

    def context_used(self):
        u = self.usage or {}
        return (
            (u.get("input_tokens") or 0)
            + (u.get("cache_read_input_tokens") or 0)
            + (u.get("cache_creation_input_tokens") or 0)
        )

    def context_breakdown(self):
        """Rough split of the last request by category. Shape sizes are chars / 4."""
        used = self.context_used()
        system = sys_tools = mcp_tools = 0
        if self.shape:
            system = len(json.dumps(self.shape.get("system") or [])) // 4
            for tool in self.shape.get("tools") or []:
                size = len(json.dumps(tool)) // 4
                if (tool.get("name") or "").startswith("mcp__"):
                    mcp_tools += size
                else:
                    sys_tools += size
        messages = max(used - system - sys_tools - mcp_tools, 0)
        return [
            ("Messages", messages),
            ("System tools", sys_tools),
            ("MCP tools", mcp_tools),
            ("System prompt", system),
        ]

    def connectors(self):
        """[(label, origin, state, calls)] where state is 'ready' or 'auth'."""
        tools = {}
        names = set(self.deferred)
        for tool in (self.shape or {}).get("tools") or []:
            names.add(tool.get("name") or "")
        for name in names:
            if name.startswith("mcp__"):
                parts = name.split("__", 2)
                if len(parts) == 3:
                    tools.setdefault(parts[1], set()).add(parts[2])
        servers = {server_key(s): s for s in tools}
        for key, name in self.instr_servers.items():
            servers.setdefault(key, name)
        result = []
        for key, raw in servers.items():
            label, origin = server_label(raw)
            raw_tools = next((v for k, v in tools.items() if server_key(k) == key), set())
            state = "auth" if raw_tools and raw_tools <= AUTH_TOOLS else "ready"
            result.append((label, origin, state, self.server_calls.get(key, 0)))
        # Used first, then ready ones, then the ones waiting for auth.
        result.sort(key=lambda r: (-r[3], r[2] == "auth", r[0].lower()))
        return result

    def recent_outputs(self):
        return sorted(self.outputs.items(), key=lambda kv: -kv[1]["seq"])
