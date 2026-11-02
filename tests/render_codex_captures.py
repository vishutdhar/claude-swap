"""Render the Codex block captures for a check by eye. Not a test.

    .venv/bin/python tests/render_codex_captures.py [OUT_DIR]

Writes the dropdown (four accounts, then the Codex block) and the Codex
header states, each in light and dark, to OUT_DIR (default
~/Code/cswap-captures/codex-block). A fixed clock and example accounts, so
two runs of one drawing give the same pictures.
"""

from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

import AppKit

from claude_swap import codex_usage as cu
from claude_swap import menubar_panel as mp
from claude_swap import menubar_panel_view as pv

NOW = 1_000_000.0
HOUR, DAY = 3600.0, 86400.0
WEEK_MIN = 7 * 24 * 60


def _window(pct: float, resets_in: float) -> dict:
    return {"pct": pct, "resets_at": dt.datetime.fromtimestamp(NOW + resets_in, dt.timezone.utc).isoformat()}


def _account(num, email, alias, usage, active=False, disabled=False):
    return mp.build_account_panel((num, email, active, usage, usage, alias, disabled, NOW - 120), NOW)


def accounts():
    return [
        _account("1", "first@example.com", "work",
                 {"five_hour": _window(17.0, 3 * HOUR), "seven_day": _window(42.0, 4 * DAY)}, active=True),
        _account("2", "second@example.com", "spare",
                 {"five_hour": {"pct": 0.0}, "seven_day": _window(100.0, 2 * DAY)}),
        _account("3", "third@example.com", "third",
                 {"five_hour": _window(64.0, 1.5 * HOUR), "seven_day": _window(71.0, 6 * DAY)}),
        _account("4", "fourth@example.com", "fourth", {"five_hour": {"pct": 5.0}}, disabled=True),
    ]


def codex(credits=2, alias=None, names_only=False, windows=None, reading=True):
    windows = windows or (cu.CodexWindow("7d", 56.0, WEEK_MIN, NOW + 2 * DAY + 3 * HOUR),)
    read = cu.CodexReading(tuple(windows), "pro", NOW - 9, "rollout") if reading else None
    state = cu.CodexState(True, read, credits, None if credits is None else NOW - 60, None)
    return mp.build_codex_panel(state, NOW, alias=alias, names_only=names_only)


def codex_states():
    return [
        codex(2),
        codex(0),
        codex(None),
        codex(1, alias="a-twelve-chr"),
        codex(3, windows=(cu.CodexWindow("7d", 56.0, WEEK_MIN, NOW - 60),)),  # reset passed
        codex(2, reading=False),
        codex(2, windows=(
            cu.CodexWindow("5h", 12.0, 300, NOW + HOUR + 6 * 60),
            cu.CodexWindow("7d", 56.0, WEEK_MIN, NOW + DAY),
        )),
    ]


SCENES = {
    "dropdown": (lambda: accounts() + [codex()], False),
    "codex-states": (codex_states, False),
    "codex-highlighted": (lambda: [codex()], True),
    "codex-alias-names-only": (lambda: accounts()[:1] + [codex(alias="a-twelve-chr", names_only=True)], False),
}


def main(argv: list[str]) -> int:
    out = Path(argv[1]) if len(argv) > 1 else Path.home() / "Code" / "cswap-captures" / "codex-block"
    out.mkdir(parents=True, exist_ok=True)
    AppKit.NSApplication.sharedApplication()
    for name, (panels, highlighted) in SCENES.items():
        for appearance, suffix in (("darkAqua", "dark"), ("aqua", "light")):
            path = out / f"{name}-{suffix}.png"
            pv.render_png(panels(), str(path), appearance, highlighted=highlighted)
            print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
