"""Work out where each skill in a session comes from, where it lives, and if it's outdated.

The transcript only lists skill names, so look them up on disk the way Claude Code
finds them: project .claude/skills folders from the cwd upwards, the user's
~/.claude/skills, and installed plugins (plugin:skill names). Skills installed with
`npx skills` are tracked in its lock files, which is what makes them updatable.
"""

import glob
import hashlib
import json
import os
import re
import subprocess
import time
import urllib.parse
import urllib.request

HOME = os.path.expanduser("~")
CLAUDE_DIR = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(HOME, ".claude")
UPDATE_TTL = 3600  # seconds between GitHub checks per skill
SHA = re.compile(r"^[0-9a-f]{40}$")


def project_skill_dirs(cwd):
    """.claude/skills in cwd and each parent, stopping above the home folder."""
    dirs = []
    path = os.path.abspath(cwd or HOME)
    while True:
        candidate = os.path.join(path, ".claude", "skills")
        if os.path.realpath(candidate) != os.path.realpath(os.path.join(CLAUDE_DIR, "skills")):
            dirs.append(candidate)
        parent = os.path.dirname(path)
        if path == HOME or parent == path:
            return dirs
        path = parent


def has_skill(folder, name):
    return os.path.isfile(os.path.join(folder, name, "SKILL.md"))


def read_json(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def installed_plugins():
    return read_json(os.path.join(CLAUDE_DIR, "plugins", "installed_plugins.json")).get("plugins") or {}


def global_lock():
    state = os.environ.get("XDG_STATE_HOME")
    path = os.path.join(state, "skills", ".skill-lock.json") if state else os.path.join(HOME, ".agents", ".skill-lock.json")
    return read_json(path).get("skills") or {}


def plugin_skill_path(skill, plugin, entries):
    """Find a plugin skill's folder: the install path first, then the plugin cache."""
    roots = [e.get("installPath") for e in entries or [] if e.get("installPath")]
    patterns = [os.path.join(r, "**", "skills", skill, "SKILL.md") for r in roots]
    patterns += [
        os.path.join(CLAUDE_DIR, "plugins", "cache", "*", plugin, "*", "skills", skill, "SKILL.md"),
        # Org plugins synced from claude.ai.
        os.path.join(CLAUDE_DIR, "plugins", "synced", "*", plugin, "**", "skills", skill, "SKILL.md"),
        os.path.join(CLAUDE_DIR, "skills", "synced", "*", skill, "SKILL.md"),
        os.path.join(CLAUDE_DIR, "plugins", "marketplaces", "*", "plugins", plugin, "skills", skill, "SKILL.md"),
    ]
    for pattern in patterns:
        hits = glob.glob(pattern, recursive=True)
        if hits:
            return os.path.dirname(max(hits, key=os.path.getmtime))
    return None


def inventory(names, cwd):
    """One dict per skill name.

    group:   "local", "global" or "builtin"
    detail:  plugin name, or the project folder a local skill comes from
    path:    real folder on disk (symlinks resolved), None for built-ins
    manager: "skills" (npx skills), "plugin" (Claude Code plugins), "synced" (org
             plugins synced from claude.ai) or None
    lock:    the npx skills lock entry, when managed by it
    run_in / scope: where and with which flag to run npx skills for it
    """
    project_dirs = project_skill_dirs(cwd)
    user_dir = os.path.join(CLAUDE_DIR, "skills")
    plugins = installed_plugins()
    glock = global_lock()
    cwd = os.path.abspath(cwd or HOME)
    result = []

    for name in sorted(names):
        prefix, _, skill = name.rpartition(":")
        item = {"name": name, "label": skill, "group": "builtin", "detail": "", "path": None,
                "manager": None, "lock": None, "run_in": None, "scope": None}
        if prefix and "/" in prefix:
            # Directory-scoped skill like "apps/web:deploy" lives inside the project.
            item.update(group="local", detail=prefix)
        elif prefix:
            entries = next((v for k, v in plugins.items() if k.split("@")[0] == prefix), None)
            local = any(
                e.get("scope") in ("project", "local")
                and (cwd + os.sep).startswith(os.path.abspath(e.get("projectPath") or "/nowhere") + os.sep)
                for e in entries or []
            )
            folder = plugin_skill_path(skill, prefix, entries)
            synced = folder and os.sep + "synced" + os.sep in folder
            item.update(group="local" if local else "global", detail=prefix,
                        manager="synced" if synced or not entries else "plugin",
                        path=os.path.realpath(folder) if folder else None)
        elif any(has_skill(d, name) for d in project_dirs):
            folder = next(d for d in project_dirs if has_skill(d, name))
            project = os.path.dirname(os.path.dirname(folder))
            item.update(group="local", detail="" if project == cwd else os.path.basename(project),
                        path=os.path.realpath(os.path.join(folder, name)))
            plock = read_json(os.path.join(project, "skills-lock.json")).get("skills") or {}
            if name in plock:
                item.update(manager="skills", lock=plock[name], run_in=project, scope="-p")
        elif has_skill(user_dir, name):
            item.update(group="global", path=os.path.realpath(os.path.join(user_dir, name)))
            if name in glock:
                item.update(manager="skills", lock=glock[name], run_in=HOME, scope="-g")
        item["installed_hash"] = installed_hash(item) if item["manager"] == "skills" else None
        result.append(item)
    return result


# ---- update checks ----------------------------------------------------------

def tree_sha(folder):
    """Git tree hash of a folder, computed locally. Matches GitHub's hash for the same files."""
    entries = []
    for name in os.listdir(folder):
        if name in (".git", "node_modules"):
            continue
        path = os.path.join(folder, name)
        if os.path.isdir(path):
            entries.append((name + "/", b"40000", name, bytes.fromhex(tree_sha(path))))
        else:
            with open(path, "rb") as f:
                data = f.read()
            blob = hashlib.sha1(b"blob %d\0" % len(data) + data).digest()
            mode = b"100755" if os.access(path, os.X_OK) else b"100644"
            entries.append((name, mode, name, blob))
    entries.sort(key=lambda e: e[0].encode())  # git orders folders as "name/"
    body = b"".join(mode + b" " + name.encode() + b"\0" + sha for _, mode, name, sha in entries)
    return hashlib.sha1(b"tree %d\0" % len(body) + body).hexdigest()


def installed_hash(item):
    """The global lock stores the git tree hash; project locks don't, so hash the folder."""
    lock = item.get("lock") or {}
    if SHA.match(lock.get("skillFolderHash") or ""):
        return lock["skillFolderHash"]
    try:
        return tree_sha(item["path"]) if item.get("path") else None
    except (OSError, ValueError):
        return None


def checkable(item):
    lock = item.get("lock") or {}
    return lock.get("sourceType") == "github" and lock.get("skillPath") and item.get("installed_hash")


def cache_key(item):
    lock = item["lock"]
    return "%s|%s|%s" % (lock.get("source"), lock.get("ref") or "", lock.get("skillPath"))


def latest_folder_hash(lock):
    """Git tree hash of the skill folder on GitHub, the value npx skills compares."""
    repo = "/".join(lock["source"].split("/")[:2])
    folder = lock["skillPath"].rsplit("/", 1)[0] if "/" in lock["skillPath"] else ""
    parent, _, base = folder.rpartition("/")
    url = "https://api.github.com/repos/%s/contents/%s" % (repo, urllib.parse.quote(parent))
    if lock.get("ref"):
        url += "?ref=" + urllib.parse.quote(lock["ref"])
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "herdr-agent-panel"}
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token:
        headers["Authorization"] = "Bearer " + token
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=10) as resp:
        items = json.load(resp)
    return next((i["sha"] for i in items if i.get("type") == "dir" and i.get("name") == base), None)


class UpdateCache:
    """Latest known folder hashes, shared between the panel and the skills popup."""

    def __init__(self, path):
        self.path = path
        self.data = {}
        self.mtime = None

    def load(self):
        try:
            mtime = os.path.getmtime(self.path)
        except OSError:
            return
        if mtime != self.mtime:
            self.data = read_json(self.path)
            self.mtime = mtime

    def save(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self.data, f)
        os.replace(tmp, self.path)
        self.mtime = os.path.getmtime(self.path)

    def status(self, item):
        """"update", "current", "unknown", or None when it can't be checked."""
        if not checkable(item):
            return None
        self.load()
        hit = self.data.get(cache_key(item))
        if not hit or not hit.get("latest"):
            return "unknown"
        return "update" if hit["latest"] != item["installed_hash"] else "current"

    def refresh(self, items, force=False):
        """Ask GitHub about skills whose cached answer is stale. Blocking; run off the UI thread."""
        self.load()
        now = time.time()
        changed = False
        for item in items:
            if not checkable(item):
                continue
            key = cache_key(item)
            hit = self.data.get(key)
            if hit and not force and now - hit.get("checked", 0) < UPDATE_TTL:
                continue
            try:
                latest = latest_folder_hash(item["lock"])
            except Exception:  # offline, rate limited, repo moved: keep the old answer
                continue
            self.data[key] = {"latest": latest, "checked": now}
            changed = True
        if changed:
            self.save()


# ---- actions -----------------------------------------------------------------

def npx_skills(action, item):
    """Run `npx skills update|remove` for one skill. Returns (ok, output)."""
    args = ["npx", "-y", "skills@latest", action, item["name"], item["scope"], "-y"]
    try:
        proc = subprocess.run(args, cwd=item["run_in"], stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, text=True, timeout=300)
    except (OSError, subprocess.SubprocessError) as err:
        return False, str(err)
    output = re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", proc.stdout)
    return proc.returncode == 0, output
