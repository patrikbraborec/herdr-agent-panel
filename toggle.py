"""Open or close the agent panel for the focused pane."""

import json
import os
import sys

from common import PLUGIN_ID, herdr, load_panels, save_panels, snapshot


def focused_pane(snap):
    try:
        ctx = json.loads(os.environ.get("HERDR_PLUGIN_CONTEXT_JSON") or "{}")
    except ValueError:
        ctx = {}
    for key in ("pane_id", "focused_pane_id"):
        if isinstance(ctx.get(key), str):
            return ctx[key]
    pane = ctx.get("pane") or ctx.get("focused_pane")
    if isinstance(pane, dict) and pane.get("pane_id"):
        return pane["pane_id"]
    return os.environ.get("HERDR_PANE_ID") or snap.get("focused_pane_id")


def main():
    snap = snapshot()
    live = {p["pane_id"] for p in snap["panes"]}
    panels = {k: v for k, v in load_panels().items() if k in live}
    save_panels(panels)

    pane = focused_pane(snap)
    if not pane:
        print("no focused pane", file=sys.stderr)
        return 1

    # Toggling from inside a panel, or from an agent that already has one, closes it.
    to_close = [p for p, target in panels.items() if p == pane or target == pane]
    if to_close:
        for p in to_close:
            herdr("pane", "close", p, check=False)
        return 0

    herdr(
        "plugin", "pane", "open",
        "--plugin", PLUGIN_ID,
        "--entrypoint", "panel",
        "--placement", "split",
        "--target-pane", pane,
        "--direction", "right",
        "--no-focus",
        "--env", "AGENT_PANEL_TARGET=" + pane,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
