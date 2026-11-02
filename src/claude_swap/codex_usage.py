"""Codex usage for the menu bar: rollout files, the app-server and its budget.

The menu bar shows one Codex block after the accounts: the usage
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
import re
import selectors
import signal
import stat
import subprocess
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from claude_swap.locking import FileLock

# ---- the readings ----------------------------------------------------------------

ROLLOUT_TAIL_BYTES = 256 * 1024  # bytes read from the end of each rollout file
ROLLOUT_FILES_READ = 5  # the most recently modified rollout files read per tick
ROLLOUT_DAYS = 7  # day folders looked at, today and the six before it
# A record dated further ahead of the clock than this is not a reading:
# real records never are, and one would otherwise win "newest" for good.
FUTURE_ALLOWANCE_S = 120.0
_MAX_WINDOW_MINUTES = 10_000_000
# Open flags where the platform has them (the menu bar is macOS only; the
# tests also run on Linux and Windows).
_OPEN_FLAGS = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0)
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


def finite_float(value) -> float | None:
    """A finite JSON number as a float; None for anything else.

    The one reader of every number in a rollout record, an app-server reply
    and the launch log. Bools, strings, NaN and the infinities are None, and
    so is an integer too large for a float (valid JSON, ``10**400``), which
    would otherwise raise and lose every other reading read with it.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        value = float(value)
    except OverflowError:
        return None
    return value if math.isfinite(value) else None


def _whole_minutes(value) -> int | None:
    """A window length: a positive whole number of minutes, else None."""
    number = finite_float(value)
    if number is None or not number.is_integer() or not 0 < number < _MAX_WINDOW_MINUTES:
        return None
    return int(number)


def _epoch(value) -> float | None:
    """Epoch seconds from a JSON number, else None."""
    number = finite_float(value)
    return number if number is not None and 0 < number < _MAX_EPOCH else None


def _window(raw, pct_key: str, minutes_key: str, reset_key: str) -> CodexWindow | None:
    """One window from its raw dict, or None when it has no usable pct or length."""
    if not isinstance(raw, dict):
        return None
    pct = finite_float(raw.get(pct_key))
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
        fd = os.open(path, _OPEN_FLAGS)
    except OSError:
        return None
    try:
        st = os.fstat(fd)
        if not _single_regular(st) or (st.st_dev, st.st_ino) != (before.st_dev, before.st_ino):
            return None
        offset = max(0, st.st_size - max_bytes)
        chunks, wanted = [], st.st_size - offset
        os.lseek(fd, offset, os.SEEK_SET)
        while wanted > 0:
            chunk = os.read(fd, min(wanted, 65536))
            if not chunk:
                break
            chunks.append(chunk)
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


# ---- the app-server, read only ----------------------------------------------------
#
# The conversation is the Usage Widget's: ``initialize`` (id 1), its reply,
# the ``initialized`` notification, ``account/rateLimits/read`` (id 2), its
# reply, then stdin is closed and the child reaped. Nothing else is ever
# sent. In particular ``account/rateLimitResetCredit/consume``, which would
# spend a banked reset, cannot be built: every outgoing line comes from the
# two encoders below, and they refuse anything off the allowlist.

ALLOWED_REQUESTS = frozenset({"initialize", "account/rateLimits/read"})
ALLOWED_NOTIFICATIONS = frozenset({"initialized"})
APP_SERVER_TIMEOUT_S = 20.0
CLIENT_NAME = "cswap-menubar"
OUTPUT_CAP = 1024 * 1024  # bytes of server output read before giving up
EXIT_GRACE_S = 2.0  # after stdin closes, for the server to leave on its own
TERM_GRACE_S = 1.0  # after SIGTERM, before SIGKILL
REAP_WAIT_S = 2.0
MAX_RESET_CREDITS = 999_999
REASON_MAX = 200
_EMAIL_RE = re.compile(r"[^\s@<>\"']+@[^\s@<>\"']+")


class RPCRefused(ValueError):
    """A message off the allowlist: it is never built, so it can never be sent."""


def _method_name(method) -> str:
    """A refused method as text: a str's own characters, never a subclass's say."""
    return str.__str__(method) if isinstance(method, str) else repr(method)


def _line(message: dict) -> bytes:
    return (json.dumps(message, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def rpc_request(id: int, method: str, params: dict | None = None) -> bytes:
    """One JSON line for a request; ``RPCRefused`` for any method not allowed.

    The name must be exactly a ``str``: a subclass can compare equal to an
    allowed name while serialising as another.
    """
    if type(method) is not str or method not in ALLOWED_REQUESTS:
        raise RPCRefused(f"refused: {_method_name(method)}")
    if type(id) is not int:
        raise RPCRefused("refused: a request id that is not an int")
    message: dict = {"id": id, "method": method}
    if params is not None:
        message["params"] = params
    return _line(message)


def rpc_notification(method: str) -> bytes:
    """One JSON line for a notification; ``RPCRefused`` for any method not allowed."""
    if type(method) is not str or method not in ALLOWED_NOTIFICATIONS:
        raise RPCRefused(f"refused: {_method_name(method)}")
    return _line({"method": method})


@dataclass(frozen=True)
class AppServerReply:
    """What one app-server conversation gave: a reading, the reset count, or why not."""

    reading: CodexReading | None
    reset_credits: int | None
    error: str | None


def _reset_count(value) -> int | None:
    number = finite_float(value)
    if number is None or not number.is_integer() or not 0 <= number <= MAX_RESET_CREDITS:
        return None
    return int(number)


def parse_rate_limits_result(result, now: float) -> tuple[CodexReading | None, int | None]:
    """The reading and the banked reset count in an ``account/rateLimits/read`` result.

    Windows come from ``rateLimits.primary``/``secondary``; the plan type
    from ``rateLimits.planType`` or, failing that, the result's own
    ``planType``. A result with no usable window is no reading (it must not
    wipe a good one) but its reset count still counts. The count is
    ``rateLimitResetCredits.availableCount`` when it is a whole number in
    0..``MAX_RESET_CREDITS``; anything else, a missing field included, is
    None, and the caller keeps the count it had.
    """
    if not isinstance(result, dict):
        return None, None
    reading = None
    limits = result.get("rateLimits")
    if isinstance(limits, dict):
        windows = _windows(limits, "usedPercent", "windowDurationMins", "resetsAt")
        if windows:
            plan = _plan(limits.get("planType")) or _plan(result.get("planType"))
            reading = CodexReading(windows, plan, now, "app-server")
    credits = result.get("rateLimitResetCredits")
    count = _reset_count(credits.get("availableCount")) if isinstance(credits, dict) else None
    return reading, count


def default_codex_dirs() -> list[Path]:
    """Where the CLI is looked for, in order; its child needs them on PATH (node is in Homebrew)."""
    dirs = [Path("/opt/homebrew/bin"), Path("/usr/local/bin")]
    try:
        dirs.append(Path.home() / ".local" / "bin")
    except RuntimeError:  # no home directory
        pass
    return dirs


def find_codex_binary(dirs: list[Path] | None = None) -> Path | None:
    """The first executable ``codex`` in ``dirs`` (default ``default_codex_dirs()``)."""
    for folder in default_codex_dirs() if dirs is None else dirs:
        candidate = Path(folder) / "codex"
        try:
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return candidate
        except OSError:
            continue
    return None


def _reason(text) -> str:
    """A server's error message fit for the log: one line, emails masked, capped."""
    if not isinstance(text, str) or not text.strip():
        return "error"
    text = " ".join(_EMAIL_RE.sub("<email>", text).split())
    return text[:REASON_MAX]


def _client_version() -> str:
    try:
        from claude_swap import __version__
    except Exception:
        return "0"
    return str(__version__)


def _child_env(env: dict | None) -> dict:
    child = dict(os.environ if env is None else env)
    inherited = child.get("PATH") or "/usr/bin:/bin:/usr/sbin:/sbin"
    child["PATH"] = os.pathsep.join([str(d) for d in default_codex_dirs()] + [inherited])
    return child


class _Failed(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _signal_group(pid: int, sig: int) -> None:
    try:
        os.killpg(pid, sig)
    except (ProcessLookupError, PermissionError):
        pass  # gone, or only the unreaped leader is left (EPERM on macOS)


class ChildGroup:
    """The running app-server's process group, so another thread can end it.

    The client calls ``spawned`` right after the launch and ``reaping``
    right before it reaps the leader. Between the two the leader is not
    reaped, so its process group id cannot belong to anyone else, and
    ``kill`` signals it under the same lock that ``reaping`` takes: it can
    never reach a group id that has been freed.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._pid: int | None = None
        self.launched = 0  # how many children this group has seen started

    def spawned(self, pid: int) -> None:
        with self._lock:
            self._pid = pid
            self.launched += 1

    def reaping(self) -> None:
        with self._lock:
            self._pid = None

    def kill(self) -> bool:
        """SIGKILL the live child's whole group; False when there is none."""
        with self._lock:
            if self._pid is None:
                return False
            _signal_group(self._pid, signal.SIGKILL)
            return True


def _kill_now(proc: subprocess.Popen, child: ChildGroup | None = None) -> None:
    """End the child's whole group at once and reap it."""
    _signal_group(proc.pid, signal.SIGKILL)
    if child is not None:
        child.reaping()  # before the reap: the group id is about to be freed
    try:
        proc.wait(timeout=REAP_WAIT_S)
    except subprocess.TimeoutExpired:
        pass


def _exited_within(pid: int, seconds: float, abort=None) -> bool:
    """Whether ``pid`` exits within ``seconds``, without reaping it.

    The leader stays unreaped until the end, so its process group id cannot
    be reused by another process while the group is being signalled. False
    at once when ``abort`` is set: nothing waits once Quit has asked.
    """
    deadline = time.monotonic() + seconds
    while True:
        try:
            if os.waitid(os.P_PID, pid, os.WEXITED | os.WNOWAIT | os.WNOHANG) is not None:
                return True
        except ChildProcessError:
            return True
        if (abort is not None and abort.is_set()) or time.monotonic() >= deadline:
            return False
        time.sleep(0.02)


class _Session:
    """One running app-server: line framing on its stdout, the deadline, the cap."""

    def __init__(self, proc: subprocess.Popen, deadline: float, abort=None, child: ChildGroup | None = None):
        self.proc = proc
        self.deadline = deadline
        self.abort = abort  # a threading.Event: set, the conversation ends at once
        self.child = child
        self.buffer = b""
        self.total = 0
        self.eof = False
        self.fd = proc.stdout.fileno()
        os.set_blocking(self.fd, False)
        self.selector = selectors.DefaultSelector()
        self.selector.register(self.fd, selectors.EVENT_READ)

    def send(self, line: bytes) -> None:
        try:
            self.proc.stdin.write(line)
            self.proc.stdin.flush()
        except (OSError, ValueError):  # BrokenPipeError: the server has gone
            raise _Failed("exited before answering") from None

    def await_result(self, want: int) -> dict:
        """The ``result`` of the reply to request ``want``; every other line is skipped."""
        while True:
            while b"\n" in self.buffer:
                raw, self.buffer = self.buffer.split(b"\n", 1)
                try:
                    message = json.loads(raw)
                except ValueError:
                    continue  # a log line
                if not (
                    isinstance(message, dict)
                    and type(message.get("id")) is int
                    and message["id"] == want
                    and "method" not in message  # a server request reusing the id
                ):
                    continue
                if isinstance(message.get("result"), dict):
                    return message["result"]
                if isinstance(message.get("error"), dict):
                    raise _Failed(_reason(message["error"].get("message")))
                raise _Failed("reply could not be read")
            if self.eof:
                raise _Failed("exited before answering")
            if self.abort is not None and self.abort.is_set():
                raise _Failed("aborted")
            remaining = self.deadline - time.monotonic()
            if remaining <= 0:
                raise _Failed("timeout")
            if not self.selector.select(min(remaining, 0.25)):
                continue
            try:
                chunk = os.read(self.fd, 65536)
            except BlockingIOError:
                continue
            except OSError:
                chunk = b""
            if not chunk:
                self.eof = True
                continue
            self.total += len(chunk)
            if self.total > OUTPUT_CAP:
                raise _Failed("output too large")
            self.buffer += chunk

    def finish(self, graceful: bool) -> None:
        """Close stdin, give a server that answered a moment to leave, then end its group.

        After a timeout, closed output or a flood, the group is signalled at
        once. SIGKILL always goes to the whole group last, so nothing the
        server started is left running; then the leader is reaped. With the
        abort set (Quit, or Show Codex off), at any point here, every wait is
        cut short and SIGKILL goes at once.
        """
        try:
            self.proc.stdin.close()
        except (OSError, ValueError):
            pass
        pid = self.proc.pid
        if not (graceful and _exited_within(pid, EXIT_GRACE_S, self.abort)):
            if not (self.abort is not None and self.abort.is_set()):
                _signal_group(pid, signal.SIGTERM)
                _exited_within(pid, TERM_GRACE_S, self.abort)
        _kill_now(self.proc, self.child)
        try:
            self.selector.close()
            self.proc.stdout.close()
        except (OSError, ValueError):
            pass


def read_rate_limits(
    binary: Path,
    timeout: float = APP_SERVER_TIMEOUT_S,
    now: float | None = None,
    env: dict | None = None,
    abort=None,
    child: ChildGroup | None = None,
) -> AppServerReply:
    """Ask ``codex app-server`` for the rate limits and the banked reset count. Never raises.

    The three outgoing lines are built before anything is launched, so a
    refused method starts nothing. The child runs in its own process group
    with ``default_codex_dirs()`` first on its PATH; its stderr is not read.
    Non-JSON lines, notifications and server requests are skipped while the
    reply is awaited. On a timeout (the whole conversation, ``timeout``
    seconds) the group is killed and the error is "timeout". ``now`` stamps
    the reading; it defaults to the time of the reply. ``abort`` (a
    ``threading.Event``) ends the conversation within a quarter second of
    being set, the child's group killed and reaped, error "aborted"; set
    before the launch, nothing is launched. ``child`` (a ``ChildGroup``) is
    told about the child from its launch to its reap.
    """
    try:
        return _converse(Path(binary), float(timeout), now, env, abort, child)
    except Exception as exc:  # a backstop: the conversation reports its own failures
        return AppServerReply(None, None, f"failed: {type(exc).__name__}")


def _converse(
    binary: Path, timeout: float, now: float | None, env: dict | None, abort=None, child: ChildGroup | None = None
) -> AppServerReply:
    try:
        hello = rpc_request(1, "initialize", {"clientInfo": {"name": CLIENT_NAME, "version": _client_version()}})
        ready = rpc_notification("initialized")
        ask = rpc_request(2, "account/rateLimits/read")
    except RPCRefused as exc:
        return AppServerReply(None, None, str(exc))
    if abort is not None and abort.is_set():
        return AppServerReply(None, None, "aborted")
    deadline = time.monotonic() + timeout
    try:
        proc = subprocess.Popen(
            [str(binary), "app-server"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
            env=_child_env(env),
        )
    except (OSError, ValueError) as exc:
        return AppServerReply(None, None, f"could not start: {getattr(exc, 'strerror', None) or type(exc).__name__}")
    if child is not None:
        child.spawned(proc.pid)
    try:
        session = _Session(proc, deadline, abort, child)
    except Exception:
        _kill_now(proc, child)  # nothing could talk to it: never leave it running
        raise
    graceful = False
    try:
        session.send(hello)
        session.await_result(1)
        session.send(ready)
        session.send(ask)
        result = session.await_result(2)
        graceful = True
    except _Failed as exc:
        graceful = exc.reason not in ("timeout", "exited before answering", "output too large", "aborted")
        return AppServerReply(None, None, exc.reason)
    finally:
        session.finish(graceful)
    reading, credits = parse_rate_limits_result(result, time.time() if now is None else now)
    return AppServerReply(reading, credits, None)


# ---- the launch budget ---------------------------------------------------------
#
# Each ``codex app-server`` launch leaves a folder in ~/.codex/.tmp, so
# launches are few: one when the menu bar starts, then one every six hours
# for the reset count (it changes over days), and Refresh now may add one
# at most every half hour. Never more than eight in any 24 hours, counted
# from a small log on disk so a restart cannot reset the count. The same
# file keeps the last reset count the app-server reported, so the badge has
# it at once after a restart instead of waiting for the next launch.

DAY_S = 24 * 3600.0
_LOG_KEEP = 64  # entries read from the log at most; it holds a day's launches
LOCK_WAIT_S = 2.0  # how long a reservation waits for another menu bar's lock
LOCK_ATTEMPTS = 5  # locks taken on a sidecar no longer at its path before giving up
# A launch or a reset count dated further ahead of the clock than this was
# written under a wrong clock: dropped, never waited out (a clock fast by
# days would otherwise hold the budget full for days after it is put right).
FUTURE_SLACK_S = 300.0


def dated_ahead(at: float, now: float) -> bool:
    """Whether a time recorded as ``at`` is further ahead of ``now`` than
    ``FUTURE_SLACK_S``: written under a wrong clock, so not to be trusted.
    The one rule for launches, a saved reset count and a live one."""
    return at - now > FUTURE_SLACK_S


def _read_clock(now) -> float:
    """A time given as a number, or read now from a clock (a callable)."""
    return float(now()) if callable(now) else float(now)


class LaunchBudget:
    """When ``codex app-server`` may be launched, from a log of recent launches.

    The log (``{"launches": [{"t": epoch, "id": token}, ...],
    "reset_credits": 2, "credits_at": epoch}``) is shared by every menu bar
    using the backup folder. Each launch carries its reservation's own token,
    so a reservation taken back (``release``) removes only itself, never
    another menu bar's launch in the same second; counting ignores the
    tokens. A bare number (the log as earlier builds wrote it) reads as a
    launch with no token, counted and never released. A launch
    is taken with ``reserve``: under an exclusive lock on a sidecar file
    (``<log>.lock``; the log itself is replaced on every write, so a lock on
    it would not hold), the log is read, the rules decide, and the launch is
    written down, in one step. There is no launch without a written
    reservation: a log that cannot be written, or a lock that cannot be had,
    admits nothing, so neither two menu bars nor a run of restarts can get
    past the limits. A launch dated a few minutes ahead of the clock counts
    as just made; one further ahead (``FUTURE_SLACK_S``) is a wrong clock's
    and is dropped, and left out when the log is next written. The same
    goes for the reset count's time. The reset count is written only together
    with its time, and a count or time that does not read back as valid is
    dropped. ``last_failure`` says why the last reservation or record could
    not be written ("launch log unwritable", "launch log busy",
    "launch log unreadable", "launch log missing").

    A history that cannot be trusted admits nothing and is never written
    over (fail closed): content that does not read as this log, cut off,
    or with any entry that is not a launch, is "unreadable", unless the
    file was last written more than a day ago, when it cannot hold a launch
    inside the rolling day and is read as empty (and written over by the
    next reservation); a log that
    this process has read or written before and that is gone now is
    "missing" (a missing log is a fresh start only for a process that has
    never seen one); and the log's folder is never made again, so a
    vanished folder is "missing" too.

    The log, read under the lock, is the only record: nothing is kept in
    memory beside it. A copy in memory could only bring back what another
    menu bar trimmed for a reason (entries a wrong clock dated days ahead),
    and since no launch goes without a written reservation it is not
    needed to hold the limits.

    Every writer may pass its time as a clock (a callable) rather than a
    number: it is then read inside the lock, after any other writer has
    finished, so a time taken before a long scan cannot make that writer's
    newer entries look dated ahead and drop them.
    """

    def __init__(
        self,
        path: Path,
        daily_cap: int = 8,
        scheduled_every: float = 6 * 3600.0,
        manual_every: float = 1800.0,
        lock_timeout: float = LOCK_WAIT_S,
    ):
        self.path = Path(path)
        self.lock_path = self.path.with_name(self.path.name + ".lock")
        self.daily_cap = daily_cap
        self.scheduled_every = scheduled_every
        self.manual_every = manual_every
        self.lock_timeout = lock_timeout
        self.last_failure: str | None = None
        self.last_reservation: str | None = None  # the token of the last reservation written
        self._seen = False  # this process has read or written the log at this path
        self._held: FileLock | None = None  # the lock while this budget holds it

    def _load(self) -> tuple[str, dict]:
        """``("ok", log)``, ``("missing", {})`` or ``("unreadable", {})``."""
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return "missing", {}
        except (OSError, ValueError):
            return "unreadable", {}
        if not isinstance(raw, dict) or not isinstance(raw.get("launches"), list):
            return "unreadable", {}
        return "ok", raw

    @staticmethod
    def _entries(raw: dict) -> list[tuple[float, str | None]] | None:
        """``(time, token)`` for each launch, or None when any entry is not one.

        An entry is ``{"t": time, "id": token or null}``, or a bare number
        (no token) as earlier builds wrote it.
        """
        found = []
        for entry in raw["launches"][-_LOG_KEEP:]:
            if isinstance(entry, dict):
                t, token = finite_float(entry.get("t")), entry.get("id")
                if token is not None and not (isinstance(token, str) and token):
                    return None
            else:
                t, token = finite_float(entry), None
            if t is None:
                return None
            found.append((t, token))
        return found

    def _stale(self, now: float) -> bool:
        """The log is a regular file last written more than ``DAY_S`` before ``now``.

        Every launch rewrites the log, so such a file cannot hold a launch
        inside the rolling day, whatever its content. A modification time
        ahead of the clock is never stale.
        """
        try:
            st = os.lstat(self.path)
        except OSError:
            return False
        return stat.S_ISREG(st.st_mode) and now - st.st_mtime > DAY_S

    def _history(self, now: float) -> list[tuple[float, str | None]] | None:
        """The log's launches inside the rolling day, as ``(time, token)``, oldest
        first; None, with ``last_failure`` set, when the log cannot be trusted.

        Two launches in the same second are two launches: nothing is
        deduplicated. Entries more than ``FUTURE_SLACK_S`` ahead of ``now``
        are left out.
        """
        status, raw = self._load()
        if status == "missing":
            if self._seen or not self.path.parent.is_dir():
                self.last_failure = "launch log missing"
                return None
            return []  # this process's first run at this path
        entries = self._entries(raw) if status == "ok" else None
        if entries is None:
            if self._stale(now):
                return []  # nothing in it can be inside the day: start afresh
            self.last_failure = "launch log unreadable"
            return None
        self._seen = True
        return sorted(
            (e for e in entries if now - e[0] < DAY_S and not dated_ahead(e[0], now)),
            key=lambda e: e[0],
        )

    def load_credits(self, now: float | None = None) -> tuple[int | None, float | None]:
        """The saved reset count and when it was reported, or ``(None, None)``.

        With ``now``, a count dated more than ``FUTURE_SLACK_S`` ahead of it
        is a wrong clock's and is not trusted.
        """
        _status, raw = self._load()
        count, at = _reset_count(raw.get("reset_credits")), finite_float(raw.get("credits_at"))
        if count is None or at is None or (now is not None and dated_ahead(at, now)):
            return None, None
        return count, at

    def save_credits(self, credits: int, at: float, now=None) -> None:
        """Keep a reported reset count with its time, beside the launches. Never raises.

        Written under the lock, so it cannot undo a reservation another menu
        bar is writing; when the lock cannot be had the count is not saved
        this time (the menu still shows it). A newer count already in the
        log is kept. ``now`` (a number or a clock, read inside the lock;
        default ``at``) dates the rest of the log.
        """
        lock = self._acquire()
        if lock is None:
            return
        try:
            now = _read_clock(at if now is None else now)
            history = self._history(now)
            if history is not None:  # never over a log that cannot be trusted
                self._write(now, history, credits=(credits, at))
        finally:
            self._release(lock)

    def _acquire(self) -> FileLock | None:
        """The budget's lock, held; None, with ``last_failure`` set, when it cannot be had.

        The log's folder is never made: a missing one is "launch log missing".
        A lock is kept only on the file that is at the sidecar's path once
        locked (``FileLock.holds_path``): if the sidecar was deleted or
        replaced, another menu bar may hold a lock on its successor, so the
        lock is dropped and taken again, ``LOCK_ATTEMPTS`` times at most
        ("launch log busy").
        """
        for _attempt in range(LOCK_ATTEMPTS):
            if not self.path.parent.is_dir():
                self.last_failure = "launch log missing"
                return None
            lock = FileLock(self.lock_path, timeout=self.lock_timeout, create_parent=False)
            try:
                if not lock.acquire():
                    self.last_failure = "launch log busy"
                    return None
            except OSError:  # the lock file cannot even be created
                missing = not self.path.parent.is_dir()
                self.last_failure = "launch log missing" if missing else "launch log unwritable"
                return None
            if lock.holds_path():
                self._held = lock
                return lock
            lock.release()  # a lock on a file no longer at the path: take it again
        self.last_failure = "launch log busy"
        return None

    def _release(self, lock: FileLock) -> None:
        self._held = None
        lock.release()

    def launches_in_last_day(self, now: float) -> int:
        """Launches in the rolling day; a log that cannot be trusted counts as full."""
        history = self._history(now)
        return self.daily_cap if history is None else len(history)

    def may_launch(self, now: float, manual: bool, first_of_process: bool) -> bool:
        """Whether a launch may go now. In order: a log that can be trusted,
        the daily cap, the first launch of this process, a manual one 30
        minutes after the last, a scheduled one six hours after it."""
        return self._allows(self._history(now), now, manual, first_of_process)

    def _allows(self, history, now: float, manual: bool, first_of_process: bool) -> bool:
        if history is None:
            return False
        recent = [t for t, _token in history]
        if len(recent) >= self.daily_cap:
            return False
        if first_of_process or not recent:
            return True
        since = now - recent[-1]
        return since >= (self.manual_every if manual else self.scheduled_every)

    def reserve(self, now: float, manual: bool, first_of_process: bool, go=None) -> bool:
        """Decide and write down a launch in one locked step. Never raises.

        True only when the rules allow it and the reservation is in the log;
        the caller launches only then. ``go()``, when given, is asked last,
        under the lock: False (Show Codex turned off while this waited for
        the lock) writes nothing. ``now`` is a number or a clock read inside
        the lock. ``last_reservation`` is the written reservation's token,
        for ``release``.
        """
        self.last_failure = None
        self.last_reservation = None
        lock = self._acquire()
        if lock is None:
            return False
        try:
            now = _read_clock(now)
            history = self._history(now)
            if not self._allows(history, now, manual, first_of_process):
                return False
            if go is not None and not go():
                return False
            token = self._append(now, history)
            if token is None:
                return False
            self.last_reservation = token
            return True
        finally:
            self._release(lock)

    def release(self, token: str | None, now=None) -> None:
        """Take back the reservation ``token`` (its child never started). Never raises.

        Only one entry with that token goes: one already trimmed is not
        looked for under its time, so another menu bar's launch in the same
        second stays. A launch without a token is never released. ``now`` (a
        number or a clock, read inside the lock; default the time now) dates
        the rest of the log.
        """
        if not token:
            return
        lock = self._acquire()
        if lock is None:
            return
        try:
            now = _read_clock(time.time if now is None else now)
            launches = self._history(now)
            if launches is None:
                return  # never over a log that cannot be trusted
            for index, entry in enumerate(launches):
                if entry[1] == token:
                    # Exactly one: a log copied with a token repeated must not
                    # lose several counted launches to one release.
                    self._write(now, launches[:index] + launches[index + 1:])
                    break
        finally:
            self._release(lock)

    def record(self, now) -> bool:
        """Write down a launch without asking the rules (locked). True when written. Never raises.

        ``now`` is a number or a clock read inside the lock.
        """
        self.last_failure = None
        lock = self._acquire()
        if lock is None:
            return False
        try:
            now = _read_clock(now)
            history = self._history(now)
            return history is not None and self._append(now, history) is not None
        finally:
            self._release(lock)

    def _append(self, now: float, history) -> str | None:
        """Add a launch at ``now`` with a new token to ``history`` and write it (lock
        held); the token, or None."""
        token = uuid.uuid4().hex
        if not self._write(now, sorted(history + [(now, token)], key=lambda e: e[0])):
            return None
        return token

    def _write(self, now: float, launches: list[tuple[float, str | None]], credits=None) -> bool:
        """Replace the log (lock held, atomically) with ``launches`` and the reset
        count: ``credits`` (count, time) when given and newer than the log's,
        else the log's own, when it is valid and not dated ahead of ``now``.
        False, with ``last_failure`` set, when it could not be written, or when
        the lock held no longer guards the log (its sidecar was deleted or
        replaced while held: another menu bar may be writing too), checked
        last, right before the new log takes the old one's place."""
        data: dict = {"launches": [{"t": t, "id": token} for t, token in launches]}
        saved = self.load_credits(now)
        if credits is not None and not dated_ahead(credits[1], now) and (
            saved[0] is None or credits[1] >= saved[1]
        ):
            saved = credits
        if saved[0] is not None:
            data["reset_credits"], data["credits_at"] = saved
        tmp = None
        try:
            fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=self.path.name + ".", suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f)
            if self._held is not None and not self._held.holds_path():
                self.last_failure = "launch log busy"
                return False
            os.replace(tmp, self.path)
            tmp = None
            self._seen = True
            return True
        except OSError:
            self.last_failure = "launch log unwritable"
            return False
        finally:
            if tmp is not None:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass


# ---- the monitor: state and its own worker ------------------------------------------
#
# Codex reads run on their own thread, never inside the account snapshot
# worker: that worker's in-flight flag drops every refresh asked for while
# it runs, so a 20 second app-server call there would leave the account rows
# stale after a switch. Like that worker, this one only rebinds a plain
# attribute for the main thread to read.

CREDITS_FRESH_S = DAY_S  # a reset count older than this is shown as unknown
CLOSE_REAP_S = 0.5  # after close kills the child itself, for the worker to reap it


@dataclass(frozen=True)
class CodexState:
    """What the menu bar shows for Codex."""

    present: bool  # a codex binary was found or ~/.codex/sessions exists
    reading: CodexReading | None  # the newest reading seen, from either source
    reset_credits: int | None  # banked usage resets, from the app-server only
    credits_at: float | None  # when the app-server last reported the count
    error: str | None  # why the last app-server read failed, if it did

    def credits_known(self, now: float) -> bool:
        """A reset count reported within the last day, and not dated ahead of
        the clock (``dated_ahead``: reported under a clock since put right)."""
        return (
            self.reset_credits is not None
            and self.credits_at is not None
            and now - self.credits_at <= CREDITS_FRESH_S
            and not dated_ahead(self.credits_at, now)
        )


ABSENT = CodexState(False, None, None, None, None)


def ur_badge(credits: int | None, known: bool) -> tuple[str, str]:
    """The usage-resets badge: ("UR 2", "ok"), ("UR 0", "over") or ("UR ?", "unknown")."""
    if not known or credits is None:
        return "UR ?", "unknown"
    return f"UR {credits}", "over" if credits == 0 else "ok"


def _newest(*readings: CodexReading | None) -> CodexReading | None:
    """The reading measured last; on a tie the earlier argument wins."""
    best = None
    for reading in readings:
        if reading is not None and (best is None or reading.fetched_at > best.fetched_at):
            best = reading
    return best


class CodexMonitor:
    """Keeps the Codex state current: rollouts every tick, the app-server on budget.

    ``tick`` is one synchronous read; ``request`` runs it on a daemon thread,
    one at a time, and a request made while one runs is run once more after
    it (a manual one, Refresh now, stays manual). ``state`` is read without a
    lock: the worker only ever rebinds it. ``enabled`` off (Show Codex off)
    drops a queued read, refuses new ones and stops a read already running
    short of a launch. ``close`` (Quit) ends a read in
    flight, its child included, and stops all reads for good: the worker is
    a daemon thread and the child has its own session, so nothing would end
    them once the app is gone.
    """

    def __init__(
        self,
        sessions_dir: Path,
        budget: LaunchBudget,
        binary: Path | None,
        logger,
        read_rollouts=newest_rollout_reading,
        read_app_server=read_rate_limits,
        clock=time.time,
        start_thread=None,
    ):
        self.sessions_dir = Path(sessions_dir)
        self.budget = budget
        self.binary = binary
        self._logger = logger
        self._read_rollouts = read_rollouts
        self._read_app_server = read_app_server
        self._clock = clock
        self._start_thread = start_thread or (
            lambda target: threading.Thread(target=target, daemon=True, name="codex-monitor").start()
        )
        self._first_of_process = True  # the launch at start is still owed
        self._logged_failure: str | None = None  # the reservation failure last logged
        self._app_reading: CodexReading | None = None
        self._lock = threading.Lock()
        self._running = False
        self._again = False
        self._again_manual = False
        self._on_done = None
        self._closed = False
        self._enabled = True
        self._abort = threading.Event()  # set by close: a read in flight ends now
        self._child = ChildGroup()  # the app-server child while one runs
        # The count saved before a restart shows at once; the launch at
        # start replaces it, and the day rule dims it if it is too old.
        try:
            credits, credits_at = budget.load_credits(self._clock())
        except Exception:
            credits, credits_at = None, None
        self._state = CodexState(self._present(), None, credits, credits_at, None)

    @property
    def state(self) -> CodexState:
        return self._state

    @property
    def enabled(self) -> bool:
        return self._enabled

    @enabled.setter
    def enabled(self, value: bool) -> None:
        """Show Codex. Off drops what is queued, and sets the abort: a reservation
        still waiting for its lock writes nothing, one written but unused is
        taken back, and a child already running is ended. On clears it again
        (unless closed)."""
        with self._lock:
            self._enabled = bool(value)
            if not self._enabled:
                self._again = self._again_manual = False
                self._abort.set()
            elif not self._closed:
                self._abort.clear()

    @property
    def busy(self) -> bool:
        """A read is running or queued."""
        return self._running

    def _present(self) -> bool:
        if self.binary is not None:
            return True
        try:
            return self.sessions_dir.is_dir()
        except OSError:
            return False

    def tick(self, manual: bool = False) -> CodexState:
        """One read: the rollouts, then the app-server when the budget allows.

        Windows come from the newest of the reply, this tick's rollout
        reading and the last reading shown; the reset count changes only when
        a reply carries one. Failures are logged at debug and kept as
        ``error``; nothing raises.
        """
        if not self._present():
            self._state = ABSENT
            return ABSENT
        now = self._clock()
        previous = self._state
        rollout = None
        try:
            rollout = self._read_rollouts(self.sessions_dir, now)
        except Exception as exc:
            self._logger.debug("Codex rollout read failed: %s", type(exc).__name__)
        credits, credits_at, error = previous.reset_credits, previous.credits_at, previous.error
        launch, failure = (False, None)
        # Checked here, right before the launch: Show Codex turned off or
        # Quit while the rollouts were read starts no child.
        if self.binary is not None and self._enabled and not self._closed:
            launch, failure = self._reserve(manual)
        if failure:
            error = failure  # no launch; the badge keeps what it had
        reservation = self.budget.last_reservation if launch else None
        if launch and not self._wanted():
            # Turned off (or Quit) after the reservation was written: no child.
            self.budget.release(reservation, now=self._clock)
            launch = False
        if launch:
            # Written down before it runs: a launch litters even when it fails.
            was_first, self._first_of_process = self._first_of_process, False
            launched_before = self._child.launched
            try:
                reply = self._read_app_server(self.binary, abort=self._abort, child=self._child)
            except Exception as exc:  # the client never raises; a backstop
                reply = AppServerReply(None, None, f"failed: {type(exc).__name__}")
            if reply.error == "aborted" and self._child.launched == launched_before:
                # Stopped before any child started: the reservation is taken
                # back, and with it, a read at start is still owed.
                self.budget.release(reservation, now=self._clock)
                self._first_of_process = was_first
            error = reply.error
            if error:
                self._logger.debug("Codex app-server read failed: %s", error)
            if reply.reading is not None:
                self._app_reading = reply.reading
            if reply.reset_credits is not None:
                credits, credits_at = reply.reset_credits, self._clock()
                self.budget.save_credits(credits, credits_at, now=self._clock)
        state = CodexState(
            True, _newest(self._app_reading, rollout, previous.reading), credits, credits_at, error
        )
        self._state = state
        return state

    def _wanted(self) -> bool:
        """Reads may still launch: Show Codex is on and Quit has not closed the monitor."""
        return self._enabled and not self._closed

    def _reserve(self, manual: bool) -> tuple[bool, str | None]:
        """``(True, None)`` with a written reservation for a launch now; else
        ``(False, why)``, why being None when the rules simply say not yet.
        A failure is logged at debug once, not on every tick it repeats."""
        # The clock, not ``now``: the time is read inside the lock (see LaunchBudget).
        if self.budget.reserve(self._clock, manual, self._first_of_process, go=self._wanted):
            self._logged_failure = None
            return True, None
        failure = self.budget.last_failure
        if failure and failure != self._logged_failure:
            self._logger.debug("Codex app-server not started: %s", failure)
            self._logged_failure = failure
        return False, failure

    def close(self, timeout: float = 2.0) -> bool:
        """End any read in flight (its child's group killed and reaped) and every read after.

        Waits up to ``timeout`` seconds for the worker, whose cleanup cuts
        every wait short once asked. If the worker is still not done, the
        child's group is killed from here (``kill_child``) and the worker
        gets a moment more to reap it. True when the worker has stopped.
        Called by Quit before the app goes.
        """
        with self._lock:
            self._closed = True
            self._again = self._again_manual = False
        self._abort.set()
        deadline = time.monotonic() + timeout
        while self._running and time.monotonic() < deadline:
            time.sleep(0.02)
        if self._running:
            self.kill_child()
            deadline = time.monotonic() + CLOSE_REAP_S
            while self._running and time.monotonic() < deadline:
                time.sleep(0.02)
        return not self._running

    def kill_child(self) -> bool:
        """SIGKILL the running app-server's group from any thread; False when none runs."""
        return self._child.kill()

    def request(self, manual: bool = False, on_done=None) -> None:
        """Run ``tick`` on the worker thread; ``on_done(state)`` runs there after each run."""
        with self._lock:
            if self._closed or not self._enabled:
                return
            if on_done is not None:
                self._on_done = on_done
            if self._running:
                self._again = True
                self._again_manual = self._again_manual or manual
                return
            self._running = True
        try:
            self._start_thread(lambda: self._run(manual))
        except Exception:
            with self._lock:
                self._running = False
            self._logger.debug("Codex read could not start", exc_info=True)

    def _run(self, manual: bool) -> None:
        while True:
            try:
                state = self.tick(manual)
            except Exception:
                self._logger.debug("Codex read failed", exc_info=True)
                state = self._state
            callback = self._on_done
            if callback is not None:
                try:
                    callback(state)
                except Exception:
                    self._logger.debug("Codex read callback failed", exc_info=True)
            with self._lock:
                if not self._again or not self._enabled or self._closed:
                    self._running = False
                    return
                manual, self._again, self._again_manual = self._again_manual, False, False
