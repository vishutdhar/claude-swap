"""Codex usage for the menu bar: rollout files, the app-server and its budget.

The menu bar shows one Codex block beside the Claude accounts: the usage
windows of whatever account ``~/.codex`` holds, and how many banked usage
resets it has. This module is the pure half of that, stdlib only and free of
rumps and AppKit, so it is unit-tested on any platform.

Two sources, as the Usage Widget reads them:

- Rollout files. Every Codex session appends JSON lines to
  ``~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl``; its ``token_count``
  events carry the account's ``rate_limits``. Reading them costs nothing on
  the network and happens on every refresh tick.
- ``codex app-server``. Only its ``account/rateLimits/read`` reply carries the
  banked reset count (``rateLimitResetCredits.availableCount``). Each launch
  leaves a folder behind in ``~/.codex/.tmp``, so launches are budgeted.

Windows are named by their length, never by the ``primary``/``secondary``
slot they arrive in: the Pro plan reports its weekly window as primary with
no secondary.
"""

from __future__ import annotations

import json
import math
import os
import stat
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

# ---- the readings ----------------------------------------------------------------

ROLLOUT_TAIL_BYTES = 256 * 1024  # bytes read from the end of each rollout file
ROLLOUT_FILES_READ = 5  # the most recently modified rollout files read per tick
ROLLOUT_DAYS = 7  # day folders looked at, today and the six before it
# A record dated further ahead of the clock than this is not a reading:
# real records never are, and one would otherwise win "newest" for good.
FUTURE_ALLOWANCE_S = 120.0
_MAX_WINDOW_MINUTES = 10_000_000
_MAX_EPOCH = 1e11  # past this an epoch is milliseconds or garbage


@dataclass(frozen=True)
class CodexWindow:
    """One usage window: its length label, how much is used, when it resets."""

    label: str  # "7d", "5h", ... (``window_label``)
    pct: float  # 0..100
    window_minutes: int
    resets_at: float | None  # epoch seconds; None when not reported


@dataclass(frozen=True)
class CodexReading:
    """Codex usage at one moment, from a rollout record or the app-server."""

    windows: tuple[CodexWindow, ...]  # shortest first
    plan_type: str | None
    fetched_at: float  # the record's timestamp, or when the app-server answered
    source: str  # "rollout" or "app-server"


def window_label(minutes: int) -> str:
    """A window's label from its length: 300 -> "5h", 10080 -> "7d", 45 -> "45m"."""
    if minutes % 1440 == 0:
        return f"{minutes // 1440}d"
    if minutes % 60 == 0:
        return f"{minutes // 60}h"
    return f"{minutes}m"


def _number(value) -> float | None:
    """A finite JSON number as a float; None for anything else (bools included)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    return value if math.isfinite(value) else None


def _whole_minutes(value) -> int | None:
    """A window length: a positive whole number of minutes, else None."""
    number = _number(value)
    if number is None or not number.is_integer() or not 0 < number < _MAX_WINDOW_MINUTES:
        return None
    return int(number)


def _epoch(value) -> float | None:
    """Epoch seconds from a JSON number, else None."""
    number = _number(value)
    return number if number is not None and 0 < number < _MAX_EPOCH else None


def _window(raw, pct_key: str, minutes_key: str, reset_key: str) -> CodexWindow | None:
    """One window from its raw dict, or None when it has no usable pct or length."""
    if not isinstance(raw, dict):
        return None
    pct = _number(raw.get(pct_key))
    minutes = _whole_minutes(raw.get(minutes_key))
    if pct is None or minutes is None:
        return None
    return CodexWindow(
        label=window_label(minutes),
        pct=min(100.0, max(0.0, pct)),
        window_minutes=minutes,
        resets_at=_epoch(raw.get(reset_key)),
    )


def _windows(limits: dict, pct_key: str, minutes_key: str, reset_key: str) -> tuple[CodexWindow, ...]:
    found = [
        _window(limits.get(slot), pct_key, minutes_key, reset_key) for slot in ("primary", "secondary")
    ]
    return tuple(sorted((w for w in found if w is not None), key=lambda w: w.window_minutes))


def _plan(value) -> str | None:
    return value if isinstance(value, str) and value else None


def _record_time(value) -> float | None:
    """A record's timestamp: ISO 8601 with an offset, or epoch seconds."""
    if isinstance(value, str):
        try:
            when = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        if when.tzinfo is None:
            return None  # a time without an offset could be any zone's
        return when.timestamp()
    return _epoch(value)


def parse_rollout_line(line: str) -> CodexReading | None:
    """The reading in one rollout line, or None when it carries none.

    ``rate_limits`` is read at the top level or under ``payload`` (today's
    ``token_count`` events nest it there). A window without a numeric
    ``used_percent`` or a positive whole ``window_minutes`` is left out; a
    line left with no window, without a usable timestamp, or that is not a
    JSON object at all, is no reading. A percentage outside 0..100 is
    clamped; a missing or odd ``resets_at`` is None.
    """
    try:
        record = json.loads(line)
    except ValueError:
        return None
    if not isinstance(record, dict):
        return None
    payload = record.get("payload")
    payload = payload if isinstance(payload, dict) else {}
    limits = record.get("rate_limits")
    if limits is None:
        limits = payload.get("rate_limits")
    if not isinstance(limits, dict):
        return None
    windows = _windows(limits, "used_percent", "window_minutes", "resets_at")
    stamp = record.get("timestamp", payload.get("timestamp"))
    fetched_at = _record_time(stamp)
    if not windows or fetched_at is None:
        return None
    return CodexReading(windows, _plan(limits.get("plan_type")), fetched_at, "rollout")


def _is_rollout_name(name: str) -> bool:
    return name.startswith("rollout-") and name.endswith(".jsonl")


def _single_regular(st: os.stat_result) -> bool:
    """A regular file with one name: never a link, a FIFO, a device or a hard link."""
    return stat.S_ISREG(st.st_mode) and st.st_nlink == 1


def _read_tail(path: Path, max_bytes: int) -> bytes | None:
    """The file's last ``max_bytes``, from a line start; None when refused or unreadable.

    The file must be a single-name regular file both by its path (lstat,
    never following a link) and once open (fstat, the same device and
    inode), so a swap between the two cannot redirect the read.
    """
    try:
        before = os.lstat(path)
        if not _single_regular(before):
            return None
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return None
    try:
        st = os.fstat(fd)
        if not _single_regular(st) or (st.st_dev, st.st_ino) != (before.st_dev, before.st_ino):
            return None
        offset = max(0, st.st_size - max_bytes)
        chunks, wanted = [], st.st_size - offset
        while wanted > 0:
            chunk = os.pread(fd, min(wanted, 65536), offset)
            if not chunk:
                break
            chunks.append(chunk)
            offset += len(chunk)
            wanted -= len(chunk)
    except OSError:
        return None
    finally:
        os.close(fd)
    data = b"".join(chunks)
    if st.st_size > max_bytes:
        # The first line was cut; start at the next whole one.
        newline = data.find(b"\n")
        data = data[newline + 1:] if newline >= 0 else b""
    return data


def read_rollout_tail(
    path: Path, max_bytes: int = ROLLOUT_TAIL_BYTES, now: float | None = None
) -> CodexReading | None:
    """The last reading in the tail of one rollout file, or None.

    Only a file named ``rollout-*.jsonl`` is opened, and only when it is a
    regular file with one name (symlinks and hard links refused). With
    ``now``, a record dated more than ``FUTURE_ALLOWANCE_S`` ahead of it is
    skipped and the search goes on further back.
    """
    path = Path(path)
    if not _is_rollout_name(path.name):
        return None
    data = _read_tail(path, max_bytes)
    if not data:
        return None
    for raw in reversed(data.split(b"\n")):
        if not raw.strip():
            continue
        reading = parse_rollout_line(raw.decode("utf-8", errors="replace"))
        if reading is None:
            continue
        if now is not None and reading.fetched_at - now > FUTURE_ALLOWANCE_S:
            continue
        return reading
    return None


def _plain_dir(path: Path) -> bool:
    """A real directory, not a symlink to one."""
    try:
        return stat.S_ISDIR(os.lstat(path).st_mode)
    except OSError:
        return False


def _day_folders(sessions_dir: Path, now: float, days: int):
    """``sessions/YYYY/MM/DD`` for the last ``days`` local calendar days, as they exist.

    Each of the three levels must be a real directory: a symlink anywhere
    below the sessions folder is not followed.
    """
    today = datetime.fromtimestamp(now).date()
    for offset in range(days):
        day = today - timedelta(days=offset)
        year = sessions_dir / f"{day.year:04d}"
        month = year / f"{day.month:02d}"
        folder = month / f"{day.day:02d}"
        if _plain_dir(year) and _plain_dir(month) and _plain_dir(folder):
            yield folder


def _rollout_files(folder: Path) -> list[tuple[int, Path]]:
    """``(mtime_ns, path)`` of every single-name regular rollout file in ``folder``."""
    found = []
    try:
        entries = list(os.scandir(folder))
    except OSError:
        return found
    for entry in entries:
        if not _is_rollout_name(entry.name):
            continue
        try:
            st = entry.stat(follow_symlinks=False)
        except OSError:
            continue
        if _single_regular(st):
            found.append((st.st_mtime_ns, Path(entry.path)))
    return found


def newest_rollout_reading(
    sessions_dir: Path, now: float, limit: int = ROLLOUT_FILES_READ, days: int = ROLLOUT_DAYS
) -> CodexReading | None:
    """The newest reading among the ``limit`` most recently modified rollout files.

    Day folders of the last ``days`` local calendar days are looked at, not
    only today: a long session keeps writing to the file of the day it
    started. Two sessions can run at once and a session that just started
    has no rate-limit record yet, so several files are read and the record
    with the newest timestamp wins, not the file touched last. A missing
    sessions folder is None, never an exception.
    """
    sessions_dir = Path(sessions_dir)
    files: list[tuple[int, Path]] = []
    for folder in _day_folders(sessions_dir, now, days):
        files.extend(_rollout_files(folder))
    files.sort(key=lambda found: found[0], reverse=True)
    best: CodexReading | None = None
    for _mtime, path in files[:limit]:
        reading = read_rollout_tail(path, now=now)
        if reading is not None and (best is None or reading.fetched_at > best.fetched_at):
            best = reading
    return best
