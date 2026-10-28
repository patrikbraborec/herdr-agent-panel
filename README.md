# Agent Panel for herdr

A right-side panel attached to one agent pane. Each agent gets its own panel, and it updates live from the agent's Claude Code transcript.

```
╭─ Session ──────────────────────────────╮
│ Herdr plugin view customization        │
│ ● working · Opus 5.5 · bypass          │
╰────────────────────────────────────────╯
╭─ Context ──────────────────────────────╮
│ 10% used                   103.5k / 1M │
│ ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ │
│ ■ Messages               81.0k    8.1% │
│ ■ System tools           20.4k    2.0% │
│ ...                                    │
```

Cards:

- **Session**: title, status, model, permission mode, number of prompts and tool calls
- **Environment**: folder, git branch, machine, pane, and whether the agent runs commands in a sandbox (and which one, e.g. macOS Seatbelt) or locally without one. It also counts commands that ran inside the sandbox versus outside it (`dangerouslyDisableSandbox`), and any commands the sandbox blocked
- **Context**: tokens used out of the context window, with an estimated split by category
- **Connectors**: MCP servers available to the session and how often each was called. Servers that still need auth are counted separately.
- **Skills**: skills available to the session, grouped as **Local** (the project's `.claude/skills` in the agent's folder or a parent, or plugins installed for that project) and **Global** (`~/.claude/skills`, user-scope and org plugins). Each skill shows the folder it's stored in, with symlinks resolved. Built-in Claude Code skills collapse to a count. Outdated skills are listed first with a yellow `↑ update` badge, then the skills the agent called, with their call counts.
- **Outputs**: files the agent created (`+`) or edited (`~`), newest first

## Install

```bash
herdr plugin install patrikbraborec/herdr-agent-panel
# or, from a local checkout
herdr plugin link /path/to/herdr-agent-panel
```

Bind the toggle in `~/.config/herdr/config.toml`:

```toml
[[keys.command]]
key = "prefix+i"
type = "plugin_action"
command = "braborec.agent-panel.toggle"
description = "toggle agent panel"
```

Focus an agent pane and press `prefix+i` to open its panel. Press it again from the agent or from the panel to close it. The panel also closes when its agent pane closes.

Everything works with the mouse:

- Click a card's title to fold it down to one line with a summary (`▸ Context ── 26% used`), and click again to unfold. Folded cards stay folded across panels and restarts. `fold all` in the footer, or `z`, folds or unfolds everything.
- Click a file under Outputs to open its diffs.
- Click a skill, or the `↑ N updates available` line, to open the skills manager on that skill.
- The scroll wheel scrolls the panel.

In the diff popup, click a file in the tab bar to switch to it, and use the footer buttons (`◂ file`, `file ▸`, `◂ change`, `change ▸`, `show file`/`show diff`, `edit`, `close`). In the skills manager, click a row to select it, click `↑ update available` to update that skill, and use the footer buttons (`update`, `update all`, `remove`, `recheck`, `close`). `remove` asks first with `Yes, remove` / `Cancel` buttons.

Mouse input needs herdr's default `ui.mouse_capture = true`.

Keyboard, inside the panel:

| Key | Action |
| --- | --- |
| `↑`/`↓` or `j`/`k` | Select a file under Outputs |
| `⏎` or `o` | Open the selected file's diffs in a popup |
| `1`–`9` | Open that file directly |
| `s` | Open the skills manager |
| `z` | Fold or unfold all cards |
| `J`/`K` | Scroll the panel |
| `q` | Close the panel |

### File diffs

`⏎` opens on the changes the agent made to that file, one block per Write or Edit, with the time of each. It covers every file in Outputs, so you can move between them without closing it:

| Key | Action |
| --- | --- |
| `Tab`/`Shift+Tab` (or `n`/`p`, `]`/`[`) | Next or previous file |
| `}`/`{` | Next or previous change in this file |
| `d` | Toggle between the diff and the full file |
| `j`/`k`, `space`/`b`, `g`/`G` | Scroll, page, top/end |
| `h`/`l` | Pan sideways |
| `e` | Open the file in `$EDITOR` |
| `q`/`Esc` | Close |

The popup updates while it's open, so new edits show up as the agent makes them. Diffs come from the structured patches Claude Code records for its Write and Edit tools. Changes made another way, such as `sed` in a shell command, only appear in the full-file view. The full-file view has basic syntax colouring for Python, JS/TS, shell, TOML/YAML, JSON and Markdown.

### Skills manager

`s` opens a popup with every skill in the session: where it's stored, where it came from (`npx skills` source and scope, a Claude Code plugin, an org plugin synced from claude.ai, or built in), and whether an update is available.

| Key | Action |
| --- | --- |
| `j`/`k` | Select a skill |
| `u` | `npx skills update <name>` |
| `U` | Update every skill with an update available |
| `x` | `npx skills remove <name>`, after a y/N confirmation |
| `r` | Check GitHub again now |
| `q`/`Esc` | Close |

Only skills installed with `npx skills` can be updated or removed here. The popup tells you how plugin and org skills are managed instead. Global skills run with `-g`, and project skills run with `-p` from the project folder that holds `skills-lock.json`.

Update checks are read-only, because `npx skills check` is an alias of `update` and would install changes. The panel instead compares each skill's git tree hash with the folder's current hash on GitHub, which is the same comparison `npx skills update` makes. Global installs record that hash in `~/.agents/.skill-lock.json`. For project installs the panel hashes the installed folder. Results are cached for an hour in the plugin state folder. Unauthenticated GitHub requests are limited to 60 an hour; set `GITHUB_TOKEN` to raise that.

## Config

Create an optional `config.json` in the directory printed by `herdr plugin config-dir braborec.agent-panel`:

```json
{
  "context_window": 1000000,
  "context_windows": { "claude-sonnet-4-5": 200000 },
  "max_connectors": 8,
  "max_outputs": 8,
  "max_skills": 5
}
```

Without it, the context window defaults to 1M for Claude 5 models and `[1m]` models, and 200k for everything else. Press `r` in the panel to reload the config.

## How it works

herdr's snapshot reports each Claude pane's session ID (`agent_session`). The panel finds `~/.claude/projects/*/<session>.jsonl` and tails it:

- Context used is `input + cache_read + cache_creation` from the latest assistant `usage`.
- The category split measures the latest `api-request-shape` (system blocks, tool definitions) at about 4 chars per token. Messages are whatever is left. Treat the split as approximate.

When a session continues in a new transcript file, herdr keeps reporting the original session ID. The panel then switches to the newer file in the same folder that contains the old file's last message.

Panes running other agents still get the Session and Environment cards.
