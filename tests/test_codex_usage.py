"""Codex usage for the menu bar: rollout files, the app-server, the launch budget.

Everything here runs against temporary files and a fake ``codex app-server``
script (``tests/fixtures/fake_codex_app_server.py``); no test reads the real
``~/.codex`` or starts the real CLI (conftest's ``_no_real_codex`` guard).
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from claude_swap import codex_usage as cu

RESETS_AT = 1793774344
NOW = datetime(2026, 11, 1, 12, 0, 0, tzinfo=timezone.utc).timestamp()


def _record(pct=56.0, minutes=10080, resets=RESETS_AT, plan="pro", stamp="2026-11-01T12:00:00.000Z",
            secondary=None, nested=True, **extra):
    limits = {
        "limit_id": "codex",
        "primary": {"used_percent": pct, "window_minutes": minutes, "resets_at": resets},
        "secondary": secondary,
        "plan_type": plan,
        **extra,
    }
    if nested:
        return {"timestamp": stamp, "type": "event_msg",
                "payload": {"type": "token_count", "info": None, "rate_limits": limits}}
    return {"timestamp": stamp, "rate_limits": limits}


def _line(obj) -> str:
    return json.dumps(obj)


# --- Task 1: the rollout reader -----------------------------------------------------

@pytest.mark.parametrize("minutes,label", [(300, "5h"), (10080, "7d"), (2880, "2d"), (120, "2h"), (45, "45m")])
def test_window_label_lengths(minutes, label):
    assert cu.window_label(minutes) == label


def test_parse_rollout_line_top_level_and_payload():
    nested = cu.parse_rollout_line(_line(_record(nested=True)))
    top = cu.parse_rollout_line(_line(_record(nested=False)))
    assert nested == top
    assert nested.source == "rollout"
    assert nested.plan_type == "pro"
    assert nested.fetched_at == NOW
    (window,) = nested.windows
    assert window == cu.CodexWindow(label="7d", pct=56.0, window_minutes=10080, resets_at=float(RESETS_AT))


@pytest.mark.parametrize("line", [
    "not json at all",
    _line({"timestamp": "2026-11-01T12:00:00Z", "payload": {"type": "token_count"}}),
    _line({"timestamp": "2026-11-01T12:00:00Z", "payload": {"rate_limits": None}}),
    _line(_record(pct="x")),
    _line(_record(minutes=0)),
    _line(_record(minutes="10080")),
], ids=["not-json", "no-rate-limits", "null-rate-limits", "pct-text", "zero-minutes", "minutes-text"])
def test_parse_rollout_line_rejects(line):
    assert cu.parse_rollout_line(line) is None


def test_parse_rollout_line_secondary_and_missing_reset():
    record = _record(secondary={"used_percent": 12.5, "window_minutes": 300, "resets_at": RESETS_AT - 3600})
    del record["payload"]["rate_limits"]["primary"]["resets_at"]
    reading = cu.parse_rollout_line(_line(record))
    # Shortest first, labelled by length whatever slot it came in.
    assert [(w.label, w.pct, w.resets_at) for w in reading.windows] == [
        ("5h", 12.5, float(RESETS_AT - 3600)),
        ("7d", 56.0, None),
    ]


def test_parse_rollout_line_clamps_pct_and_takes_an_epoch_timestamp():
    reading = cu.parse_rollout_line(_line(_record(pct=140.0, stamp=NOW)))
    assert reading.windows[0].pct == 100.0
    assert reading.fetched_at == NOW


def test_parse_rollout_line_needs_a_timestamp_with_an_offset():
    assert cu.parse_rollout_line(_line(_record(stamp="2026-11-01T12:00:00"))) is None
    assert cu.parse_rollout_line(_line(_record(stamp=None))) is None


def _rollout(folder: Path, name: str, *lines: str) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    path.write_text("".join(line + "\n" for line in lines), encoding="utf-8")
    return path


def test_read_rollout_tail_last_parseable_line_wins(tmp_path):
    path = _rollout(
        tmp_path, "rollout-a.jsonl",
        _line(_record(pct=10.0, stamp="2026-11-01T11:00:00Z")),
        _line(_record(pct=20.0, stamp="2026-11-01T11:30:00Z")),
        _line(_record(pct=30.0, stamp="2026-11-01T11:45:00Z")),
        '{"timestamp": "2026-11-01T11:50:00Z", "payload": {"type": "agent_message"}}',
        "{garbage that never closes",
    )
    reading = cu.read_rollout_tail(path)
    assert reading.windows[0].pct == 30.0


def test_read_rollout_tail_reads_only_the_tail(tmp_path):
    old = _line(_record(pct=10.0, stamp="2026-11-01T11:00:00Z"))
    filler = _line({"timestamp": "2026-11-01T11:10:00Z", "payload": {"type": "agent_message", "text": "x" * 500}})
    path = _rollout(tmp_path, "rollout-a.jsonl", old, *[filler] * 4)
    assert cu.read_rollout_tail(path, max_bytes=1200) is None  # the record is before the tail
    assert cu.read_rollout_tail(path).windows[0].pct == 10.0


def test_read_rollout_tail_refuses_symlink(tmp_path):
    real = _rollout(tmp_path / "real", "rollout-a.jsonl", _line(_record()))
    link = tmp_path / "rollout-link.jsonl"
    link.symlink_to(real)
    assert cu.read_rollout_tail(real) is not None
    assert cu.read_rollout_tail(link) is None


def test_read_rollout_tail_refuses_a_hard_link_and_other_names(tmp_path):
    real = _rollout(tmp_path, "rollout-a.jsonl", _line(_record()))
    os.link(real, tmp_path / "rollout-b.jsonl")
    assert cu.read_rollout_tail(real) is None  # a second name: nlink 2
    other = _rollout(tmp_path / "x", "auth.json", _line(_record()))
    assert cu.read_rollout_tail(other) is None  # only rollout files are ever opened


def test_read_rollout_tail_skips_records_dated_ahead(tmp_path):
    path = _rollout(
        tmp_path, "rollout-a.jsonl",
        _line(_record(pct=20.0, stamp="2026-11-01T11:59:00Z")),
        _line(_record(pct=90.0, stamp="2026-11-01T13:00:00Z")),  # an hour ahead of NOW
    )
    assert cu.read_rollout_tail(path, now=NOW).windows[0].pct == 20.0
    assert cu.read_rollout_tail(path).windows[0].pct == 90.0  # no clock given, no check


def _day_folder(sessions: Path, when: float, days_back: int = 0) -> Path:
    day = datetime.fromtimestamp(when).date() - timedelta(days=days_back)
    return sessions / f"{day.year:04d}" / f"{day.month:02d}" / f"{day.day:02d}"


def test_newest_rollout_reading_newest_timestamp_across_files(tmp_path):
    sessions = tmp_path / "sessions"
    today = _day_folder(sessions, NOW)
    newer_file = _rollout(today, "rollout-new.jsonl", _line(_record(pct=11.0, stamp="2026-11-01T10:00:00Z")))
    older_file = _rollout(today, "rollout-old.jsonl", _line(_record(pct=44.0, stamp="2026-11-01T11:00:00Z")))
    os.utime(older_file, (NOW - 600, NOW - 600))
    os.utime(newer_file, (NOW - 60, NOW - 60))  # touched last, but its record is older
    reading = cu.newest_rollout_reading(sessions, NOW)
    assert reading.windows[0].pct == 44.0


def test_newest_rollout_reading_looks_back_seven_days_and_reads_five_files(tmp_path):
    sessions = tmp_path / "sessions"
    week = _rollout(_day_folder(sessions, NOW, 6), "rollout-week.jsonl", _line(_record(pct=66.0, stamp="2026-10-26T09:00:00Z")))
    old = _rollout(_day_folder(sessions, NOW, 7), "rollout-too-old.jsonl", _line(_record(pct=77.0, stamp="2026-11-01T11:59:00Z")))
    os.utime(week, (NOW - 6 * 86400, NOW - 6 * 86400))
    os.utime(old, (NOW - 7 * 86400, NOW - 7 * 86400))
    assert cu.newest_rollout_reading(sessions, NOW).windows[0].pct == 66.0
    # Six files without limits touched after it: the five newest are read, it is not.
    today = _day_folder(sessions, NOW)
    for i in range(6):
        path = _rollout(today, f"rollout-{i}.jsonl", _line({"timestamp": "2026-11-01T11:00:00Z", "payload": {}}))
        os.utime(path, (NOW - i, NOW - i))
    assert cu.newest_rollout_reading(sessions, NOW) is None
    assert cu.newest_rollout_reading(sessions, NOW, limit=7).windows[0].pct == 66.0


def test_newest_rollout_reading_skips_a_symlinked_day_folder(tmp_path):
    sessions = tmp_path / "sessions"
    elsewhere = _rollout(tmp_path / "elsewhere", "rollout-a.jsonl", _line(_record()))
    today = _day_folder(sessions, NOW)
    today.parent.mkdir(parents=True)
    today.symlink_to(elsewhere.parent, target_is_directory=True)
    assert cu.newest_rollout_reading(sessions, NOW) is None


def test_newest_rollout_reading_missing_dir(tmp_path):
    assert cu.newest_rollout_reading(tmp_path / "no-such-sessions", NOW) is None
    (tmp_path / "empty").mkdir()
    assert cu.newest_rollout_reading(tmp_path / "empty", NOW) is None
