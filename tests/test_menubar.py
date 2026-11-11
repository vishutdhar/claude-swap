"""Tests for the menu bar module.

These tests never import or run rumps/AppKit. They exercise the pure helpers
(settings store, title/label formatting, usage/snapshot adapters, log parsing)
only — the auto-switch engine itself lives in ``claude_swap.autoswitch`` and is
tested there.
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
import os
import plistlib
import shlex
import shutil
import stat
import subprocess
import threading
import sys
from pathlib import Path

import pytest

from claude_swap import menubar
from claude_swap.exceptions import ClaudeSwitchError
from claude_swap.switcher import USAGE_API_KEY


# --- notification identity -----------------------------------------------------

def test_notification_identity_creates_and_preserves_info_plist(tmp_path: Path):
    executable = tmp_path / "bin" / "python3"
    executable.parent.mkdir()
    info = executable.parent / "Info.plist"
    info.write_bytes(plistlib.dumps({"ExistingKey": "kept"}))

    result = menubar.ensure_notification_identity(executable, platform="darwin")

    assert result == info
    data = plistlib.loads(info.read_bytes())
    assert data["CFBundleIdentifier"] == "com.claude-swap.menubar"
    assert data["CFBundleName"] == "claude-swap"
    assert data["ExistingKey"] == "kept"


def test_notification_identity_heals_corrupt_info_plist(tmp_path: Path):
    executable = tmp_path / "bin" / "python3"
    executable.parent.mkdir()
    info = executable.parent / "Info.plist"
    # truncated XML plist: plistlib raises ExpatError, not InvalidFileException
    info.write_bytes(
        b'<?xml version="1.0" encoding="UTF-8"?>\n'
        b'<plist version="1.0"><dict><key>CFBundle'
    )

    result = menubar.ensure_notification_identity(executable, platform="darwin")

    assert result == info
    data = plistlib.loads(info.read_bytes())
    assert data["CFBundleIdentifier"] == "com.claude-swap.menubar"
    assert data["CFBundleName"] == "claude-swap"
    assert not (executable.parent / "Info.plist.tmp").exists()


def test_notification_identity_is_noop_off_macos(tmp_path: Path):
    executable = tmp_path / "bin" / "python3"
    assert menubar.ensure_notification_identity(
        executable, platform="linux"
    ) is None
    assert not (executable.parent / "Info.plist").exists()


# --- settings ------------------------------------------------------------------

def test_settings_defaults_when_file_missing(tmp_path: Path):
    s = menubar.MenuBarSettings.load(tmp_path / "nope.json")
    assert s.show_account_name is True
    assert s.title_pct == "both"
    assert s.refresh_interval == 60
    assert s.auto_switch_enabled is False


def test_settings_round_trip(tmp_path: Path):
    path = tmp_path / "menubar_settings.json"
    original = menubar.MenuBarSettings(
        show_account_name=False,
        title_pct="5h",
        refresh_interval=300,
        auto_switch_enabled=True,
    )
    original.save(path)
    loaded = menubar.MenuBarSettings.load(path)
    assert loaded == original


def test_settings_corrupt_file_falls_back_to_defaults(tmp_path: Path):
    path = tmp_path / "menubar_settings.json"
    path.write_text("{ this is not json", encoding="utf-8")
    s = menubar.MenuBarSettings.load(path)
    assert s == menubar.MenuBarSettings()


def test_settings_ignores_unknown_and_bad_types(tmp_path: Path):
    path = tmp_path / "menubar_settings.json"
    path.write_text(
        json.dumps(
            {"refresh_interval": "fast", "bogus": 1, "show_account_name": False}
        ),
        encoding="utf-8",
    )
    s = menubar.MenuBarSettings.load(path)
    # bad-typed refresh_interval falls back to default; valid bool is kept
    assert s.refresh_interval == 60
    assert s.show_account_name is False


_USAGE = {
    "five_hour": {"pct": 42.0},
    "seven_day": {"pct": 18.0},
    "spend": {"pct": 30.0, "used": 3.0, "limit": 10.0},
}


# --- usage display helpers -----------------------------------------------------

def test_tightest_pct_uses_max_window():
    assert menubar.tightest_pct(_USAGE) == 42.0


def test_tightest_pct_none_for_non_dict_or_empty():
    assert menubar.tightest_pct("no credentials") is None
    assert menubar.tightest_pct(None) is None
    assert menubar.tightest_pct({"spend": {"pct": 90.0}}) is None  # no 5h/7d


def test_usage_summary_dict():
    assert menubar.usage_summary(_USAGE) == "5h 42% · 7d 18% · $ 30%"


def test_usage_summary_partial_windows():
    assert menubar.usage_summary({"five_hour": {"pct": 5.0}}) == "5h 5%"


def test_usage_summary_includes_scoped_model_limits():
    # Per-model weekly limits (e.g. Fable) come through as usage["scoped"], after
    # 5h/7d and before spend.
    usage = {
        "five_hour": {"pct": 82.0},
        "seven_day": {"pct": 12.0},
        "scoped": [{"name": "Fable", "pct": 4.0}],
        "spend": {"pct": 30.0},
    }
    assert menubar.usage_summary(usage) == "5h 82% · 7d 12% · Fable 4% · $ 30%"


def test_usage_summary_scoped_over_limit_marker():
    usage = {"scoped": [{"name": "Fable", "pct": 100.0}]}
    assert menubar.usage_summary(usage) == "Fable 100% (!)"


def test_usage_summary_scoped_multiple_and_countdown():
    usage = {
        "scoped": [
            {"name": "Fable", "pct": 4.0, "resets_at": _iso(2 * 3600)},
            {"name": "Opus", "pct": 55.0},
        ],
    }
    assert menubar.usage_summary(usage, _NOW) == "Fable 4% (2h 0m) · Opus 55%"


def test_usage_summary_string_sentinel_passthrough():
    assert menubar.usage_summary("no credentials") == "no credentials"


def test_usage_summary_none():
    assert menubar.usage_summary(None) == "usage unavailable"


def test_usage_summary_seven_day_ahead_of_pace_marker():
    # 1 day elapsed of the week, 50% used -> far ahead of the ~14% expected.
    usage = {"seven_day": {"pct": 50.0, "resets_at": _iso(6 * 86400)}}
    out = menubar.usage_summary(usage, _NOW, fetched_at=_NOW)
    assert out == "7d 50% (ahead) (6d 0h)"


def test_usage_summary_five_hour_never_shows_pace_marker():
    usage = {"five_hour": {"pct": 90.0, "resets_at": _iso(4 * 3600)}}
    out = menubar.usage_summary(usage, _NOW, fetched_at=_NOW)
    assert "ahead" not in out


def test_usage_summary_scoped_ahead_of_pace_marker():
    usage = {"scoped": [{"name": "Fable", "pct": 50.0, "resets_at": _iso(6 * 86400)}]}
    out = menubar.usage_summary(usage, _NOW, fetched_at=_NOW)
    assert out == "Fable 50% (ahead) (6d 0h)"


def test_usage_summary_maxed_scoped_marker_wins_over_pace():
    # At/over the limit shows "(!)" — the more urgent signal — not "(ahead)".
    usage = {"scoped": [{"name": "Fable", "pct": 100.0, "resets_at": _iso(6 * 86400)}]}
    out = menubar.usage_summary(usage, _NOW, fetched_at=_NOW)
    assert "(!)" in out
    assert "ahead" not in out


def test_usage_summary_no_pace_marker_without_fetched_at():
    usage = {"seven_day": {"pct": 50.0, "resets_at": _iso(6 * 86400)}}
    out = menubar.usage_summary(usage, _NOW)
    assert "ahead" not in out


def test_usage_summary_no_pace_marker_on_window_rolled_to_zero():
    # A weekly window whose resets_at has already passed (stale cache, not
    # refetched since the actual reset) is rolled to a display pct of 0% —
    # pace must be computed against that rolled 0%, not the raw stale pct,
    # or the display would show "7d 0% (ahead)" (a marker paired with a
    # percentage it doesn't correspond to).
    usage = {"seven_day": {"pct": 95.0, "resets_at": _iso(-3 * 86400)}}
    out = menubar.usage_summary(usage, _NOW, fetched_at=_NOW - 4 * 86400)
    assert "ahead" not in out
    assert "7d 0%" in out


def test_usage_summary_scoped_no_pace_marker_on_window_rolled_to_zero():
    usage = {"scoped": [{"name": "Fable", "pct": 95.0, "resets_at": _iso(-3 * 86400)}]}
    out = menubar.usage_summary(usage, _NOW, fetched_at=_NOW - 4 * 86400)
    assert "ahead" not in out
    assert "Fable 0%" in out


def test_format_account_label():
    label = menubar.format_account_label(2, "loc@papaya.asia", _USAGE)
    assert label == "2  loc@papaya.asia  5h 42% · 7d 18% · $ 30%"


def test_format_account_label_with_alias():
    label = menubar.format_account_label(2, "loc@papaya.asia", _USAGE, alias="dev")
    assert label == "2  dev  (loc@papaya.asia)  5h 42% · 7d 18% · $ 30%"


def test_format_account_label_paused_marker():
    # Disabled with no hold of the rotation guard on it: the owner's own pause.
    label = menubar.format_account_label(2, "loc@papaya.asia", _USAGE, disabled=True)
    assert label == "2  loc@papaya.asia  (paused)  5h 42% · 7d 18% · $ 30%"


# --- held or paused: the rotation guard's hold marker ------------------------------
#
# cswap knows one disabled flag per account. The rotation daemon
# (cswap-rotate, a separate script) sets that same flag for its 5-hour guard
# and records each hold in ~/Library/Logs/cswap-rotate.hold5h as
# `N:email:<ISO release time>`, with `:releasing` / `:released` appended
# while a release is under way. These tests mirror its tolerant parser.

_HOLD_NOW = 1_800_000_000.0


def _hold_iso(delta_s):
    return _dt.datetime.fromtimestamp(_HOLD_NOW + delta_s, _dt.timezone.utc).isoformat()


def _marker(tmp_path, text):
    path = tmp_path / "cswap-rotate.hold5h"
    path.write_text(text, encoding="utf-8")
    return path


def test_hold_marker_missing_file_is_no_holds(tmp_path):
    assert menubar.read_hold_marker(tmp_path / "cswap-rotate.hold5h") == {}


def test_hold_marker_unreadable_is_unknown(tmp_path):
    # A directory where the file should be, and bytes that are not UTF-8:
    # a marker that cannot be read is unknown, never "no holds".
    assert menubar.read_hold_marker(tmp_path) is None
    bad = tmp_path / "bad.hold5h"
    bad.write_bytes(b"2:b@x.com:\xff\xfe")
    assert menubar.read_hold_marker(bad) is None


@pytest.mark.parametrize(
    "text",
    [
        "junk",
        ":b@x.com",  # no slot
        "2:b@x.com:not-a-time",
        "2:b@x.com:2026-10-23T01:10:00",  # a time with no offset, as the daemon refuses it
        "2:b@x.com:2026-10-23T01:10:00+00:00 junk",  # one bad token spoils the read
    ],
)
def test_hold_marker_garbage_is_unknown(tmp_path, text):
    assert menubar.read_hold_marker(_marker(tmp_path, text)) is None


@pytest.mark.parametrize(
    "text, expected",
    [
        ("2", {"2": (None, None, None)}),  # the first release's bare slot: no identity
        ("2:b@x.com", {"2": ("b@x.com", None, None)}),  # no release time recorded
        (f"2:b@x.com:{_hold_iso(1680)}", {"2": ("b@x.com", _HOLD_NOW + 1680, None)}),
        (f"2:b@x.com:{_hold_iso(1680)}:releasing", {"2": ("b@x.com", _HOLD_NOW + 1680, "releasing")}),
        (f"2:b@x.com:{_hold_iso(1680)}:released", {"2": ("b@x.com", _HOLD_NOW + 1680, "released")}),
        ("2:b@x.com:releasing", {"2": ("b@x.com", None, "releasing")}),
        ("2:b@x.com:2026-10-23T01:10:00Z", {"2": ("b@x.com", 1792717800.0, None)}),  # "Z" is UTC
    ],
)
def test_hold_marker_entry_forms(tmp_path, text, expected):
    assert menubar.read_hold_marker(_marker(tmp_path, text + "\n")) == expected


def test_hold_marker_reads_the_live_shape(tmp_path):
    text = "2:dharvishut@gmail.com:2026-10-23T01:10:00.372185+00:00\n"
    when = _dt.datetime(2026, 10, 23, 1, 10, 0, 372185, tzinfo=_dt.timezone.utc).timestamp()
    assert menubar.read_hold_marker(_marker(tmp_path, text)) == {"2": ("dharvishut@gmail.com", when, None)}


def test_hold_marker_two_entries(tmp_path):
    text = f"2:b@x.com:{_hold_iso(1680)}\n3:c@x.com:{_hold_iso(60)}:released\n"
    assert menubar.read_hold_marker(_marker(tmp_path, text)) == {
        "2": ("b@x.com", _HOLD_NOW + 1680, None),
        "3": ("c@x.com", _HOLD_NOW + 60, "released"),
    }


def test_hold_marker_default_path_is_the_daemons_under_home(tmp_path, monkeypatch):
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    path = menubar.hold_marker_path()
    assert path == tmp_path / "Library" / "Logs" / "cswap-rotate.hold5h"
    assert menubar.read_hold_marker() == {}  # not there yet
    path.parent.mkdir(parents=True)
    path.write_text(f"2:b@x.com:{_hold_iso(60)}\n", encoding="utf-8")
    assert menubar.read_hold_marker() == {"2": ("b@x.com", _HOLD_NOW + 60, None)}


_UNTIL = _HOLD_NOW + 28 * 60


_NOT_HELD = "not held"


@pytest.mark.parametrize(
    "holds, num, email, held_until",
    [
        ({"2": ("b@x.com", _UNTIL, None)}, "2", "b@x.com", _UNTIL),
        ({"2": ("b@x.com", _UNTIL, None)}, 2, "b@x.com", _UNTIL),  # an int slot
        ({"2": ("b@x.com", None, None)}, "2", "b@x.com", None),  # held, no release time recorded
        # Still the guard's while the release is under way.
        ({"2": ("b@x.com", _UNTIL, "releasing")}, "2", "b@x.com", _UNTIL),
        # The guard is done with it: a slot still disabled is the owner's.
        ({"2": ("b@x.com", _UNTIL, "released")}, "2", "b@x.com", _NOT_HELD),
        # A hold belongs to the account it was taken on, not the slot.
        ({"2": ("b@x.com", _UNTIL, None)}, "2", "other@x.com", _NOT_HELD),
        ({"2": ("b@x.com", _UNTIL, None)}, "3", "b@x.com", _NOT_HELD),
        ({"2": (None, None, None)}, "2", "b@x.com", _NOT_HELD),  # a bare slot names no account
        ({}, "2", "b@x.com", _NOT_HELD),
        (None, "2", "b@x.com", _NOT_HELD),  # unknown: never held
    ],
)
def test_guard_hold_needs_the_slot_and_the_email(holds, num, email, held_until):
    expected = None if held_until == _NOT_HELD else menubar.GuardHold(held_until)
    assert menubar.guard_hold(holds, num, email) == expected


@pytest.mark.parametrize(
    "held_until, expected",
    [
        (_UNTIL, "held · 28m"),
        (_HOLD_NOW + 2 * 3600 + 5 * 60, "held · 2h 5m"),  # the rows' countdown format
        (_HOLD_NOW + 30, "held · 0m"),
        (_HOLD_NOW, "held"),  # due now
        (_HOLD_NOW - 60, "held"),  # past: the guard releases it on its next tick
        (None, "held"),  # no release time recorded
        (_NOT_HELD, "paused"),
    ],
)
def test_out_of_rotation_word(held_until, expected):
    hold = None if held_until == _NOT_HELD else menubar.GuardHold(held_until)
    assert menubar.out_of_rotation_word(hold, _HOLD_NOW) == expected


def _held_label(holds, disabled=True, email="loc@papaya.asia"):
    return menubar.format_account_label(2, email, _USAGE, _HOLD_NOW, disabled=disabled, holds=holds)


def test_format_account_label_says_held_with_its_countdown():
    holds = {"2": ("loc@papaya.asia", _UNTIL, None)}
    assert _held_label(holds) == "2  loc@papaya.asia  (held · 28m)  5h 42% · 7d 18% · $ 30%"


def test_format_account_label_held_past_its_release_is_just_held():
    holds = {"2": ("loc@papaya.asia", _HOLD_NOW - 60, None)}
    assert _held_label(holds) == "2  loc@papaya.asia  (held)  5h 42% · 7d 18% · $ 30%"


@pytest.mark.parametrize(
    "holds",
    [
        {},  # no marker file
        None,  # a marker that cannot be read
        {"2": ("someone@else.com", _UNTIL, None)},  # the slot now holds another account
        {"2": ("loc@papaya.asia", _UNTIL, "released")},  # the guard let it go; the owner disabled it since
        {"3": ("loc@papaya.asia", _UNTIL, None)},  # another slot
    ],
)
def test_format_account_label_paused_when_the_guard_does_not_hold_it(holds):
    assert _held_label(holds) == "2  loc@papaya.asia  (paused)  5h 42% · 7d 18% · $ 30%"


def test_format_account_label_says_nothing_for_an_enabled_account_in_the_marker():
    # Not disabled: whatever the marker says, it is in rotation.
    holds = {"2": ("loc@papaya.asia", _UNTIL, None)}
    assert _held_label(holds, disabled=False) == "2  loc@papaya.asia  5h 42% · 7d 18% · $ 30%"


# --- usage logging -------------------------------------------------------------

def test_format_usage_log_full():
    usage = {
        "five_hour": {"pct": 35.0, "clock": "06:59"},
        "seven_day": {"pct": 55.0, "clock": "Jun 29 21:59"},
    }
    assert menubar.format_usage_log("a@x.com", usage) == (
        "usage a@x.com: 5h 35% (resets 06:59) · 7d 55% (resets Jun 29 21:59)"
    )


def test_format_usage_log_without_clock():
    usage = {"five_hour": {"pct": 0.0}, "seven_day": {"pct": 12.0}}
    assert menubar.format_usage_log("a@x.com", usage) == "usage a@x.com: 5h 0% · 7d 12%"


def test_format_usage_log_partial_window():
    usage = {"seven_day": {"pct": 12.0, "clock": "Jul 3"}}
    assert menubar.format_usage_log("a@x.com", usage) == "usage a@x.com: 7d 12% (resets Jul 3)"


def test_format_usage_log_none_when_no_numeric_window():
    assert menubar.format_usage_log("a@x.com", None) is None
    assert menubar.format_usage_log("a@x.com", "rate limited") is None
    assert menubar.format_usage_log("a@x.com", {"spend": {"pct": 5.0}}) is None


def test_usage_log_key_ignores_clock_tracks_pct():
    u1 = {"five_hour": {"pct": 35.0, "clock": "06:59"}, "seven_day": {"pct": 55.0}}
    u2 = {"five_hour": {"pct": 35.0, "clock": "07:59"}, "seven_day": {"pct": 55.0}}
    u3 = {"five_hour": {"pct": 36.0}, "seven_day": {"pct": 55.0}}
    assert menubar._usage_log_key(u1) == menubar._usage_log_key(u2)  # clock-only change
    assert menubar._usage_log_key(u1) != menubar._usage_log_key(u3)  # pct change
    assert menubar._usage_log_key(None) == (None, None)


# --- title ---------------------------------------------------------------------

def test_format_title_name_and_5h():
    s = menubar.MenuBarSettings(show_account_name=True, title_pct="5h")
    assert menubar.format_title("loc@papaya.asia", _USAGE, s) == "⇄ loc · 42%"


def test_format_title_prefers_alias_over_local_part():
    s = menubar.MenuBarSettings(show_account_name=True, title_pct="off")
    assert menubar.format_title("loc@papaya.asia", _USAGE, s, alias="dev") == "⇄ dev"


def test_format_title_name_only_when_pct_off():
    s = menubar.MenuBarSettings(show_account_name=True, title_pct="off")
    assert menubar.format_title("loc@papaya.asia", _USAGE, s) == "⇄ loc"


def test_format_title_5h_only():
    s = menubar.MenuBarSettings(show_account_name=False, title_pct="5h")
    assert menubar.format_title("loc@papaya.asia", _USAGE, s) == "⇄ 42%"


def test_format_title_7d_only():
    s = menubar.MenuBarSettings(show_account_name=False, title_pct="7d")
    assert menubar.format_title("loc@papaya.asia", _USAGE, s) == "⇄ 18%"


def test_format_title_both_windows():
    s = menubar.MenuBarSettings(show_account_name=False, title_pct="both")
    assert menubar.format_title("loc@papaya.asia", _USAGE, s) == "⇄ 42% · 18%"


def test_format_title_both_windows_with_name():
    s = menubar.MenuBarSettings(show_account_name=True, title_pct="both")
    assert menubar.format_title("loc@papaya.asia", _USAGE, s) == "⇄ loc · 42% · 18%"


def test_format_title_icon_only_when_off():
    s = menubar.MenuBarSettings(show_account_name=False, title_pct="off")
    assert menubar.format_title("loc@papaya.asia", _USAGE, s) == "⇄"


def test_format_title_scoped_appends_model_limits():
    # title_pct="off" + title_scoped gives a title tracking only the scoped model
    s = menubar.MenuBarSettings(show_account_name=True, title_pct="off", title_scoped=True)
    usage = {**_USAGE, "scoped": [{"name": "Fable", "pct": 55.0}]}
    assert menubar.format_title("loc@papaya.asia", usage, s) == "⇄ loc · Fable 55%"


def test_format_title_scoped_after_windows_multiple_models():
    s = menubar.MenuBarSettings(show_account_name=False, title_pct="both", title_scoped=True)
    usage = {
        **_USAGE,
        "scoped": [{"name": "Fable", "pct": 55.0}, {"name": "Opus", "pct": 7.0}],
    }
    assert menubar.format_title("loc@papaya.asia", usage, s) == "⇄ 42% · 18% · Fable 55% · Opus 7%"


def test_format_title_scoped_off_by_default():
    # default settings ignore scoped windows entirely
    s = menubar.MenuBarSettings(show_account_name=False, title_pct="off")
    usage = {**_USAGE, "scoped": [{"name": "Fable", "pct": 55.0}]}
    assert not s.title_scoped
    assert menubar.format_title("loc@papaya.asia", usage, s) == "⇄"


def test_format_title_icon_only_when_no_active_account():
    s = menubar.MenuBarSettings(show_account_name=True, title_pct="both")
    assert menubar.format_title(None, None, s) == "⇄"


def test_format_title_truncates_long_local_part():
    s = menubar.MenuBarSettings(show_account_name=True, title_pct="off")
    title = menubar.format_title("averylonglocalpart@example.com", None, s)
    assert title == "⇄ averylonglo*"  # 12 chars: 11 letters + asterisk marker


def test_format_title_both_drops_unavailable_windows():
    s = menubar.MenuBarSettings(show_account_name=False, title_pct="both")
    assert menubar.format_title("loc@x.com", "no credentials", s) == "⇄"


def test_format_title_both_keeps_available_window():
    s = menubar.MenuBarSettings(show_account_name=False, title_pct="both")
    # only 5h present -> 7d dropped, no trailing separator
    assert menubar.format_title("loc@x.com", {"five_hour": {"pct": 9.0}}, s) == "⇄ 9%"


# --- reset-time helpers --------------------------------------------------------

def test_resets_at_ts_orders_and_handles_missing():
    early = {"resets_at": "2026-06-24T07:00:00+00:00"}
    late = {"resets_at": "2026-06-26T07:00:00+00:00"}
    assert menubar._resets_at_ts(early) < menubar._resets_at_ts(late)
    assert menubar._resets_at_ts({"pct": 5.0}) == float("inf")   # no resets_at
    assert menubar._resets_at_ts({"resets_at": "garbage"}) == float("inf")
    assert menubar._resets_at_ts(None) == float("inf")


_NOW = 1_000_000.0


def _iso(delta_s):  # ISO-8601 for _NOW + delta_s, UTC
    return _dt.datetime.fromtimestamp(_NOW + delta_s, _dt.timezone.utc).isoformat()


def test_live_countdown_formats_from_resets_at():
    assert menubar._live_countdown({"resets_at": _iso(9 * 3600 + 5 * 60)}, _NOW) == "9h 5m"
    assert menubar._live_countdown({"resets_at": _iso(86400 + 19 * 3600)}, _NOW) == "1d 19h"
    assert menubar._live_countdown({"resets_at": _iso(34 * 60)}, _NOW) == "34m"


def test_live_countdown_none_when_passed_or_missing():
    assert menubar._live_countdown({"resets_at": _iso(-60)}, _NOW) is None   # already reset
    assert menubar._live_countdown({"pct": 5.0}, _NOW) is None               # no resets_at
    assert menubar._live_countdown("no credentials", _NOW) is None


def test_usage_summary_live_countdown_from_resets_at():
    usage = {
        "five_hour": {"pct": 42.0, "resets_at": _iso(2 * 3600 + 33 * 60)},
        "seven_day": {"pct": 18.0, "resets_at": _iso(86400 + 19 * 3600)},
        "spend": {"pct": 30.0},
    }
    assert menubar.usage_summary(usage, _NOW) == "5h 42% (2h 33m) · 7d 18% (1d 19h) · $ 30%"


def test_usage_summary_omits_countdown_when_passed_or_missing():
    # 5h reset already passed (stale data) -> omit; 7d has no resets_at -> omit
    usage = {"five_hour": {"pct": 53.0, "resets_at": _iso(-60)}, "seven_day": {"pct": 8.0}}
    assert menubar.usage_summary(usage, _NOW) == "5h 53% · 7d 8%"


# --- switch-history log parsing ------------------------------------------------

_SWITCH_LOG = (
    "2026-06-27 00:57:50,178 - INFO - Switched from account 1 to 3\n"
    "2026-06-27 02:06:21,302 - INFO - usage a@x.com: 5h 10%\n"
    "2026-06-27 02:10:00,000 - INFO - Switched from account 3 to 1\n"
)


def test_parse_switch_history_most_recent_first():
    assert menubar.parse_switch_history(_SWITCH_LOG) == [
        "3 → 1   2026-06-27 02:10",
        "1 → 3   2026-06-27 00:57",
    ]


def test_parse_switch_history_respects_limit():
    lines = "\n".join(
        f"2026-06-27 0{i}:00:00,000 - INFO - Switched from account 1 to 2"
        for i in range(1, 6)
    )
    out = menubar.parse_switch_history(lines, limit=2)
    assert len(out) == 2
    assert out[0] == "1 → 2   2026-06-27 05:00"  # newest first


def test_parse_switch_history_empty_or_no_matches():
    assert menubar.parse_switch_history("") == []
    assert menubar.parse_switch_history("nothing relevant here") == []


# --- snapshot adapter (fakes for AccountsSnapshot / UsageEntry) -----------------

class _FakeEntry:
    def __init__(self, sentinel=None, last_good=None, fetched_at=None):
        self.sentinel = sentinel
        self.last_good = last_good
        self.fetched_at = fetched_at


class _FakeAcct:
    def __init__(self, number, email, is_active, usage, alias="", disabled=False):
        self.number = number
        self.email = email
        self.is_active = is_active
        self.usage = usage
        self.alias = alias
        self.disabled = disabled


class _FakeSnap:
    def __init__(self, accounts):
        self.accounts = accounts


def test_account_display_usage_sentinel_note_last_good_or_none():
    assert menubar._account_display_usage(
        _FakeEntry(sentinel=USAGE_API_KEY)
    ) == menubar.SENTINEL_NOTES[USAGE_API_KEY]
    lg = {"five_hour": {"pct": 5.0}}
    assert menubar._account_display_usage(_FakeEntry(last_good=lg)) == lg
    assert menubar._account_display_usage(_FakeEntry()) is None


def test_adapt_snapshot_shape_and_active_selection():
    # _adapt_snapshot is a pure transform of an AccountsSnapshot (the fetch
    # pacing now lives in SnapshotSource, tested separately).
    lg = {"five_hour": {"pct": 10.0}, "seven_day": {"pct": 20.0}}
    accts = [
        _FakeAcct("1", "a@x.com", True, _FakeEntry(last_good=lg, fetched_at=123.0)),
        _FakeAcct("2", "b@x.com", False, _FakeEntry(sentinel=USAGE_API_KEY), disabled=True),
    ]
    snap = menubar._adapt_snapshot(_FakeSnap(accts))
    assert snap["active_email"] == "a@x.com"
    assert snap["active_usage"] == lg
    assert snap["active_alias"] == ""
    # (num, email, is_active, display_usage, last_good, alias, disabled, fetched_at)
    assert snap["accounts"][0] == ("1", "a@x.com", True, lg, lg, "", False, 123.0)
    # sentinel account: display is the human note, last_good/fetched_at are None; disabled carried through
    assert snap["accounts"][1] == (
        "2", "b@x.com", False, menubar.SENTINEL_NOTES[USAGE_API_KEY], None, "", True, None,
    )


def test_adapt_snapshot_empty():
    assert menubar._adapt_snapshot(_FakeSnap([])) == menubar.EMPTY_SNAPSHOT


def test_adapt_snapshot_reads_the_hold_marker_once(tmp_path, monkeypatch):
    path = _marker(tmp_path, f"2:b@x.com:{_hold_iso(60)}\n")
    reads = []
    real = menubar.read_hold_marker
    monkeypatch.setattr(menubar, "read_hold_marker", lambda p=None: reads.append(p) or real(p))
    accts = [
        _FakeAcct("1", "a@x.com", True, _FakeEntry(last_good={"five_hour": {"pct": 1.0}})),
        _FakeAcct("2", "b@x.com", False, _FakeEntry(), disabled=True),
    ]
    snap = menubar._adapt_snapshot(_FakeSnap(accts), marker_path=path)
    assert reads == [path]  # once for the whole snapshot, not once per account
    assert snap["holds"] == {"2": ("b@x.com", _HOLD_NOW + 60, None)}


def test_adapt_snapshot_reads_the_daemons_marker_by_default(tmp_path, monkeypatch):
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    path = menubar.hold_marker_path()
    path.parent.mkdir(parents=True)
    path.write_text(f"2:b@x.com:{_hold_iso(60)}\n", encoding="utf-8")
    assert menubar._adapt_snapshot(_FakeSnap([]))["holds"] == {"2": ("b@x.com", _HOLD_NOW + 60, None)}


def test_adapt_snapshot_keeps_an_unreadable_marker_unknown(tmp_path):
    assert menubar._adapt_snapshot(_FakeSnap([]), marker_path=tmp_path)["holds"] is None


# --- weekly reset roll-forward (static 7-day cadence) --------------------------

def test_rolled_weekly_window_advances_passed_reset():
    w = {"pct": 95.0, "resets_at": _iso(-3 * 86400), "countdown": "stale", "clock": "old"}
    rolled = menubar._rolled_weekly_window(w, _NOW)
    assert rolled["pct"] == 0.0  # the window objectively rolled over
    assert abs(menubar._resets_at_ts(rolled) - (_NOW + 4 * 86400)) < 1
    assert "countdown" not in rolled and "clock" not in rolled  # stale strings dropped


def test_rolled_weekly_window_advances_multiple_missed_weeks():
    w = {"pct": 80.0, "resets_at": _iso(-10 * 86400)}  # two boundaries crossed
    rolled = menubar._rolled_weekly_window(w, _NOW)
    assert abs(menubar._resets_at_ts(rolled) - (_NOW + 4 * 86400)) < 1


def test_rolled_weekly_window_leaves_future_or_unknown_untouched():
    future = {"pct": 42.0, "resets_at": _iso(2 * 86400)}
    assert menubar._rolled_weekly_window(future, _NOW) is future
    no_reset = {"pct": 42.0}
    assert menubar._rolled_weekly_window(no_reset, _NOW) is no_reset
    assert menubar._rolled_weekly_window(None, _NOW) is None


def test_usage_summary_reflects_passed_weekly_reset():
    # 7d reset a day ago: show it as reset (0%) with the next weekly boundary,
    # from the static schedule alone. 5h is untouched (dynamic session window).
    usage = {
        "five_hour": {"pct": 10.0},
        "seven_day": {"pct": 95.0, "resets_at": _iso(-86400)},
    }
    assert menubar.usage_summary(usage, _NOW) == "5h 10% · 7d 0% (6d 0h)"


def test_usage_summary_scoped_reflects_passed_weekly_reset():
    usage = {"scoped": [{"name": "Fable", "pct": 100.0, "resets_at": _iso(-86400)}]}
    # rolled to 0% → the over-limit "(!)" marker is gone too
    assert menubar.usage_summary(usage, _NOW) == "Fable 0% (6d 0h)"


def test_format_title_reflects_passed_weekly_reset():
    s = menubar.MenuBarSettings(show_account_name=False, title_pct="7d")
    usage = {"seven_day": {"pct": 95.0, "resets_at": _iso(-86400)}}
    assert menubar.format_title("a@x.com", usage, s, _NOW) == "⇄ 0%"


# --- run() app glue ------------------------------------------------------------

def test_run_without_rumps_raises_clean_error(monkeypatch):
    """A missing menubar extra surfaces as ClaudeSwitchError, not a traceback.

    The module is import-safe without rumps, so the CLI's ImportError guard
    around ``from claude_swap.menubar import run`` can never fire — the import
    failure happens inside ``run()``. Blocking the import (a ``None`` entry in
    ``sys.modules`` makes ``import rumps`` raise) checks that ``run()`` turns
    it into the error type the CLI renders with the install hint.
    """
    monkeypatch.setitem(sys.modules, "rumps", None)
    with pytest.raises(ClaudeSwitchError, match=r"claude-swap\[menubar\]"):
        menubar.run(switcher=None)


# --- open dashboard -------------------------------------------------------------

def _no_which(_name):
    return None


def test_dashboard_executable_uses_launching_cswap(tmp_path: Path):
    exe = tmp_path / "venv" / "bin" / "cswap"
    cmd = menubar.dashboard_executable(
        argv0=str(exe), which=lambda _n: "/elsewhere/cswap", python="/py"
    )
    assert cmd == [str(exe), "watch"]


def test_dashboard_executable_accepts_claude_swap_name(tmp_path: Path):
    exe = tmp_path / "bin" / "claude-swap"
    cmd = menubar.dashboard_executable(argv0=str(exe), which=_no_which, python="/py")
    assert cmd == [str(exe), "watch"]


def test_dashboard_executable_makes_relative_argv0_absolute(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cmd = menubar.dashboard_executable(argv0="bin/cswap", which=_no_which, python="/py")
    assert cmd == [str(tmp_path / "bin" / "cswap"), "watch"]


def test_dashboard_executable_module_launch_beats_competing_path_install(tmp_path: Path):
    # `/venv-A/bin/python -m claude_swap menubar` with venv B's cswap on PATH:
    # the dashboard must stay on install A, so PATH is never consulted.
    main_py = tmp_path / "venv-A" / "lib" / "site-packages" / "claude_swap" / "__main__.py"
    seen = []

    def which(name):
        seen.append(name)
        return "/venv-B/bin/cswap"

    cmd = menubar.dashboard_executable(
        argv0=str(main_py), which=which, python="/venv-A/bin/python"
    )
    assert cmd == ["/venv-A/bin/python", "-m", "claude_swap", "watch"]
    assert seen == []


def test_dashboard_executable_other_packages_main_uses_path():
    # Only this package's __main__.py is a module launch of cswap; another
    # tool's __main__.py (e.g. a test runner) falls through to PATH.
    seen = []

    def which(name):
        seen.append(name)
        return "/opt/bin/cswap" if name == "cswap" else None

    cmd = menubar.dashboard_executable(
        argv0="/usr/lib/python3/site-packages/pytest/__main__.py", which=which, python="/py"
    )
    assert cmd == [os.path.abspath("/opt/bin/cswap"), "watch"]
    assert seen == ["cswap"]


def test_dashboard_executable_path_lookup_is_made_absolute(tmp_path: Path, monkeypatch):
    # PATH=.venv/bin:... makes which() return a relative path, which breaks
    # once Terminal starts the command in $HOME.
    monkeypatch.chdir(tmp_path)
    cmd = menubar.dashboard_executable(
        argv0="/x/python", which=lambda n: ".venv/bin/cswap" if n == "cswap" else None, python="/py"
    )
    assert cmd == [str(tmp_path / ".venv" / "bin" / "cswap"), "watch"]


def test_dashboard_executable_bare_argv0_lookup_is_made_absolute(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cmd = menubar.dashboard_executable(
        argv0="cswap", which=lambda n: "bin/cswap" if n == "cswap" else None, python="/py"
    )
    assert cmd == [str(tmp_path / "bin" / "cswap"), "watch"]


def test_dashboard_executable_tries_claude_swap_after_cswap():
    def which(name):
        return "/opt/bin/claude-swap" if name == "claude-swap" else None

    cmd = menubar.dashboard_executable(argv0="/x/python", which=which, python="/py")
    assert cmd == [os.path.abspath("/opt/bin/claude-swap"), "watch"]


def test_dashboard_executable_falls_back_to_python_module():
    cmd = menubar.dashboard_executable(argv0="/x/python", which=_no_which, python="/v/bin/python")
    assert cmd == ["/v/bin/python", "-m", "claude_swap", "watch"]


def test_dashboard_executable_defaults_to_sys_argv0(monkeypatch, tmp_path: Path):
    exe = tmp_path / "cswap"
    monkeypatch.setattr(sys, "argv", [str(exe)])
    assert menubar.dashboard_executable(which=_no_which, python="/py") == [str(exe), "watch"]


class _Proc:
    def __init__(self, returncode, stderr=""):
        self.returncode = returncode
        self.stderr = stderr
        self.stdout = ""


_ENV_NONE = {"CLAUDE_CONFIG_DIR": None, "CLAUDE_SECURESTORAGE_CONFIG_DIR": None,
             "XDG_DATA_HOME": None, "PYTHONPATH": None}


def _open(script_dir: Path, run, create=None, cwd="/w", env=None):
    kwargs = {} if create is None else {"create": create}
    return menubar.open_dashboard_terminal(
        ["/bin/cswap", "watch"], script_dir, cwd=cwd,
        env=_ENV_NONE if env is None else env, run=run, **kwargs,
    )


def test_open_dashboard_terminal_writes_script_then_opens_it_in_terminal(tmp_path: Path):
    calls = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return _Proc(0)

    ok, _msg = _open(tmp_path, run)
    assert ok is True
    argv, kwargs = calls[0]
    # By bundle id, so a renamed Terminal.app still resolves.
    assert argv[:3] == ["open", "-b", "com.apple.Terminal"]
    script = Path(argv[3])
    assert script.parent == tmp_path
    assert script.name.startswith("open-dashboard.") and script.name.endswith(".command")
    assert script.read_text() == menubar.dashboard_script(["/bin/cswap", "watch"], env=_ENV_NONE, cwd="/w")
    assert kwargs.get("check") is False
    assert kwargs.get("timeout") == 30


def test_open_dashboard_terminal_uses_a_fresh_file_per_launch(tmp_path: Path):
    # Two menu bars (different profiles, same backup dir) must never share a
    # script: B's write could otherwise replace A's before Terminal reads it.
    opened = []
    run = lambda argv, **kw: opened.append(Path(argv[3])) or _Proc(0)  # noqa: E731
    env_a = dict(_ENV_NONE, CLAUDE_CONFIG_DIR="/profile/a")
    env_b = dict(_ENV_NONE, CLAUDE_CONFIG_DIR="/profile/b")
    assert _open(tmp_path, run, cwd="/cwd/a", env=env_a)[0]
    assert _open(tmp_path, run, cwd="/cwd/b", env=env_b)[0]
    a, b = opened
    assert a != b
    assert a.read_text() == menubar.dashboard_script(["/bin/cswap", "watch"], env=env_a, cwd="/cwd/a")
    assert b.read_text() == menubar.dashboard_script(["/bin/cswap", "watch"], env=env_b, cwd="/cwd/b")


def _no_files(directory: Path) -> bool:
    return list(directory.iterdir()) == []


def test_open_dashboard_terminal_missing_terminal_reports_open_error(tmp_path: Path):
    def run(argv, **kwargs):
        return _Proc(1, stderr="Unable to find application with bundle identifier com.apple.Terminal\n")

    ok, msg = _open(tmp_path, run)
    assert ok is False
    assert "Unable to find application" in msg
    assert _no_files(tmp_path)  # nobody will ever run that script


def test_open_dashboard_terminal_nonzero_exit_without_stderr(tmp_path: Path):
    ok, msg = _open(tmp_path, lambda argv, **kw: _Proc(1))
    assert ok is False
    assert msg
    assert _no_files(tmp_path)


def test_open_dashboard_terminal_missing_open_command(tmp_path: Path):
    def run(argv, **kwargs):
        raise FileNotFoundError(2, "No such file or directory", "open")

    ok, msg = _open(tmp_path, run)
    assert ok is False
    assert "open" in msg
    assert _no_files(tmp_path)


def test_open_dashboard_terminal_timeout(tmp_path: Path):
    def run(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, kwargs.get("timeout") or 0)

    ok, msg = _open(tmp_path, run)
    assert ok is False
    assert msg
    assert _no_files(tmp_path)


def test_open_dashboard_terminal_create_failure_never_runs_open(tmp_path: Path):
    ran = []

    def create(_directory, _text):
        raise PermissionError(13, "Permission denied")

    ok, msg = _open(tmp_path, lambda argv, **kw: ran.append(argv) or _Proc(0), create=create)
    assert ok is False
    assert "Permission denied" in msg
    assert ran == []


def test_open_dashboard_terminal_missing_backup_dir_fails_cleanly(tmp_path: Path):
    ran = []
    ok, msg = _open(tmp_path / "gone", lambda argv, **kw: ran.append(argv) or _Proc(0))
    assert ok is False
    assert "launcher script" in msg
    assert ran == []


def test_dashboard_executable_resolves_bare_argv0_on_path():
    # A bare name has no directory to anchor it; abspath would invent one
    # under the cwd, so it must be looked up on PATH instead.
    cmd = menubar.dashboard_executable(
        argv0="cswap", which=lambda n: "/opt/bin/cswap" if n == "cswap" else None, python="/py"
    )
    assert cmd == [os.path.abspath("/opt/bin/cswap"), "watch"]


# --- dashboard environment ------------------------------------------------------

_KEYS = ("CLAUDE_CONFIG_DIR", "CLAUDE_SECURESTORAGE_CONFIG_DIR", "XDG_DATA_HOME", "PYTHONPATH", "HOME")


def test_dashboard_env_keys_are_exactly_the_profile_and_import_keys():
    assert menubar.DASHBOARD_ENV_KEYS == _KEYS


def test_dashboard_env_marks_every_key_set_or_unset():
    environ = {
        "CLAUDE_CONFIG_DIR": "/profiles/work",
        "PATH": "/usr/bin",
        "HOME": "/Users/x",
        "TERM": "xterm-256color",
    }
    assert menubar.dashboard_env(environ) == {
        "CLAUDE_CONFIG_DIR": "/profiles/work",
        "CLAUDE_SECURESTORAGE_CONFIG_DIR": None,
        "XDG_DATA_HOME": None,
        "PYTHONPATH": None,
        "HOME": "/Users/x",
    }


def test_dashboard_env_forwards_home():
    # The account store (~/.claude-swap-backup) and the default profile both
    # resolve through HOME, so the dashboard must use the menu bar's.
    assert menubar.dashboard_env({"HOME": "/Users/menu"})["HOME"] == "/Users/menu"
    assert menubar.dashboard_env({})["HOME"] is None


def test_dashboard_env_keeps_empty_value_as_set():
    # An empty CLAUDE_SECURESTORAGE_CONFIG_DIR selects the default keychain
    # service, which differs from it being unset.
    env = menubar.dashboard_env({"CLAUDE_SECURESTORAGE_CONFIG_DIR": ""})
    assert env["CLAUDE_SECURESTORAGE_CONFIG_DIR"] == ""


def test_dashboard_env_forwards_pythonpath():
    env = menubar.dashboard_env({"PYTHONPATH": "src"})
    assert env["PYTHONPATH"] == "src"


def test_dashboard_env_defaults_to_process_environment(monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/from/process")
    for key in _KEYS[1:]:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("HOME", "/from/process/home")
    assert menubar.dashboard_env() == {
        "CLAUDE_CONFIG_DIR": "/from/process",
        "CLAUDE_SECURESTORAGE_CONFIG_DIR": None,
        "XDG_DATA_HOME": None,
        "PYTHONPATH": None,
        "HOME": "/from/process/home",
    }


def _exec_argv(script: str) -> list[str]:
    # The command line between "then" and the status capture; quoted values
    # may themselves contain newlines, so this is not a line-based split.
    body = script.split("; then\n    ", 1)[1].split("\n    dashboard_status=$?", 1)[0]
    return shlex.split(body)


def test_dashboard_cwd_returns_current_directory():
    assert menubar.dashboard_cwd(getcwd=lambda: "/work dir") == "/work dir"


def test_dashboard_cwd_failure_returns_none():
    def gone():
        raise FileNotFoundError(2, "No such file or directory")

    assert menubar.dashboard_cwd(getcwd=gone) is None


def test_dashboard_script_exact_text_for_awkward_cwd():
    cwd = "/Users/o'brien/My \"50%\" dir\nété"
    script = menubar.dashboard_script(["/bin/cswap", "watch"], env=_ENV_NONE, cwd=cwd)
    quoted_cwd = "'/Users/o'\"'\"'brien/My \"50%\" dir\nété'"
    assert script == (
        "#!/bin/sh\n"
        'rm -f -- "$0"\n'
        f"if cd {quoted_cwd}; then\n"
        "    /usr/bin/env -u CLAUDE_CONFIG_DIR -u CLAUDE_SECURESTORAGE_CONFIG_DIR"
        " -u XDG_DATA_HOME -u PYTHONPATH /bin/cswap watch\n"
        "    dashboard_status=$?\n"
        '    if [ "$dashboard_status" -ne 0 ]; then\n'
        "        printf '%s\\n' \"cswap: the dashboard exited with status $dashboard_status."
        " Run: cswap watch\" >&2\n"
        "        printf '%s\\n' 'Press Return to close this window.' >&2\n"
        "        read -r _ || :\n"
        "    fi\n"
        "else\n"
        "    printf '%s\\n' 'cswap: cannot open the dashboard from "
        "/Users/o'\"'\"'brien/My \"50%\" dir\nété "
        "(Terminal may not have access to it). Run: cswap watch' >&2\n"
        "    printf '%s\\n' 'Press Return to close this window.' >&2\n"
        "    read -r _ || :\n"
        "fi\n"
    )


def test_dashboard_script_has_no_exit_or_exec():
    # Nothing may end the window early, and the script must outlive the
    # dashboard so it can report a failed start.
    words = menubar.dashboard_script(["/bin/cswap", "watch"], env=_ENV_NONE, cwd="/w").split()
    assert "exit" not in words
    assert "exec" not in words


def test_dashboard_script_unsets_missing_keys_and_sets_present_ones():
    env = {"CLAUDE_CONFIG_DIR": "/p", "CLAUDE_SECURESTORAGE_CONFIG_DIR": None,
           "XDG_DATA_HOME": "", "PYTHONPATH": None}
    argv = _exec_argv(menubar.dashboard_script(["/bin/cswap", "watch"], env=env, cwd="/w"))
    assert argv == [
        "/usr/bin/env",
        "-u", "CLAUDE_SECURESTORAGE_CONFIG_DIR",
        "-u", "PYTHONPATH",
        "CLAUDE_CONFIG_DIR=/p",
        "XDG_DATA_HOME=",
        "/bin/cswap", "watch",
    ]


def test_dashboard_script_routes_exe_with_equals_sign_through_sh():
    # env takes its first NAME=value operand as an assignment, so a program
    # path containing "=" must not be env's utility operand.
    argv = _exec_argv(menubar.dashboard_script(["/opt/a=b/cswap", "watch"], env=_ENV_NONE, cwd="/w"))
    assert argv[-5:] == ["/bin/sh", "-c", 'exec "$0" "$@"', "/opt/a=b/cswap", "watch"]


@pytest.mark.parametrize(
    "value, path",
    [
        ('/p "q" \\x', '/Users/a b/"q"/cswap'),
        ("/it's 100%", "/Users/o'brien/cswap"),
        ("/line1\nline2", "/tmp/new\nline/cswap"),
        ("/Profils/été 日本", "/Users/üñî/\U0001f600/cswap"),
        ("", "/bin/cswap"),
    ],
)
def test_dashboard_script_quotes_values_and_paths(value, path):
    env = dict(_ENV_NONE, CLAUDE_CONFIG_DIR=value)
    argv = _exec_argv(menubar.dashboard_script([path, "watch"], env=env, cwd="/w"))
    assert argv == [
        "/usr/bin/env",
        "-u", "CLAUDE_SECURESTORAGE_CONFIG_DIR", "-u", "XDG_DATA_HOME", "-u", "PYTHONPATH",
        f"CLAUDE_CONFIG_DIR={value}", path, "watch",
    ]


def test_create_dashboard_script_is_owner_only_executable(tmp_path: Path):
    path = menubar.create_dashboard_script(tmp_path, "#!/bin/sh\necho hi\n")
    assert path.parent == tmp_path
    assert path.name.startswith("open-dashboard.") and path.name.endswith(".command")
    assert path.read_text() == "#!/bin/sh\necho hi\n"
    assert stat.S_IMODE(path.stat().st_mode) == 0o700


def test_create_dashboard_script_never_reuses_a_name(tmp_path: Path):
    paths = {menubar.create_dashboard_script(tmp_path, f"{i}\n") for i in range(20)}
    assert len(paths) == 20
    assert all(p.read_text() == f"{i}\n" for i, p in enumerate(sorted(paths, key=lambda p: int(p.read_text()))))


def test_create_dashboard_script_write_failure_leaves_no_file(tmp_path: Path, monkeypatch):
    real_fdopen = os.fdopen

    class _Broken:
        def __init__(self, f):
            self._f = f

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self._f.close()
            return False

        def write(self, _text):
            raise OSError(5, "Input/output error")

    monkeypatch.setattr(menubar.os, "fdopen", lambda fd, *a, **k: _Broken(real_fdopen(fd, *a, **k)))
    with pytest.raises(OSError):
        menubar.create_dashboard_script(tmp_path, "x\n")
    assert _no_files(tmp_path)


def test_create_dashboard_script_chmod_failure_leaves_no_file(tmp_path: Path, monkeypatch):
    def broken_chmod(*_a, **_k):
        raise OSError(1, "Operation not permitted")

    monkeypatch.setattr(menubar.os, "chmod", broken_chmod)
    with pytest.raises(OSError):
        menubar.create_dashboard_script(tmp_path, "x\n")
    assert _no_files(tmp_path)


def test_create_dashboard_script_missing_directory_raises(tmp_path: Path):
    with pytest.raises(OSError):
        menubar.create_dashboard_script(tmp_path / "gone", "x\n")


def _fake_exe(directory: Path, exit_code: int = 0) -> Path:
    directory.mkdir(parents=True)
    exe = directory / "cswap"
    exe.write_text(
        "#!/bin/sh\n"
        'printf "cwd=%s\\n" "$(pwd -P)"\n'
        'printf "args=%s\\n" "$*"\n'
        "env\n"
        f"exit {exit_code}\n"
    )
    exe.chmod(0o755)
    return exe


def _shell(shell: str) -> str:
    if sys.platform == "win32":
        pytest.skip("POSIX shells and /usr/bin/env")
    path = shutil.which(shell)
    if path is None:
        pytest.skip(f"{shell} not installed")
    return path


# The environment Terminal's shell would have: its own HOME, and rc files that
# export a different profile and a secure-storage override the menu bar never
# had.
def _rc_environment(home: Path) -> dict[str, str]:
    return {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(home),
        "CLAUDE_CONFIG_DIR": "/rc/profile",
        "CLAUDE_SECURESTORAGE_CONFIG_DIR": "/rc/secure",
        "PYTHONPATH": "/rc/pythonpath",
    }


_MENU_HOME = "/Users/menu bar's home"
_MENU_ENV = {"CLAUDE_CONFIG_DIR": "profiles/work", "CLAUDE_SECURESTORAGE_CONFIG_DIR": None,
             "XDG_DATA_HOME": "", "PYTHONPATH": "src", "HOME": _MENU_HOME}
_AWKWARD_CWD = "menu bar's \"50%\" cwd é"


def _create_script(tmp_path: Path, exe: Path, cwd: Path, env=None) -> Path:
    backup = tmp_path / "backup dir"
    backup.mkdir(exist_ok=True)
    return menubar.create_dashboard_script(
        backup,
        menubar.dashboard_script([str(exe), "watch"], env=_MENU_ENV if env is None else env, cwd=str(cwd)),
    )


def _run_script(shell_path, script, tmp_path, stdin=subprocess.DEVNULL):
    return subprocess.run(
        [shell_path, str(script)], env=_rc_environment(tmp_path), cwd=str(tmp_path),
        stdin=stdin, capture_output=True, text=True, check=False,
    )


@pytest.mark.parametrize("shell", ["sh", "bash", "zsh", "dash"])
@pytest.mark.parametrize("exe_dir", ["bin dir", "a=b dir"])
def test_dashboard_script_file_runs_with_menu_bar_profile_and_cwd(tmp_path: Path, shell, exe_dir):
    shell_path = _shell(shell)
    exe = _fake_exe(tmp_path / exe_dir)
    cwd = tmp_path / _AWKWARD_CWD
    cwd.mkdir()
    script = _create_script(tmp_path, exe, cwd)
    proc = _run_script(shell_path, script, tmp_path)
    assert proc.returncode == 0, proc.stderr
    lines = proc.stdout.splitlines()
    assert f"cwd={os.path.realpath(cwd)}" in lines
    assert "args=watch" in lines
    assert "CLAUDE_CONFIG_DIR=profiles/work" in lines
    assert "XDG_DATA_HOME=" in lines
    assert "PYTHONPATH=src" in lines
    assert not any(l.startswith("CLAUDE_SECURESTORAGE_CONFIG_DIR=") for l in lines)
    assert f"HOME={_MENU_HOME}" in lines  # the menu bar's HOME, not Terminal's
    assert "HOME=" + str(tmp_path) not in lines
    assert "PATH=" + _rc_environment(tmp_path)["PATH"] in lines  # the rest passes through
    assert proc.stderr == ""  # a clean exit reports nothing
    assert not script.exists()  # the script removed itself


@pytest.mark.parametrize("shell", ["sh", "bash", "zsh", "dash"])
def test_dashboard_script_file_removes_home_the_menu_bar_lacked(tmp_path: Path, shell):
    shell_path = _shell(shell)
    exe = _fake_exe(tmp_path / "bin dir")
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    script = _create_script(tmp_path, exe, cwd, env=dict(_MENU_ENV, HOME=None))
    proc = _run_script(shell_path, script, tmp_path)
    assert proc.returncode == 0, proc.stderr
    lines = proc.stdout.splitlines()
    assert "args=watch" in lines
    assert not any(l.startswith("HOME=") for l in lines)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX shebang")
def test_dashboard_script_file_runs_directly_by_its_shebang(tmp_path: Path):
    # Terminal executes the file itself, so the shebang and mode must work.
    exe = _fake_exe(tmp_path / "bin dir")
    cwd = tmp_path / _AWKWARD_CWD
    cwd.mkdir()
    script = _create_script(tmp_path, exe, cwd)
    proc = subprocess.run(
        [str(script)], env=_rc_environment(tmp_path), cwd=str(tmp_path),
        stdin=subprocess.DEVNULL, capture_output=True, text=True, check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert "args=watch" in proc.stdout.splitlines()
    assert not script.exists()


@pytest.mark.parametrize("shell", ["sh", "bash", "zsh", "dash"])
@pytest.mark.parametrize("failure", ["missing", "no access"])
def test_dashboard_script_file_reports_unenterable_cwd(tmp_path: Path, shell, failure):
    shell_path = _shell(shell)
    if failure == "no access" and hasattr(os, "geteuid") and os.geteuid() == 0:
        pytest.skip("root enters any directory")
    exe = _fake_exe(tmp_path / "bin dir")
    cwd = tmp_path / _AWKWARD_CWD
    script = _create_script(tmp_path, exe, cwd)
    try:
        if failure == "no access":
            cwd.mkdir()
            cwd.chmod(0)  # inside the try, so the finally always restores it
        proc = _run_script(shell_path, script, tmp_path)
    finally:
        if cwd.exists():
            cwd.chmod(0o755)
    assert f"cswap: cannot open the dashboard from {cwd}" in proc.stderr
    assert "Run: cswap watch" in proc.stderr
    assert "Press Return to close this window." in proc.stderr
    assert "args=" not in proc.stdout  # the dashboard never started
    assert not script.exists()


@pytest.mark.parametrize("shell", ["sh", "bash", "zsh", "dash"])
@pytest.mark.parametrize("case", ["exits 3", "missing exe", "not executable"])
def test_dashboard_script_file_reports_failed_dashboard(tmp_path: Path, shell, case):
    shell_path = _shell(shell)
    if case == "exits 3":
        exe, expected = _fake_exe(tmp_path / "bin dir", exit_code=3), 3
    elif case == "missing exe":
        exe, expected = tmp_path / "gone" / "cswap", 127
    else:
        exe = _fake_exe(tmp_path / "bin dir")
        exe.chmod(0o644)
        expected = 126
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    script = _create_script(tmp_path, exe, cwd)
    proc = _run_script(shell_path, script, tmp_path)
    assert f"cswap: the dashboard exited with status {expected}. Run: cswap watch" in proc.stderr
    assert "Press Return to close this window." in proc.stderr
    assert not script.exists()


def _pauses(tmp_path: Path, script: Path) -> tuple[bool, str]:
    """Whether the script stops for Return, and its stderr once answered.

    No clock decides it: stderr is read to the end, and the script counts as
    waiting when it prints the prompt and then blocks on its read, which
    this answers. A script that exits cleanly just closes stderr. The
    watchdog only bounds a hang (a read with no prompt), and a hang fails.
    """
    proc = subprocess.Popen(
        ["/bin/sh", str(script)], stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE, text=True, env=_rc_environment(tmp_path),
    )
    watchdog = threading.Timer(10, proc.kill)
    watchdog.start()
    waited, err = False, []
    try:
        for line in proc.stderr:
            err.append(line)
            if "Press Return to close this window." in line:
                waited = True
                proc.stdin.write("\n")
                proc.stdin.close()
        proc.wait(timeout=10)
    finally:
        watchdog.cancel()
        if proc.poll() is None:
            proc.kill()
    assert proc.returncode >= 0, "the script hung and was killed"
    return waited, "".join(err)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX shells")
def test_dashboard_script_waits_for_return_after_unenterable_cwd(tmp_path: Path):
    # Terminal runs "<file> ; exit;", and a profile may close the window when
    # the shell exits, so the message must stay up until the user answers.
    script = _create_script(tmp_path, _fake_exe(tmp_path / "bin dir"), tmp_path / "missing")
    waited, err = _pauses(tmp_path, script)
    assert waited
    assert "Press Return to close this window." in err


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX shells")
@pytest.mark.parametrize("exit_code", [3, 127])
def test_dashboard_script_waits_for_return_after_failed_dashboard(tmp_path: Path, exit_code):
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    exe = _fake_exe(tmp_path / "bin dir", exit_code=3) if exit_code == 3 else tmp_path / "gone" / "cswap"
    waited, err = _pauses(tmp_path, _create_script(tmp_path, exe, cwd))
    assert waited
    assert f"exited with status {exit_code}" in err


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX shells")
def test_dashboard_script_does_not_wait_after_clean_exit(tmp_path: Path):
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    waited, err = _pauses(tmp_path, _create_script(tmp_path, _fake_exe(tmp_path / "bin dir"), cwd))
    assert not waited
    assert err == ""


def test_launcher_default_open_uses_process_env_cwd_and_script_dir(monkeypatch, tmp_path: Path):
    seen = {}

    def fake_open(cmd, script_dir, cwd, env=None):
        seen.update(cmd=cmd, script_dir=script_dir, env=env, cwd=cwd)
        return True, "ok"

    monkeypatch.setattr(menubar, "open_dashboard_terminal", fake_open)
    monkeypatch.setattr(menubar, "dashboard_executable", lambda: ["/bin/cswap", "watch"])
    monkeypatch.setattr(menubar, "dashboard_env", lambda: {"CLAUDE_CONFIG_DIR": "/x"})
    monkeypatch.setattr(menubar, "dashboard_cwd", lambda: "/menu/cwd")
    launcher = menubar.DashboardLauncher(_Logger(), tmp_path, start_thread=lambda target: target())
    launcher.start()
    assert seen == {
        "cmd": ["/bin/cswap", "watch"],
        "script_dir": tmp_path,
        "env": {"CLAUDE_CONFIG_DIR": "/x"},
        "cwd": "/menu/cwd",
    }


def test_launcher_refuses_to_open_without_a_working_directory(monkeypatch, tmp_path: Path):
    # Without the cd, relative profile and PYTHONPATH values would resolve
    # against Terminal's directory: a different profile or package. Fail closed.
    opened = []
    monkeypatch.setattr(menubar, "open_dashboard_terminal", lambda *a, **k: opened.append(1) or (True, "ok"))
    monkeypatch.setattr(menubar, "dashboard_executable", lambda: ["/bin/cswap", "watch"])
    monkeypatch.setattr(menubar, "dashboard_env", lambda: {"CLAUDE_CONFIG_DIR": "rel"})
    monkeypatch.setattr(menubar, "dashboard_cwd", lambda: None)
    logger = _Logger()
    launcher = menubar.DashboardLauncher(logger, tmp_path, start_thread=lambda target: target())
    launcher.start()
    assert opened == []
    error = launcher.take_error()
    assert error == menubar.DASHBOARD_CWD_GONE
    assert "working directory" in error and "restart the menu bar" in error
    assert logger.warnings == [f"Could not open dashboard: {error}"]


def test_launcher_refuses_when_the_script_cannot_be_written(monkeypatch, tmp_path: Path):
    ran = []
    monkeypatch.setattr(menubar.subprocess, "run", lambda *a, **k: ran.append(a) or _Proc(0))
    monkeypatch.setattr(menubar, "dashboard_executable", lambda: ["/bin/cswap", "watch"])
    monkeypatch.setattr(menubar, "dashboard_env", lambda: dict(_ENV_NONE))
    monkeypatch.setattr(menubar, "dashboard_cwd", lambda: "/menu/cwd")
    logger = _Logger()
    launcher = menubar.DashboardLauncher(
        logger, tmp_path / "no such dir", start_thread=lambda target: target()
    )
    launcher.start()
    assert ran == []  # Terminal was never asked to open anything
    error = launcher.take_error()
    assert error and "launcher script" in error
    assert logger.warnings == [f"Could not open dashboard: {error}"]


# --- stale launcher sweep ---------------------------------------------------------

def _dead_pid() -> int:
    """A pid that belonged to a process which has exited and been reaped."""
    proc = subprocess.Popen(["sleep", "30"])
    proc.terminate()
    proc.wait(timeout=5)
    return proc.pid


def _launcher(directory: Path, pid, rest: str = "x1y2z3") -> Path:
    path = directory / f"open-dashboard.{pid}.{rest}.command"
    path.write_text("x\n")
    return path


def test_create_dashboard_script_name_carries_the_creating_pid(tmp_path: Path):
    path = menubar.create_dashboard_script(tmp_path, "x\n")
    assert path.name.startswith(f"open-dashboard.{os.getpid()}.")
    assert path.name.endswith(".command")


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX pids and sleep")
def test_sweep_removes_launchers_of_dead_instances(tmp_path: Path):
    dead = _launcher(tmp_path, _dead_pid())
    removed = menubar.sweep_stale_dashboard_scripts(tmp_path)
    assert removed == 1
    assert not dead.exists()


def test_sweep_keeps_this_instances_launchers(tmp_path: Path):
    mine = _launcher(tmp_path, os.getpid())
    assert menubar.sweep_stale_dashboard_scripts(tmp_path) == 0
    assert mine.exists()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX pids and sleep")
def test_sweep_keeps_launchers_of_another_live_instance(tmp_path: Path):
    other = subprocess.Popen(["sleep", "30"])
    try:
        theirs = _launcher(tmp_path, other.pid)
        assert menubar.sweep_stale_dashboard_scripts(tmp_path) == 0
        assert theirs.exists()
    finally:
        other.terminate()
        other.wait(timeout=5)


def test_sweep_keeps_launchers_of_a_pid_it_may_not_signal(tmp_path: Path, monkeypatch):
    # kill(pid, 0) raising PermissionError means the process exists.
    def kill(pid, sig):
        raise PermissionError(1, "Operation not permitted")

    monkeypatch.setattr(menubar.os, "kill", kill)
    theirs = _launcher(tmp_path, 424242)
    assert menubar.sweep_stale_dashboard_scripts(tmp_path) == 0
    assert theirs.exists()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX pids and sleep")
def test_sweep_keeps_names_without_a_parsable_pid(tmp_path: Path):
    names = [
        "open-dashboard.abc123.command",   # no pid segment (older naming)
        "open-dashboard.12ab.x.command",
        "open-dashboard..x.command",
        "open-dashboard.-5.x.command",
        "open-dashboard. 7.x.command",
    ]
    for name in names:
        (tmp_path / name).write_text("x\n")
    assert menubar.sweep_stale_dashboard_scripts(tmp_path) == 0
    assert sorted(p.name for p in tmp_path.iterdir()) == sorted(names)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX pids and sleep")
def test_sweep_keeps_unrelated_names_even_with_a_dead_pid(tmp_path: Path):
    pid = _dead_pid()
    unrelated = [
        tmp_path / "accounts.json",
        tmp_path / f"backup.{pid}.x.command",           # right suffix, wrong prefix
        # Same length as the real prefix, so slicing the prefix off would
        # still find the dead pid: only the prefix check keeps it.
        tmp_path / f"other-launcher.{pid}.x.command",
        tmp_path / f"open-dashboard.{pid}.x.txt",       # right prefix, wrong suffix
    ]
    for p in unrelated:
        p.write_text("x\n")
    assert menubar.sweep_stale_dashboard_scripts(tmp_path) == 0
    assert all(p.exists() for p in unrelated)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX pids and sleep")
def test_sweep_skips_non_regular_entries(tmp_path: Path):
    pid = _dead_pid()
    target = tmp_path / "keep-me.txt"
    target.write_text("x\n")
    link = tmp_path / f"open-dashboard.{pid}.link.command"
    link.symlink_to(target)
    folder = tmp_path / f"open-dashboard.{pid}.dir.command"
    folder.mkdir()
    menubar.sweep_stale_dashboard_scripts(tmp_path)
    assert link.is_symlink() and target.exists()
    assert folder.is_dir()


def test_sweep_missing_directory_is_a_no_op(tmp_path: Path):
    assert menubar.sweep_stale_dashboard_scripts(tmp_path / "gone") == 0


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX pids and sleep")
def test_sweep_unlink_error_does_not_stop_the_others(tmp_path: Path, monkeypatch):
    pid = _dead_pid()
    stuck = _launcher(tmp_path, pid, "stuck")
    others = [_launcher(tmp_path, pid, f"o{i}") for i in range(3)]
    real_unlink = os.unlink

    def unlink(path, *a, **k):
        if os.fspath(path) == str(stuck):
            raise PermissionError(13, "Permission denied")
        return real_unlink(path, *a, **k)

    monkeypatch.setattr(menubar.os, "unlink", unlink)
    assert menubar.sweep_stale_dashboard_scripts(tmp_path) == 3
    assert stuck.exists()
    assert not any(p.exists() for p in others)


def test_launcher_sweeps_stale_scripts_before_creating_a_new_one(monkeypatch, tmp_path: Path):
    order = []
    monkeypatch.setattr(
        menubar, "sweep_stale_dashboard_scripts",
        lambda directory, **k: order.append(("sweep", directory)) or 0,
    )
    monkeypatch.setattr(
        menubar, "open_dashboard_terminal",
        lambda cmd, script_dir, cwd, env=None: order.append(("open", script_dir)) or (True, "ok"),
    )
    monkeypatch.setattr(menubar, "dashboard_executable", lambda: ["/bin/cswap", "watch"])
    monkeypatch.setattr(menubar, "dashboard_env", lambda: dict(_ENV_NONE))
    monkeypatch.setattr(menubar, "dashboard_cwd", lambda: "/menu/cwd")
    launched_on = []
    launcher = menubar.DashboardLauncher(
        _Logger(), tmp_path,
        start_thread=lambda target: launched_on.append(1) or target(),
    )
    launcher.start()
    assert order == [("sweep", tmp_path), ("open", tmp_path)]
    assert launched_on == [1]  # it ran inside the worker, not on the click


def test_launcher_real_sweep_keeps_a_pending_launch(monkeypatch, tmp_path: Path):
    # A launch Terminal has not picked up yet belongs to a live instance (this
    # one) and must survive the next click's sweep, however the clock moves.
    pending = menubar.create_dashboard_script(tmp_path, "x\n")
    old = 1_000_000_000
    os.utime(pending, (old, old))
    monkeypatch.setattr(menubar, "open_dashboard_terminal", lambda *a, **k: (True, "ok"))
    monkeypatch.setattr(menubar, "dashboard_executable", lambda: ["/bin/cswap", "watch"])
    monkeypatch.setattr(menubar, "dashboard_env", lambda: dict(_ENV_NONE))
    monkeypatch.setattr(menubar, "dashboard_cwd", lambda: "/menu/cwd")
    menubar.DashboardLauncher(_Logger(), tmp_path, start_thread=lambda t: t()).start()
    assert pending.exists()


def test_launcher_pid_accepts_up_to_pid_t_max():
    assert menubar._launcher_pid("open-dashboard.2147483647.x.command") == 2**31 - 1
    assert menubar._launcher_pid("open-dashboard.1.x.command") == 1


def test_launcher_pid_rejects_values_above_pid_t_max():
    assert menubar._launcher_pid("open-dashboard.2147483648.x.command") is None
    assert menubar._launcher_pid("open-dashboard.99999999999999999999.x.command") is None


def test_pid_alive_treats_unrepresentable_pid_as_alive():
    # os.kill raises OverflowError past C int range, outside OSError.
    assert menubar._pid_alive(2**31) is True
    assert menubar._pid_alive(2**64) is True


def test_sweep_keeps_oversized_pid_file_and_continues(tmp_path: Path):
    oversized = _launcher(tmp_path, 2**31)
    mine = _launcher(tmp_path, os.getpid())
    assert menubar.sweep_stale_dashboard_scripts(tmp_path) == 0
    assert oversized.exists() and mine.exists()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX pids and sleep")
def test_sweep_survives_an_unexpected_error_on_one_entry(tmp_path: Path, monkeypatch, caplog):
    pid = _dead_pid()
    broken = _launcher(tmp_path, 7777777)
    others = [_launcher(tmp_path, pid, f"o{i}") for i in range(2)]
    real_alive = menubar._pid_alive

    def alive(p):
        if p == 7777777:
            raise RuntimeError("unexpected")
        return real_alive(p)

    monkeypatch.setattr(menubar, "_pid_alive", alive)
    with caplog.at_level(logging.DEBUG, logger="claude-swap"):
        assert menubar.sweep_stale_dashboard_scripts(tmp_path) == 2
    assert broken.exists()
    assert not any(p.exists() for p in others)
    assert any("unexpected" in r.getMessage() or r.exc_info for r in caplog.records)


def test_sweep_never_raises_even_if_listing_breaks(tmp_path: Path, monkeypatch):
    def scandir(_d):
        raise RuntimeError("listing broke")

    monkeypatch.setattr(menubar.os, "scandir", scandir)
    assert menubar.sweep_stale_dashboard_scripts(tmp_path) == 0


def _launch_with_real_sweep(monkeypatch, tmp_path: Path):
    opened = []
    monkeypatch.setattr(
        menubar, "open_dashboard_terminal",
        lambda *a, **k: opened.append(1) or (True, "ok"),
    )
    monkeypatch.setattr(menubar, "dashboard_executable", lambda: ["/bin/cswap", "watch"])
    monkeypatch.setattr(menubar, "dashboard_env", lambda: dict(_ENV_NONE))
    monkeypatch.setattr(menubar, "dashboard_cwd", lambda: "/menu/cwd")
    launcher = menubar.DashboardLauncher(_Logger(), tmp_path, start_thread=lambda t: t())
    launcher.start()
    return opened, launcher.take_error()


def test_oversized_pid_file_never_blocks_the_launch(monkeypatch, tmp_path: Path):
    oversized = _launcher(tmp_path, 2**31)
    for _ in range(2):  # and not on the next click either
        opened, error = _launch_with_real_sweep(monkeypatch, tmp_path)
        assert opened == [1] and error is None
    assert oversized.exists()


def test_broken_entry_never_blocks_the_launch(monkeypatch, tmp_path: Path):
    _launcher(tmp_path, 7777777)

    def alive(_p):
        raise RuntimeError("unexpected")

    monkeypatch.setattr(menubar, "_pid_alive", alive)
    opened, error = _launch_with_real_sweep(monkeypatch, tmp_path)
    assert opened == [1] and error is None


def test_sweep_takes_no_clock():
    import inspect

    params = inspect.signature(menubar.sweep_stale_dashboard_scripts).parameters
    assert "max_age_s" not in params and "now" not in params
    assert not hasattr(menubar, "DASHBOARD_SCRIPT_MAX_AGE")


def test_launcher_requires_a_script_dir_without_open_fn():
    with pytest.raises(ValueError):
        menubar.DashboardLauncher(_Logger())


# --- dashboard launcher (off the main thread) -----------------------------------

class _Logger:
    def __init__(self):
        self.warnings = []

    def warning(self, fmt, *args):
        self.warnings.append(fmt % args)


class _ManualThreads:
    """Captures thread targets so a test decides when the worker runs."""

    def __init__(self):
        self.targets = []

    def __call__(self, target):
        self.targets.append(target)

    def run_all(self):
        targets, self.targets = self.targets, []
        for t in targets:
            t()


def test_launcher_runs_open_off_the_calling_thread():
    opened_on = []
    main = threading.get_ident()

    def open_fn():
        opened_on.append(threading.get_ident())
        return True, "ok"

    launcher = menubar.DashboardLauncher(_Logger(), open_fn=open_fn)
    assert launcher.start() is True
    deadline = __import__("time").monotonic() + 5
    while launcher.in_flight and __import__("time").monotonic() < deadline:
        __import__("time").sleep(0.01)
    assert opened_on and opened_on[0] != main
    assert launcher.take_error() is None


def test_launcher_start_returns_before_open_finishes():
    threads = _ManualThreads()
    calls = []
    launcher = menubar.DashboardLauncher(
        _Logger(), open_fn=lambda: calls.append(1) or (True, "ok"), start_thread=threads
    )
    launcher.start()
    assert calls == []  # the click returned before Terminal was asked
    threads.run_all()
    assert calls == [1]


def test_launcher_ignores_second_click_while_in_flight():
    threads = _ManualThreads()
    calls = []
    launcher = menubar.DashboardLauncher(
        _Logger(), open_fn=lambda: calls.append(1) or (True, "ok"), start_thread=threads
    )
    assert launcher.start() is True
    assert launcher.start() is False
    assert len(threads.targets) == 1
    threads.run_all()
    assert calls == [1]
    assert launcher.in_flight is False
    assert launcher.start() is True  # a later click works again


def test_launcher_failure_is_logged_and_handed_to_main_thread_once():
    threads = _ManualThreads()
    logger = _Logger()
    launcher = menubar.DashboardLauncher(
        logger, open_fn=lambda: (False, "Not authorized (-1743)"), start_thread=threads
    )
    launcher.start()
    assert launcher.take_error() is None  # nothing until the worker finishes
    threads.run_all()
    assert logger.warnings == ["Could not open dashboard: Not authorized (-1743)"]
    assert launcher.take_error() == "Not authorized (-1743)"
    assert launcher.take_error() is None


def test_launcher_success_hands_back_nothing():
    threads = _ManualThreads()
    logger = _Logger()
    launcher = menubar.DashboardLauncher(logger, open_fn=lambda: (True, "ok"), start_thread=threads)
    launcher.start()
    threads.run_all()
    assert launcher.take_error() is None
    assert logger.warnings == []


def test_launcher_worker_exception_is_reported_and_clears_flag():
    threads = _ManualThreads()

    def boom():
        raise RuntimeError("boom")

    launcher = menubar.DashboardLauncher(_Logger(), open_fn=boom, start_thread=threads)
    launcher.start()
    threads.run_all()  # must not raise
    assert launcher.in_flight is False
    assert launcher.take_error() == "boom"


def test_launcher_thread_start_failure_does_not_raise_or_wedge():
    def no_threads(_target):
        raise RuntimeError("can't start new thread")

    launcher = menubar.DashboardLauncher(
        _Logger(), open_fn=lambda: (True, "ok"), start_thread=no_threads
    )
    launcher.start()
    assert launcher.in_flight is False
    assert launcher.take_error() == "can't start new thread"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX shells")
def test_a_slow_clean_dashboard_does_not_count_as_waiting(tmp_path: Path):
    # A busy machine (a parallel test run) can make a clean exit slow: that
    # must still read as "did not wait", so no clock may decide it.
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    exe = _fake_exe(tmp_path / "bin dir")
    exe.write_text("#!/bin/sh\nsleep 1\nexit 0\n")
    waited, err = _pauses(tmp_path, _create_script(tmp_path, exe, cwd))
    assert not waited
    assert err == ""


def test_adapt_snapshot_records_each_accounts_organization():
    a = _FakeAcct("1", "a@x.com", True, _FakeEntry())
    a.org_uuid = "org-a"
    b = _FakeAcct("2", "a@x.com", False, _FakeEntry())
    b.org_uuid = ""
    snap = menubar._adapt_snapshot(_FakeSnap([a, b]))
    assert snap["orgs"] == {"1": "org-a", "2": ""}


# --- snapshot worker logging --------------------------------------------------------
#
# The worker keeps the last good snapshot when a read fails, so a failing or
# stalled read freezes the menu while its ages keep ticking. These lines must
# reach the log (WARNING) and must not flood it, and nothing from an
# exception's message may reach any level: it can hold a secret. A failure is
# its class, where it was raised, and an integer status when it has one.


class _Clock:
    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now


def _snapshot_log(clock=None):
    logger = logging.getLogger("claude-swap.test-snapshot-log")
    return menubar.SnapshotLog(logger, clock=clock or _Clock()), logger.name


def _lines(caplog, name, level=logging.WARNING):
    return [r.getMessage() for r in caplog.records if r.name == name and r.levelno == level]


def _raised(exc):
    try:
        raise exc
    except BaseException as caught:  # a real traceback, as the worker sees it
        return caught


def _secret():
    # Built at run time, so no constant in this file can stand in for it.
    import uuid

    return "s3cr3t-" + uuid.uuid4().hex


@pytest.mark.parametrize(
    "shape",
    [
        lambda s: '{"password": "prefix\\"' + s + '", "note": "mArKeR"}',
        lambda s: 'mArKeR Authorization: Bearer "' + s + '"',
        lambda s: "mArKeR refresh failed\nrefreshToken=" + s + "\n",
        lambda s: "mArKeR short words then " + s,
        lambda s: "mArKeR " + "x" * (10 * 1024 * 1024) + " " + s,
    ],
    ids=["json-escaped-quote", "quoted-bearer", "newlines", "plain", "10-MB"],
)
def test_no_message_text_reaches_any_level(caplog, shape):
    secret = _secret()
    log, name = _snapshot_log()
    caplog.set_level(logging.DEBUG, logger=name)
    log.failed(_raised(RuntimeError(shape(secret))))
    assert len(_lines(caplog, name)) == 1
    assert secret not in caplog.text
    assert "mArKeR" not in caplog.text
    assert "xxxxxxxx" not in caplog.text


def test_a_failure_line_is_its_class_and_raise_site(caplog):
    log, name = _snapshot_log()
    caplog.set_level(logging.DEBUG, logger=name)
    log.failed(_raised(RuntimeError("mArKeR")))
    (line,) = _lines(caplog, name)
    assert line.startswith("menubar snapshot failed: RuntimeError at test_menubar.py:")
    assert line.endswith(" in _raised")
    (frames,) = _lines(caplog, name, logging.DEBUG)
    assert "test_menubar.py:" in frames and " in _raised" in frames
    assert "mArKeR" not in caplog.text


def test_the_raise_site_is_the_innermost_frame_inside_the_package(caplog):
    # Our frame (a module compiled at run time under the package's own
    # directory) calls json.loads, which raises from inside the standard
    # library: the site names our frame, where the failing call was made.
    pkg = os.path.dirname(menubar.__file__)
    filename = os.path.join(pkg, "runtime_frame_probe.py")
    source = "import json\ndef call():\n    return json.loads('{not json')\n"
    namespace: dict = {}
    exec(compile(source, filename, "exec"), namespace)
    try:
        namespace["call"]()
    except ValueError as exc:
        caught = exc
    log, name = _snapshot_log()
    caplog.set_level(logging.DEBUG, logger=name)
    log.failed(caught)
    (line,) = _lines(caplog, name)
    assert line == "menubar snapshot failed: JSONDecodeError at runtime_frame_probe.py:3 in call"
    (frames,) = _lines(caplog, name, logging.DEBUG)
    assert "decoder.py:" in frames  # the frames still go all the way down


@pytest.mark.parametrize(
    "attr, value, shown",
    [
        ("status", 429, "status 429"),
        ("status_code", 503, "status_code 503"),
        ("code", 7, "code 7"),
        ("errno", 61, "errno 61"),
        ("returncode", 44, "returncode 44"),
    ],
)
def test_an_integer_status_is_carried(caplog, attr, value, shown):
    exc = RuntimeError("mArKeR")
    setattr(exc, attr, value)
    log, name = _snapshot_log()
    caplog.set_level(logging.WARNING, logger=name)
    log.failed(_raised(exc))
    (line,) = _lines(caplog, name)
    assert line.startswith(f"menubar snapshot failed: RuntimeError ({shown}) at ")


def test_real_status_bearing_errors_carry_only_their_number(caplog):
    import urllib.error

    http = urllib.error.HTTPError("https://x.invalid/u", 429, "Too Many mArKeR", {}, None)
    log, name = _snapshot_log(_Clock())
    caplog.set_level(logging.WARNING, logger=name)
    log.failed(_raised(http))
    (line,) = _lines(caplog, name)
    assert line.startswith("menubar snapshot failed: HTTPError (status 429) at ")
    log2, name2 = _snapshot_log()
    # OSError(61, ...) constructs the errno's own subclass.
    log2.failed(_raised(OSError(61, "Connection refused mArKeR")))
    line2 = _lines(caplog, name2)[-1]  # both logs write under one logger name
    assert line2.startswith("menubar snapshot failed: ConnectionRefusedError (errno 61) at ")
    assert "mArKeR" not in caplog.text


@pytest.mark.parametrize("value", ["429", 4.29, True, None])
def test_a_status_that_is_not_an_integer_is_left_out(caplog, value):
    exc = RuntimeError("x")
    exc.status = value
    log, name = _snapshot_log()
    caplog.set_level(logging.WARNING, logger=name)
    log.failed(_raised(exc))
    (line,) = _lines(caplog, name)
    assert line.startswith("menubar snapshot failed: RuntimeError at ")


def test_a_hostile_or_unraised_exception_never_breaks_the_worker(caplog):
    class Hostile(Exception):
        def __str__(self):
            raise RuntimeError("no text")

        @property
        def status(self):
            raise RuntimeError("no status")

    log, name = _snapshot_log()
    caplog.set_level(logging.DEBUG, logger=name)
    log.failed(_raised(Hostile()))
    clock = _Clock()
    log2, name2 = _snapshot_log(clock)
    log2.failed(ValueError("never raised, so no traceback"))
    lines = _lines(caplog, name)
    # A class defined outside the trusted modules is named "Exception".
    assert lines and lines[0].startswith("menubar snapshot failed: Exception at ")
    assert "menubar snapshot failed: ValueError" in _lines(caplog, name2)


def test_one_window_for_every_class(caplog):
    clock = _Clock()
    log, name = _snapshot_log(clock)
    caplog.set_level(logging.WARNING, logger=name)
    log.failed(_raised(RuntimeError("a")))
    for i, cls in enumerate([TimeoutError, OSError, RuntimeError, KeyError]):
        clock.now += 100  # all inside the first line's ten minutes
        log.failed(_raised(cls("b")))
    log.succeeded()  # a good read inside the window ends nothing
    clock.now += 50
    log.failed(_raised(ValueError("c")))
    assert len(_lines(caplog, name)) == 1


def test_the_rolled_window_reports_its_count_and_class_names(caplog):
    clock = _Clock()
    log, name = _snapshot_log(clock)
    caplog.set_level(logging.WARNING, logger=name)
    log.failed(_raised(RuntimeError("a")))
    for cls in (TimeoutError, OSError, TimeoutError, KeyError):
        clock.now += 60
        log.failed(_raised(cls("b")))
    clock.now += 600
    log.succeeded()
    lines = _lines(caplog, name)
    assert lines[1] == (
        "menubar snapshot failed 4 more times in the 10 min after the last line: "
        "KeyError, OSError, TimeoutError"
    )
    log.succeeded()  # reported once
    assert len(_lines(caplog, name)) == 2


def test_a_failure_after_the_window_reports_it_and_opens_the_next(caplog):
    clock = _Clock()
    log, name = _snapshot_log(clock)
    caplog.set_level(logging.WARNING, logger=name)
    log.failed(_raised(RuntimeError("a")))
    clock.now += 30
    log.failed(_raised(RuntimeError("a")))
    clock.now += 600
    log.failed(_raised(TimeoutError("b")))
    lines = _lines(caplog, name)
    assert len(lines) == 2  # the rolled count rides on the new failure's own line
    assert lines[1].startswith("menubar snapshot failed: TimeoutError at ")
    assert lines[1].endswith("; 1 more in the previous 10 min: RuntimeError")


def test_each_window_counts_from_zero(caplog):
    clock = _Clock()
    log, name = _snapshot_log(clock)
    caplog.set_level(logging.WARNING, logger=name)
    for repeats in (3, 2):
        log.failed(_raised(RuntimeError("a")))
        for _ in range(repeats):
            clock.now += 10
            log.failed(_raised(RuntimeError("a")))
        clock.now += 600
        log.succeeded()
    summaries = [m for m in _lines(caplog, name) if "more time" in m]
    assert [m.split(" more ")[0].rsplit(" ", 1)[1] for m in summaries] == ["3", "2"]


def test_the_rolled_line_names_at_most_eight_classes(caplog):
    clock = _Clock()
    log, name = _snapshot_log(clock)
    caplog.set_level(logging.WARNING, logger=name)
    log.failed(_raised(RuntimeError("first")))
    classes = [  # twenty distinct trusted classes, each failing twice
        ValueError, TypeError, KeyError, IndexError, LookupError, ArithmeticError,
        ZeroDivisionError, OverflowError, AttributeError, NameError, ImportError,
        ModuleNotFoundError, EOFError, NotImplementedError, RecursionError,
        UnicodeError, BufferError, AssertionError, PermissionError, FileNotFoundError,
    ]
    for cls in classes:
        log.failed(_raised(cls("x")))
        log.failed(_raised(cls("x")))
    assert len(log._held_names) <= 8
    clock.now += 600
    log.succeeded()
    summary = _lines(caplog, name)[1]
    assert summary.startswith("menubar snapshot failed 40 more times in the 10 min after the last line: ")
    first_eight = sorted(cls.__name__ for cls in classes[:8])
    assert summary.split(": ", 1)[1] == ", ".join(first_eight) + " (+24 others)"


def test_a_class_built_at_run_time_never_names_itself(caplog):
    # type(name, ...) can carry any text as a class name, a secret included,
    # and it can claim any module. Only a class its trusted module really
    # holds under that name is named; anything else is "Exception".
    first, second, forged = _secret(), _secret(), _secret()
    clock = _Clock()
    log, name = _snapshot_log(clock)
    caplog.set_level(logging.DEBUG, logger=name)
    log.failed(_raised(type(first, (Exception,), {})("x")))
    clock.now += 10
    log.failed(_raised(type(second, (Exception,), {})("x")))
    log.failed(_raised(type(forged, (Exception,), {"__module__": "builtins"})("x")))
    clock.now += 600
    log.succeeded()  # the rolled window's own line
    log.failed(_raised(type(second, (Exception,), {})("x")))
    clock.now += 10
    log.failed(_raised(type(first, (Exception,), {})("x")))  # held in the new window
    clock.now += 600
    log.failed(_raised(RuntimeError("x")))  # its line carries the rolled count
    lines = _lines(caplog, name)
    assert len(lines) == 4
    assert lines[0].startswith("menubar snapshot failed: Exception at ")
    assert lines[1] == (
        "menubar snapshot failed 2 more times in the 10 min after the last line: Exception"
    )
    assert lines[2].startswith("menubar snapshot failed: Exception at ")
    assert lines[3].startswith("menubar snapshot failed: RuntimeError at ")
    assert lines[3].endswith("; 1 more in the previous 10 min: Exception")
    for secret in (first, second, forged):
        assert secret not in caplog.text
        assert secret[-12:] not in caplog.text


def test_a_status_from_an_int_subclass_is_left_out(caplog):
    class Sneaky(int):
        def __format__(self, spec):
            return "mArKeR"

        def __str__(self):
            return "mArKeR"

        __repr__ = __str__

    exc = RuntimeError("x")
    exc.status = Sneaky(429)
    log, name = _snapshot_log()
    caplog.set_level(logging.DEBUG, logger=name)
    log.failed(_raised(exc))
    (line,) = _lines(caplog, name)
    assert line.startswith("menubar snapshot failed: RuntimeError at ")
    assert "mArKeR" not in caplog.text


def test_a_status_whose_class_raises_cannot_break_the_record(caplog):
    class Hostile:
        @property
        def __class__(self):
            raise RuntimeError("no class")

    exc = RuntimeError("x")
    exc.status = Hostile()
    log, name = _snapshot_log()
    caplog.set_level(logging.WARNING, logger=name)
    log.failed(_raised(exc))  # must not raise
    (line,) = _lines(caplog, name)
    assert line.startswith("menubar snapshot failed: RuntimeError at ")


def test_a_class_that_cannot_say_its_module_falls_back(caplog):
    class Meta(type):
        @property
        def __module__(cls):
            raise RuntimeError("no module")

    class Shifty(Exception, metaclass=Meta):
        pass

    log, name = _snapshot_log()
    caplog.set_level(logging.WARNING, logger=name)
    log.failed(_raised(Shifty("x")))  # must not raise
    assert _lines(caplog, name) == ["menubar snapshot failed: Exception"]


def test_a_failure_inside_the_helper_still_writes_its_line_first(caplog, monkeypatch):
    # Whatever breaks while the record is built, the failure still gets a
    # line, and only then is the window opened: a broken record can never
    # leave a window open that silences the failures after it.
    def broken(_exc):
        raise RuntimeError("helper broke")

    monkeypatch.setattr(menubar, "_frames", broken)
    clock = _Clock()
    log, name = _snapshot_log(clock)
    caplog.set_level(logging.DEBUG, logger=name)
    log.failed(_raised(RuntimeError("x")))  # must not raise
    assert _lines(caplog, name) == ["menubar snapshot failed: Exception"]
    clock.now += 10
    log.failed(_raised(RuntimeError("x")))  # inside the window that line opened
    clock.now += 600
    log.failed(_raised(RuntimeError("x")))
    assert _lines(caplog, name) == [
        "menubar snapshot failed: Exception",
        "menubar snapshot failed: Exception; 1 more in the previous 10 min: Exception",
    ]


def test_a_slow_read_logs_its_duration(caplog):
    log, name = _snapshot_log()
    caplog.set_level(logging.WARNING, logger=name)
    log.took(60.0)  # at the limit: not slow
    assert _lines(caplog, name) == []
    log.took(452.4)
    assert _lines(caplog, name) == ["menubar snapshot took 452s (over 60s)"]


# --- the Codex settings and title segment -------------------------------------------

from claude_swap import codex_usage as _cu  # noqa: E402

_CODEX_NOW = 2_000_000_000.0


def _codex(pct=56.0, resets_in=2 * 86400, credits=2, credits_age=60.0, windows=None, reading=True):
    if not reading:
        return _cu.CodexState(True, None, credits, _CODEX_NOW - credits_age, None)
    windows = windows or (_cu.CodexWindow("7d", pct, 10080, _CODEX_NOW + resets_in),)
    found = _cu.CodexReading(tuple(windows), "pro", _CODEX_NOW - 9, "rollout")
    credits_at = None if credits is None else _CODEX_NOW - credits_age
    return _cu.CodexState(True, found, credits, credits_at, None)


def test_settings_round_trip_codex_keys(tmp_path: Path):
    defaults = menubar.MenuBarSettings()
    assert (defaults.show_codex, defaults.title_codex, defaults.codex_alias) == (True, True, "")
    path = tmp_path / "menubar_settings.json"
    original = menubar.MenuBarSettings(show_codex=False, title_codex=False, codex_alias="Review")
    original.save(path)
    assert menubar.MenuBarSettings.load(path) == original
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert (saved["show_codex"], saved["title_codex"], saved["codex_alias"]) == (False, False, "Review")


def test_settings_ignore_wrong_type_codex_alias(tmp_path: Path):
    path = tmp_path / "menubar_settings.json"
    path.write_text(json.dumps({"codex_alias": 7, "show_codex": "no", "title_codex": 0}), encoding="utf-8")
    s = menubar.MenuBarSettings.load(path)
    assert (s.codex_alias, s.show_codex, s.title_codex) == ("", True, True)


def test_format_title_codex_segment():
    s = menubar.MenuBarSettings(show_account_name=True, title_pct="5h")
    title = menubar.format_title("loc@papaya.asia", _USAGE, s, now=_CODEX_NOW, codex=_codex())
    assert title == "⇄ loc · 42% · Codex 56%"


def test_format_title_codex_uses_the_longest_window():
    s = menubar.MenuBarSettings(show_account_name=False, title_pct="off")
    both = (_cu.CodexWindow("5h", 12.0, 300, _CODEX_NOW + 3600), _cu.CodexWindow("7d", 56.0, 10080, _CODEX_NOW + 86400))
    assert menubar.format_title("a@x.com", None, s, now=_CODEX_NOW, codex=_codex(windows=both)) == "⇄ Codex 56%"


def test_format_title_codex_passed_reset_omits_the_segment():
    # No current pct: no Codex segment at all, not a bare name.
    s = menubar.MenuBarSettings(show_account_name=True, title_pct="off")
    title = menubar.format_title("loc@x.com", None, s, now=_CODEX_NOW, codex=_codex(pct=97.0, resets_in=-30))
    assert title == "⇄ loc"
    bare = menubar.MenuBarSettings(show_account_name=False, title_pct="off")
    assert menubar.format_title("a@x.com", None, bare, now=_CODEX_NOW, codex=_codex(resets_in=-30)) == "⇄"


@pytest.mark.parametrize("credits, credits_age", [(2, 60.0), (0, 60.0), (None, 60.0), (3, 25 * 3600)])
def test_format_title_never_shows_the_codex_usage_resets(credits, credits_age):
    # The owner reads the count in the dropdown only: any count, known,
    # zero, unknown or stale, leaves the title as the pct alone.
    s = menubar.MenuBarSettings(show_account_name=False, title_pct="off")
    state = _codex(credits=credits, credits_age=credits_age)
    assert menubar.format_title("a@x.com", None, s, now=_CODEX_NOW, codex=state) == "⇄ Codex 56%"


@pytest.mark.parametrize("credits, credits_age, text", [
    (2, 60.0, "UR 2"),
    (0, 60.0, "UR 0"),  # the dropdown hides a spent count; the log keeps it
    (None, 60.0, "UR ?"),
    (0, 25 * 3600, "UR ?"),
])
def test_codex_usage_log_keeps_every_count(credits, credits_age, text):
    state = _codex(credits=credits, credits_age=credits_age)
    line = menubar.format_codex_usage_log(state, _CODEX_NOW)
    assert line.startswith("usage codex: 7d 56% (resets ") and line.endswith(f" · {text}")
    assert menubar.codex_usage_log_key(state, _CODEX_NOW) == ((("7d", 56.0),), text)


def test_codex_usage_log_key_tells_a_spent_count_from_an_unknown_one():
    spent = menubar.codex_usage_log_key(_codex(credits=0), _CODEX_NOW)
    unknown = menubar.codex_usage_log_key(_codex(credits=None), _CODEX_NOW)
    one = menubar.codex_usage_log_key(_codex(credits=1), _CODEX_NOW)
    assert len({spent, unknown, one}) == 3  # each change logs a line


def test_format_title_codex_off():
    title_off = menubar.MenuBarSettings(show_account_name=True, title_pct="off", title_codex=False)
    assert menubar.format_title("loc@x.com", None, title_off, now=_CODEX_NOW, codex=_codex()) == "⇄ loc"
    block_off = menubar.MenuBarSettings(show_account_name=True, title_pct="off", show_codex=False)
    assert menubar.format_title("loc@x.com", None, block_off, now=_CODEX_NOW, codex=_codex()) == "⇄ loc"
    on = menubar.MenuBarSettings(show_account_name=True, title_pct="off")
    # No reading: nothing at all, not even the badge.
    assert menubar.format_title("loc@x.com", None, on, now=_CODEX_NOW, codex=_codex(reading=False)) == "⇄ loc"
    assert menubar.format_title("loc@x.com", None, on, now=_CODEX_NOW, codex=None) == "⇄ loc"


def test_format_title_codex_alias():
    s = menubar.MenuBarSettings(show_account_name=False, title_pct="off")
    title = menubar.format_title("a@x.com", None, s, now=_CODEX_NOW, codex=_codex(credits=0), codex_alias="Review")
    assert title == "⇄ Review 56%"


def test_format_title_codex_without_an_active_account():
    s = menubar.MenuBarSettings()
    assert menubar.format_title(None, None, s, now=_CODEX_NOW, codex=_codex()) == "⇄ Codex 56%"


@pytest.mark.parametrize("stored, shown", [
    ("Review", "Review"), ("", None), ("  ", None), ("my review", None), ("123", None), ("-x", None),
    ("x@y", None), ("Pro\u200b", "Pro"),
])
def test_codex_alias_name_uses_the_account_alias_rule(stored, shown):
    # A name hand-edited into the settings file is shown only when the
    # rename prompt would have accepted it.
    assert menubar.codex_alias_name(stored) == shown


def test_long_codex_alias_bounds_status_title_width():
    s = menubar.MenuBarSettings(show_account_name=False, title_pct="off")
    alias = "review-" + "x" * 60 + "-end"
    assert menubar.codex_alias_name(alias) == alias  # the alias rule is left alone
    title = menubar.format_title("a@x.com", None, s, now=_CODEX_NOW, codex=_codex(), codex_alias=alias)
    assert title == "⇄ review…x-end 56%"  # 12 characters, cut in the middle
    passed = menubar.format_title(
        "a@x.com", None, s, now=_CODEX_NOW, codex=_codex(resets_in=-30), codex_alias=alias
    )
    assert passed == "⇄"  # no current pct: no segment, so no name either
    whole = menubar.format_title("a@x.com", None, s, now=_CODEX_NOW, codex=_codex(), codex_alias="a-twelve-chr")
    assert whole == "⇄ a-twelve-chr 56%"


def test_the_menu_bar_reads_rollouts_from_the_codex_home(tmp_path, monkeypatch):
    log = logging.getLogger("claude-swap")
    assert menubar.default_codex_monitor(tmp_path, log).sessions_dir == Path.home() / ".codex" / "sessions"
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "alt-home"))
    assert menubar.default_codex_monitor(tmp_path, log).sessions_dir == tmp_path / "alt-home" / "sessions"


# --- limit resets on the Claude accounts (the UR tag) --------------------------------

import time as _time  # noqa: E402

_UR_NOW = _dt.datetime(2026, 11, 2, 12, 0, tzinfo=_dt.timezone.utc).timestamp()


def _one_grant(left=1, ends_at="2026-11-20T12:00:00+00:00", usable=True, paused=False, id="g"):
    return {
        "id": id, "label": "Free limit reset", "left": left,
        "total": left if type(left) is int and left > 1 else 1, "ends_at": ends_at,
        "usable_now": usable, "paused": paused,
    }


def _resets(*grants, **kwargs):
    """The stored entry: each live grant, as the usage store keeps it."""
    return {"grants": list(grants) or [_one_grant(**kwargs)], "eligible": True, "fetched_at": _UR_NOW - 60}


def _with_resets(usage=None, **kwargs):
    return {**(_USAGE if usage is None else usage), "limit_resets": _resets(**kwargs)}


@pytest.fixture
def new_york(monkeypatch):
    """Local time in New York, as on the owner's Mac, for the spoken date."""
    monkeypatch.setenv("TZ", "America/New_York")
    _time.tzset()
    yield
    monkeypatch.undo()
    _time.tzset()


@pytest.mark.parametrize("kwargs, badge", [
    ({}, ("UR 1", "ok")),
    ({"left": 2}, ("UR 2", "ok")),
    ({"usable": False}, ("UR 1", "unknown")),  # held but not usable right now: dimmed
    ({"paused": True, "usable": False}, ("UR 1", "unknown")),
    # Nothing left is no pill at all (the owner, Nov 3), whatever the grant
    # says of itself: a spent grant is spent.
    ({"left": 0, "usable": False}, None),
    ({"left": 0}, None),
    ({"left": 0, "paused": True}, None),
    ({"left": 0, "paused": True, "usable": False}, None),
])
def test_limit_resets_badge_states(kwargs, badge):
    assert menubar.limit_resets_badge(_with_resets(**kwargs), _UR_NOW) == badge


@pytest.mark.parametrize("kwargs, text", [
    ({}, "UR 1"),
    ({"left": 2}, "UR 2"),
    ({"usable": False}, "UR 1"),
    ({"left": 0, "usable": False}, "UR 0"),  # the log keeps a spent reset
    ({"left": 0, "paused": True}, "UR 0"),
])
def test_limit_resets_text_keeps_every_count_for_the_log(kwargs, text):
    assert menubar.limit_resets_text(_with_resets(**kwargs), _UR_NOW) == text


@pytest.mark.parametrize("usage", [
    None, "token expired", {}, _USAGE,
    {**_USAGE, "limit_resets": None},
    {**_USAGE, "limit_resets": "UR 1"},
    {**_USAGE, "limit_resets": {"fetched_at": 1.0}},  # no grant reading at all
])
def test_no_badge_without_a_grant(usage):
    assert menubar.limit_resets_badge(usage, _UR_NOW) is None
    assert menubar.limit_resets_spoken(usage, _UR_NOW) is None
    assert menubar.limit_resets_text(usage, _UR_NOW) is None


@pytest.mark.parametrize("left", [True, -1, 1.5, "1", None, 10**7])
def test_no_badge_for_an_unreadable_count(left):
    assert menubar.limit_resets_badge(_with_resets(left=left), _UR_NOW) is None


@pytest.mark.parametrize("ends_at", [None, "", "soon", 5, "2026-11-02T11:59:59+00:00", "2026-11-02T12:00:00+00:00"])
def test_no_badge_once_the_grant_has_ended_or_cannot_be_dated(ends_at):
    assert menubar.limit_resets_badge(_with_resets(ends_at=ends_at), _UR_NOW) is None


def test_remaining_grant_stays_visible_after_earliest_grant_expires(new_york):
    # A spent grant that ends in 30 seconds and a live one until December:
    # once the first has ended the pill, the text and VoiceOver still show
    # the second, without waiting for the next read.
    spent = _one_grant(left=0, usable=False, ends_at="2026-11-02T12:00:30+00:00", id="spent")
    live = _one_grant(left=1, ends_at="2026-12-01T04:59:00+00:00", id="live")
    usage = {**_USAGE, "limit_resets": _resets(spent, live)}
    for at in (_UR_NOW, _UR_NOW + 31):
        assert menubar.limit_resets_badge(usage, at) == ("UR 1", "ok")
        assert menubar.limit_resets_spoken(usage, at) == "1 limit reset, use by Nov 30"
    assert menubar.format_account_label(2, "a@x.com", usage, _UR_NOW + 31).endswith("$ 30% · UR 1")
    december = _dt.datetime(2026, 12, 1, 4, 59, tzinfo=_dt.timezone.utc).timestamp()
    assert menubar.limit_resets_badge(usage, december) is None  # both ended


def test_spent_usable_grant_does_not_make_remaining_resets_usable(new_york):
    spent = _one_grant(left=0, usable=True, id="a")
    waiting = _one_grant(left=1, usable=False, id="b", ends_at="2026-11-25T12:00:00+00:00")
    usage = {**_USAGE, "limit_resets": _resets(spent, waiting)}
    assert menubar.limit_resets_badge(usage, _UR_NOW) == ("UR 1", "unknown")  # dimmed
    assert menubar.limit_resets_spoken(usage, _UR_NOW) == "1 limit reset, not usable now, use by Nov 25"


def test_badge_while_the_grant_is_still_live():
    usage = _with_resets(ends_at="2026-11-02T12:00:01+00:00")
    assert menubar.limit_resets_badge(usage, _UR_NOW) == ("UR 1", "ok")


def test_limit_resets_spoken_says_the_local_use_by_date(new_york):
    # The grant the server reported on 2026-11-02: ends 04:59 UTC on Dec 1,
    # which is 11:59 PM on Nov 30 in New York (claude.ai: "Expires Nov 30").
    usage = _with_resets(ends_at="2026-12-01T04:59:00+00:00")
    assert menubar.limit_resets_spoken(usage, _UR_NOW) == "1 limit reset, use by Nov 30"


@pytest.mark.parametrize("kwargs, spoken", [
    ({"left": 2}, "2 limit resets, use by Nov 20"),
    ({"usable": False}, "1 limit reset, not usable now, use by Nov 20"),
    ({"paused": True, "usable": False}, "1 limit reset, paused, use by Nov 20"),
    # No pill, nothing spoken: nothing left is not a sentence.
    ({"left": 0, "usable": False}, None),
    ({"left": 0}, None),
    ({"left": 0, "paused": True}, None),
])
def test_limit_resets_spoken_states(new_york, kwargs, spoken):
    assert menubar.limit_resets_spoken(_with_resets(**kwargs), _UR_NOW) == spoken


def test_format_account_label_appends_the_resets_after_the_windows():
    label = menubar.format_account_label(2, "loc@papaya.asia", _with_resets(), _UR_NOW)
    assert label == "2  loc@papaya.asia  5h 42% · 7d 18% · $ 30% · UR 1"
    # Nothing left: the row reads as an account without a grant.
    for kwargs in ({"left": 0}, {"left": 0, "usable": False}, {"left": 0, "paused": True}):
        zero = menubar.format_account_label(2, "loc@papaya.asia", _with_resets(**kwargs), _UR_NOW)
        assert zero == "2  loc@papaya.asia  5h 42% · 7d 18% · $ 30%"


def test_format_account_label_without_a_grant_is_unchanged():
    assert menubar.format_account_label(2, "loc@papaya.asia", _USAGE, _UR_NOW) == (
        "2  loc@papaya.asia  5h 42% · 7d 18% · $ 30%"
    )


@pytest.mark.parametrize("left", [1, 0])
def test_format_title_never_shows_an_accounts_limit_resets(left):
    # The owner reads the pill in the dropdown only; the title keeps the pcts.
    s = menubar.MenuBarSettings(show_account_name=True, title_pct="both", title_scoped=True)
    usage = {**_with_resets(left=left), "scoped": [{"name": "Fable", "pct": 55.0}]}
    title = menubar.format_title("loc@papaya.asia", usage, s, now=_UR_NOW)
    assert title == "⇄ loc · 42% · 18% · Fable 55%"
    assert "UR" not in title
    codex = _cu.CodexState(
        True,
        _cu.CodexReading((_cu.CodexWindow("7d", 56.0, 10080, _UR_NOW + 86400),), "pro", _UR_NOW - 9, "rollout"),
        2, _UR_NOW - 60, None,
    )
    five = menubar.MenuBarSettings(show_account_name=True, title_pct="5h")
    title = menubar.format_title("loc@papaya.asia", _with_resets(left=left), five, now=_UR_NOW, codex=codex)
    assert title == "⇄ loc · 42% · Codex 56%"


def test_there_is_no_limit_resets_title_setting():
    import dataclasses

    assert "title_resets" not in {f.name for f in dataclasses.fields(menubar.MenuBarSettings)}


def test_a_settings_file_written_with_title_resets_still_loads(tmp_path: Path):
    # A build that had the setting wrote it to the file; it is now an
    # unknown key, ignored, and the next save leaves it out.
    path = tmp_path / "menubar_settings.json"
    path.write_text(
        json.dumps({"title_resets": False, "title_codex": False, "title_pct": "5h"}), encoding="utf-8"
    )
    loaded = menubar.MenuBarSettings.load(path)
    assert (loaded.title_codex, loaded.title_pct) == (False, "5h")
    assert not hasattr(loaded, "title_resets")
    loaded.save(path)
    assert "title_resets" not in json.loads(path.read_text(encoding="utf-8"))


def test_format_usage_log_gains_the_resets():
    usage = {
        "five_hour": {"pct": 35.0, "clock": "06:59"},
        "seven_day": {"pct": 55.0, "clock": "Jul 3"},
        "limit_resets": _resets(),
    }
    assert menubar.format_usage_log("a@x.com", usage, _UR_NOW) == (
        "usage a@x.com: 5h 35% (resets 06:59) · 7d 55% (resets Jul 3) · UR 1"
    )
    assert menubar.format_usage_log("a@x.com", {"limit_resets": _resets()}, _UR_NOW) is None  # no window


@pytest.mark.parametrize("kwargs", [{"left": 0}, {"left": 0, "usable": False}, {"left": 0, "paused": True}])
def test_format_usage_log_keeps_a_spent_reset_the_menu_hides(kwargs):
    # The log is a diagnostic line, not UI: a used reset leaves "UR 0" in
    # it while the dropdown shows no pill for the same usage.
    usage = {"five_hour": {"pct": 35.0, "clock": "06:59"}, "limit_resets": _resets(**kwargs)}
    assert menubar.format_usage_log("a@x.com", usage, _UR_NOW) == "usage a@x.com: 5h 35% (resets 06:59) · UR 0"
    assert menubar.limit_resets_badge(usage, _UR_NOW) is None


def _attested_resets(account_uuid="uuid-1", org_uuid=""):
    return {**_resets(), "account_uuid": account_uuid, "org_uuid": org_uuid}


def _acct_with_ids(entry, account_uuid="uuid-1", org_uuid="", active=True, number="1"):
    acct = _FakeAcct(number, "a@x.com", active, entry)
    acct.account_uuid, acct.account_org_uuid = account_uuid, org_uuid
    return acct


def test_adapt_snapshot_hands_the_resets_entry_to_the_menu():
    lg = {"seven_day": {"pct": 20.0}, "limit_resets": _attested_resets()}
    accts = [_acct_with_ids(_FakeEntry(last_good=lg, fetched_at=123.0))]
    snap = menubar._adapt_snapshot(_FakeSnap(accts))
    (entry,) = snap["accounts"]
    assert entry[3]["limit_resets"] == lg["limit_resets"]  # the display usage the rows read
    assert entry[4]["limit_resets"] == lg["limit_resets"]  # the last good the log reads
    assert snap["active_usage"]["limit_resets"] == lg["limit_resets"]  # and the title


@pytest.mark.parametrize("stored, slot", [
    (("uuid-1", ""), ("uuid-9", "")),  # the slot now holds another account
    (("uuid-1", "org-1"), ("uuid-1", "org-2")),  # same account, another organization
    (("uuid-1", ""), ("", "")),  # the slot has no stored uuid (a token-only account)
    ((None, None), ("uuid-1", "")),  # an entry with no attested ids at all
])
def test_display_hides_entry_whose_attested_ids_differ_from_slot(stored, slot):
    entry = _resets()
    if stored != (None, None):
        entry = {**entry, "account_uuid": stored[0], "org_uuid": stored[1]}
    lg = {"seven_day": {"pct": 20.0}, "limit_resets": entry}
    snap = menubar._adapt_snapshot(_FakeSnap([_acct_with_ids(_FakeEntry(last_good=lg), *slot)]))
    (row,) = snap["accounts"]
    for usage in (row[3], row[4], snap["active_usage"]):
        assert "limit_resets" not in usage
        assert usage["seven_day"] == {"pct": 20.0}  # the windows stay
    assert "limit_resets" in lg  # the stored entry itself is left alone
    assert menubar.format_account_label("1", "a@x.com", row[3], _UR_NOW).endswith("7d 20%")


class TestFrameworkBuildWarning:
    """The menu bar draws nothing from a framework build on macOS 26.

    See #310. The interpreter *version* is not the variable — measured with the
    same bare rumps app, Homebrew 3.14.6 and 3.10.21 (both framework) draw
    nothing while uv-managed 3.14.7 and 3.13.15 (neither framework) draw.
    Nothing here can fix that; these tests are about the warning, not the icon.
    """

    def test_silent_on_a_build_that_draws(self):
        assert menubar.framework_build_warning("", "uv", "26.6.2") is None

    def test_silent_on_macos_where_framework_builds_still_draw(self):
        # The evidence is a framework build *on macOS 26*. Warning on 14 or 15
        # would nag every Homebrew user on every launch, and in the service log
        # on every restart, about something that works there.
        assert menubar.framework_build_warning("Python", "uv", "15.7") is None
        assert menubar.framework_build_warning("Python", "uv", "14.2") is None

    def test_warns_from_macos_26_onwards(self):
        assert menubar.framework_build_warning("Python", "uv", "26.0") is not None
        assert menubar.framework_build_warning("Python", "uv", "27.1") is not None

    def test_unreadable_macos_version_stays_quiet(self):
        assert menubar.framework_build_warning("Python", "uv", "") is None

    def test_the_wording_does_not_overclaim(self):
        # An observation on one machine that has also been seen to behave
        # otherwise; "observed" is what the evidence supports.
        msg = menubar.framework_build_warning("Python", "uv", "26.6.2")
        assert "observed" in msg

    def test_warns_on_a_framework_build(self):
        assert menubar.framework_build_warning("Python", "uv", "26.6.2") is not None

    def test_the_version_is_not_the_gate(self):
        # A framework build warns whatever the version; a non-framework one
        # never does. Keying on version_info was the original mistake here.
        assert menubar.framework_build_warning("Python", None, "26.6.2") is not None
        assert menubar.framework_build_warning("", None, "26.6.2") is None

    def test_uv_gets_a_uv_remedy(self):
        msg = menubar.framework_build_warning("Python", "uv", "26.6.2")
        assert "--managed-python" in msg

    def test_pipx_is_not_handed_a_uv_command(self):
        # `uv tool install --force` would overwrite pipx's own executable.
        msg = menubar.framework_build_warning("Python", "pipx", "26.6.2")
        assert "uv tool install" not in msg
        assert "pipx install" in msg

    def test_unknown_install_method_still_says_what_to_aim_for(self):
        msg = menubar.framework_build_warning("Python", None, "26.6.2")
        assert "non-framework" in msg

    def test_the_warning_names_the_silence(self):
        # The symptom is that everything looks healthy, so say so.
        msg = menubar.framework_build_warning("Python", "uv", "26.6.2")
        assert "logs nothing" in msg


# --- external rotation (a launchd daemon that rotates accounts itself) -----------

def test_settings_round_trip_external_rotation_keys(tmp_path: Path):
    defaults = menubar.MenuBarSettings()
    assert (defaults.external_rotation_label, defaults.external_rotation_summary) == ("", "")
    path = tmp_path / "menubar_settings.json"
    original = menubar.MenuBarSettings(
        external_rotation_label="com.example.rotate",
        external_rotation_summary="cswap auto, 99.9%",
    )
    original.save(path)
    assert menubar.MenuBarSettings.load(path) == original
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["external_rotation_label"] == "com.example.rotate"
    assert saved["external_rotation_summary"] == "cswap auto, 99.9%"


def test_settings_ignore_wrong_type_external_rotation_keys(tmp_path: Path):
    path = tmp_path / "menubar_settings.json"
    path.write_text(
        json.dumps({"external_rotation_label": ["com.example.rotate"], "external_rotation_summary": 7,
                    "auto_switch_enabled": True}),
        encoding="utf-8",
    )
    s = menubar.MenuBarSettings.load(path)
    assert (s.external_rotation_label, s.external_rotation_summary) == ("", "")
    assert s.auto_switch_enabled is True  # a valid neighbour is kept


@pytest.mark.parametrize("label", [
    "com.example.rotate",
    "com.example.cswap-rotate",
    "local_rotate.v2",
    "rotate",
])
def test_safe_launchd_label_accepts_plain_labels(label):
    assert menubar.safe_launchd_label(label) is True


@pytest.mark.parametrize("label", [
    "",
    "   ",
    ".hidden",
    "..",
    "com..example",
    "../escape",
    "com/example",
    "com\\example",
    "/abs/path",
    "com.example\n",
    "com.example\x00",
    "com.\x07example",
    "com.example\u200b",  # invisible format character
    "has space",
    "x" * 300,  # longer than a file name may be
    7,
    None,
])
def test_safe_launchd_label_refuses_unsafe_labels(label):
    assert menubar.safe_launchd_label(label) is False


def test_external_rotation_active_needs_a_label(tmp_path: Path):
    (tmp_path / ".plist").write_text("x", encoding="utf-8")
    assert menubar.external_rotation_active("", tmp_path) is False


def test_external_rotation_active_needs_the_plist(tmp_path: Path):
    assert menubar.external_rotation_active("com.example.rotate", tmp_path) is False
    assert menubar.external_rotation_active("com.example.rotate", tmp_path / "missing") is False


def test_external_rotation_active_with_the_plist(tmp_path: Path):
    (tmp_path / "com.example.rotate.plist").write_bytes(plistlib.dumps({"Label": "com.example.rotate"}))
    assert menubar.external_rotation_active("com.example.rotate", tmp_path) is True
    assert menubar.external_rotation_active("com.example.other", tmp_path) is False


def test_external_rotation_active_refuses_a_directory_named_like_the_plist(tmp_path: Path):
    (tmp_path / "com.example.rotate.plist").mkdir()
    assert menubar.external_rotation_active("com.example.rotate", tmp_path) is False


def test_external_rotation_active_refuses_an_unsafe_label_even_when_its_file_exists(tmp_path: Path):
    agents = tmp_path / "LaunchAgents"
    agents.mkdir()
    (tmp_path / "outside.plist").write_text("x", encoding="utf-8")
    (agents / ".hidden.plist").write_text("x", encoding="utf-8")
    assert menubar.external_rotation_active("../outside", agents) is False
    assert menubar.external_rotation_active(".hidden", agents) is False


def test_external_rotation_active_does_not_raise_on_an_unreadable_folder(tmp_path: Path, monkeypatch):
    def denied(_self, *a, **k):
        raise PermissionError("denied")

    monkeypatch.setattr(Path, "stat", denied)
    assert menubar.external_rotation_active("com.example.rotate", tmp_path) is False


def test_default_launch_agents_dir_is_under_the_home(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    assert menubar.default_launch_agents_dir() == tmp_path / "Library" / "LaunchAgents"


@pytest.mark.parametrize("summary, shown", [
    ("", "Rotation: external daemon"),
    ("   ", "Rotation: external daemon"),
    ("\u200b\n\t", "Rotation: external daemon"),
    ("cswap auto, 99.9%", "Rotation: cswap auto, 99.9%"),
    ("  two\nlines\there  ", "Rotation: two lines here"),
    ("a \u200b b", "Rotation: a b"),
    ("\uff46\uff55\uff4c\uff4c", "Rotation: full"),  # compatibility forms folded, as an alias is
])
def test_external_rotation_text(summary, shown):
    assert menubar.external_rotation_text(summary) == shown


def test_external_rotation_text_is_bounded():
    text = menubar.external_rotation_text("word " * 100)
    body = text.removeprefix("Rotation: ")
    assert len(body) == menubar.ROTATION_SUMMARY_MAX
    assert body.endswith("\u2026")
    assert "\n" not in text
    exact = "y" * menubar.ROTATION_SUMMARY_MAX
    assert menubar.external_rotation_text(exact) == f"Rotation: {exact}"  # at the limit: whole
