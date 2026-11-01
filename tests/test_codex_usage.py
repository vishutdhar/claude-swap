"""Codex usage for the menu bar: rollout files, the app-server, the launch budget.

Everything here runs against temporary files and a fake ``codex app-server``
script (``tests/fixtures/fake_codex_app_server.py``); no test reads the real
``~/.codex`` or starts the real CLI (conftest's ``_no_real_codex`` guard).
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from claude_swap import codex_usage as cu

RESETS_AT = 1793774344
# Links, the executable bit and the fake server (a shell script) are POSIX;
# the menu bar that uses all this is macOS only.
posix_only = pytest.mark.skipif(sys.platform == "win32", reason="POSIX links, modes and shell scripts")
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


@posix_only
def test_read_rollout_tail_refuses_symlink(tmp_path):
    real = _rollout(tmp_path / "real", "rollout-a.jsonl", _line(_record()))
    link = tmp_path / "rollout-link.jsonl"
    link.symlink_to(real)
    assert cu.read_rollout_tail(real) is not None
    assert cu.read_rollout_tail(link) is None


@posix_only
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


@posix_only
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


# --- Task 2: the app-server client, read only ---------------------------------------

FAKE_SERVER = Path(__file__).parent / "fixtures" / "fake_codex_app_server.py"


def test_rpc_allowlist_refuses_consume():
    with pytest.raises(cu.RPCRefused):
        cu.rpc_request(3, "account/rateLimitResetCredit/consume")
    with pytest.raises(cu.RPCRefused):
        cu.rpc_notification("initialize")  # a request name is not a notification
    with pytest.raises(cu.RPCRefused):
        cu.rpc_request(1, "initialized")  # nor the other way round
    assert cu.ALLOWED_REQUESTS == frozenset({"initialize", "account/rateLimits/read"})
    assert cu.ALLOWED_NOTIFICATIONS == frozenset({"initialized"})


def test_rpc_allowlist_cannot_be_fooled_by_a_lookalike_string():
    class Lookalike(str):
        # Compares and hashes like an allowed name, serialises as itself.
        def __eq__(self, other):
            return True

        def __hash__(self):
            return hash("initialize")

    with pytest.raises(cu.RPCRefused):
        cu.rpc_request(1, Lookalike("account/rateLimitResetCredit/consume"))
    with pytest.raises(cu.RPCRefused):
        cu.rpc_notification(Lookalike("account/rateLimitResetCredit/consume"))


def test_rpc_lines_are_sorted_json_with_newline():
    line = cu.rpc_request(1, "initialize", {"clientInfo": {"version": "1", "name": "x"}})
    assert line.endswith(b"\n") and line.count(b"\n") == 1
    message = json.loads(line)
    assert message == {"id": 1, "method": "initialize", "params": {"clientInfo": {"version": "1", "name": "x"}}}
    assert line == (json.dumps(message, sort_keys=True, separators=(",", ":")) + "\n").encode()
    assert json.loads(cu.rpc_request(2, "account/rateLimits/read")) == {"id": 2, "method": "account/rateLimits/read"}
    assert cu.rpc_notification("initialized") == b'{"method":"initialized"}\n'


def test_parse_rate_limits_result():
    ok = {"rateLimits": {"primary": {"usedPercent": 56, "windowDurationMins": 10080, "resetsAt": RESETS_AT}},
          "rateLimitResetCredits": {"availableCount": 2}, "planType": "pro"}
    reading, credits = cu.parse_rate_limits_result(ok, NOW)
    assert credits == 2
    assert reading == cu.CodexReading(
        (cu.CodexWindow("7d", 56.0, 10080, float(RESETS_AT)),), "pro", NOW, "app-server"
    )
    # The plan type as the CLI sends it today: inside rateLimits.
    inside = {"rateLimits": {"planType": "plus", "primary": ok["rateLimits"]["primary"], "secondary": None}}
    assert cu.parse_rate_limits_result(inside, NOW)[0].plan_type == "plus"
    assert cu.parse_rate_limits_result({"rateLimits": {}, "rateLimitResetCredits": {"availableCount": 2}}, NOW) == (None, 2)
    assert cu.parse_rate_limits_result({}, NOW) == (None, None)
    for bad in (-1, 1e7, "2", True, None, 2.5):
        assert cu.parse_rate_limits_result({"rateLimitResetCredits": {"availableCount": bad}}, NOW) == (None, None)
    assert cu.parse_rate_limits_result({"rateLimitResetCredits": {"availableCount": 0}}, NOW) == (None, 0)


def _server(tmp_path, mode):
    """A launcher script for the fake server, as the real binary is launched."""
    log = tmp_path / f"{mode}.log"
    script = tmp_path / f"codex-{mode}"
    script.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{FAKE_SERVER}" {mode} "{log}" "$@"\n', encoding="utf-8")
    script.chmod(0o755)
    return script, log


def _log(log):
    return log.read_text(encoding="utf-8").splitlines() if log.exists() else []


def _methods(log):
    return [line[len("METHOD "):] for line in _log(log) if line.startswith("METHOD ")]


def _pid(log):
    (line,) = [line for line in _log(log) if line.startswith("PID ")]
    return int(line[len("PID "):])


def _gone(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    return False


@posix_only
def test_read_rate_limits_ok_sends_only_allowed_methods(tmp_path):
    server, log = _server(tmp_path, "ok")
    reply = cu.read_rate_limits(server, timeout=10, now=NOW)
    assert reply.error is None
    assert reply.reset_credits == 2
    assert reply.reading.plan_type == "pro"
    assert [(w.label, w.pct) for w in reply.reading.windows] == [("7d", 56.0)]
    assert reply.reading.fetched_at == NOW
    assert _methods(log) == ["initialize", "initialized", "account/rateLimits/read"]
    assert "ARGS app-server" in _log(log)
    assert _gone(_pid(log))  # reaped, not left behind


@posix_only
def test_read_rate_limits_today_shape(tmp_path):
    server, _log_path = _server(tmp_path, "realshape")
    reply = cu.read_rate_limits(server, timeout=10, now=NOW)
    assert (reply.reading.plan_type, reply.reset_credits, reply.error) == ("pro", 2, None)


@posix_only
def test_read_rate_limits_without_credits(tmp_path):
    server, _log_path = _server(tmp_path, "nocredits")
    reply = cu.read_rate_limits(server, timeout=10, now=NOW)
    assert reply.reading is not None and reply.reset_credits is None and reply.error is None


@posix_only
def test_read_rate_limits_noisy_still_finds_reply(tmp_path):
    server, log = _server(tmp_path, "noisy")
    reply = cu.read_rate_limits(server, timeout=10, now=NOW)
    assert reply.error is None and reply.reset_credits == 2
    assert _methods(log) == ["initialize", "initialized", "account/rateLimits/read"]


@posix_only
def test_read_rate_limits_rpcerror(tmp_path):
    server, _log_path = _server(tmp_path, "rpcerror")
    reply = cu.read_rate_limits(server, timeout=10, now=NOW)
    assert reply.reading is None and reply.reset_credits is None
    # The server's own message, with any email address masked: it goes to the log.
    assert reply.error == "not logged in as <email>"


@posix_only
def test_read_rate_limits_hang_times_out_and_kills(tmp_path):
    import time

    server, log = _server(tmp_path, "hang")
    started = time.monotonic()
    reply = cu.read_rate_limits(server, timeout=1, now=NOW)
    assert time.monotonic() - started < 3
    assert reply == cu.AppServerReply(None, None, "timeout")
    assert _gone(_pid(log))


@posix_only
def test_read_rate_limits_server_that_exits(tmp_path):
    server, log = _server(tmp_path, "exit")
    reply = cu.read_rate_limits(server, timeout=10, now=NOW)
    assert reply.reading is None and reply.error == "exited before answering"
    assert _methods(log) == []


@posix_only
def test_read_rate_limits_caps_its_output(tmp_path):
    server, log = _server(tmp_path, "flood")
    reply = cu.read_rate_limits(server, timeout=10, now=NOW)
    assert reply.error == "output too large"
    assert _gone(_pid(log))


def test_read_rate_limits_never_raises(tmp_path):
    assert cu.read_rate_limits(tmp_path / "no-such-codex", timeout=1).error.startswith("could not start")


@posix_only
def test_the_client_sends_nothing_the_encoder_refuses(tmp_path, monkeypatch):
    # Every line the client writes comes from the encoder, built before the
    # launch: with the read taken off the allowlist, nothing is even started.
    monkeypatch.setattr(cu, "ALLOWED_REQUESTS", frozenset({"initialize"}))
    server, log = _server(tmp_path, "ok")
    reply = cu.read_rate_limits(server, timeout=10, now=NOW)
    assert reply == cu.AppServerReply(None, None, "refused: account/rateLimits/read")
    assert not log.exists()


@posix_only
def test_read_rate_limits_puts_the_install_dirs_first_on_path(tmp_path, monkeypatch):
    dirs = [tmp_path / "brew", tmp_path / "local"]
    monkeypatch.setattr(cu, "default_codex_dirs", lambda: dirs)
    server, log = _server(tmp_path, "ok")
    cu.read_rate_limits(server, timeout=10, now=NOW, env={"PATH": "/usr/bin:/bin"})
    (path,) = [line for line in _log(log) if line.startswith("PATH ")]
    assert path == f"PATH {dirs[0]}:{dirs[1]}:/usr/bin:/bin"


@posix_only
def test_find_codex_binary(tmp_path):
    empty, plain, good = tmp_path / "empty", tmp_path / "plain", tmp_path / "good"
    for folder in (empty, plain, good):
        folder.mkdir()
    (plain / "codex").write_text("not executable")
    (good / "codex").write_text("#!/bin/sh\n")
    (good / "codex").chmod(0o755)
    assert cu.find_codex_binary([empty, plain, good]) == good / "codex"
    assert cu.find_codex_binary([empty, plain]) is None


def test_tests_never_find_the_real_codex():
    # conftest's guard: whatever this Mac has installed, no test finds it.
    assert cu.default_codex_dirs() == []
    assert cu.find_codex_binary() is None


# --- Task 3: the launch budget ------------------------------------------------------

HOUR = 3600.0


def _budget(tmp_path, name="launches.json"):
    return cu.LaunchBudget(tmp_path / name)


def test_budget_first_of_process(tmp_path):
    budget = _budget(tmp_path)
    assert budget.may_launch(NOW, manual=False, first_of_process=True)
    budget.record(NOW)
    # A restart right after a launch still gets its one launch at start.
    assert budget.may_launch(NOW + 1, manual=False, first_of_process=True)
    assert not budget.may_launch(NOW + 1, manual=False, first_of_process=False)


def test_budget_with_no_launches_logged_may_launch(tmp_path):
    assert _budget(tmp_path).may_launch(NOW, manual=False, first_of_process=False)


def test_budget_scheduled_every_six_hours(tmp_path):
    budget = _budget(tmp_path)
    budget.record(NOW)
    assert not budget.may_launch(NOW + 6 * HOUR - 1, manual=False, first_of_process=False)
    assert budget.may_launch(NOW + 6 * HOUR, manual=False, first_of_process=False)


def test_budget_manual_every_thirty_minutes(tmp_path):
    budget = _budget(tmp_path)
    budget.record(NOW)
    assert not budget.may_launch(NOW + 29 * 60, manual=True, first_of_process=False)
    assert budget.may_launch(NOW + 30 * 60, manual=True, first_of_process=False)
    assert not budget.may_launch(NOW + 30 * 60, manual=False, first_of_process=False)


def test_budget_daily_cap_eight(tmp_path):
    budget = _budget(tmp_path)
    for i in range(8):
        budget.record(NOW + i * HOUR)
    assert budget.launches_in_last_day(NOW + 7 * HOUR) == 8
    later = NOW + 7 * HOUR + 31 * 60
    for manual, first in ((True, False), (False, True), (True, True)):
        assert not budget.may_launch(later, manual=manual, first_of_process=first)  # the ninth
    # Once the first launch is a day old there is room again.
    assert budget.may_launch(NOW + 24 * HOUR, manual=True, first_of_process=False)


def test_budget_counts_a_launch_dated_ahead_as_just_made(tmp_path):
    budget = _budget(tmp_path)
    budget.record(NOW + HOUR)  # the clock was set back since
    assert budget.launches_in_last_day(NOW) == 1
    assert not budget.may_launch(NOW, manual=True, first_of_process=False)


def test_budget_persists_across_instances(tmp_path):
    _budget(tmp_path).record(NOW)
    again = _budget(tmp_path)
    assert again.launches_in_last_day(NOW + 1) == 1
    assert not again.may_launch(NOW + 1, manual=False, first_of_process=False)


def test_budget_file_keeps_a_day_only(tmp_path):
    budget = _budget(tmp_path)
    budget.record(NOW - 25 * HOUR)
    budget.record(NOW)
    saved = json.loads((tmp_path / "launches.json").read_text())
    assert saved == {"launches": [NOW]}


@pytest.mark.parametrize("text", ["{ not json", "[1, 2]", '{"launches": "x"}', '{"launches": [true, "1", null]}'])
def test_budget_unreadable_file_is_empty(tmp_path, text):
    (tmp_path / "launches.json").write_text(text)
    budget = _budget(tmp_path)
    assert budget.launches_in_last_day(NOW) == 0
    assert budget.may_launch(NOW, manual=False, first_of_process=False)
    budget.record(NOW)
    assert json.loads((tmp_path / "launches.json").read_text()) == {"launches": [NOW]}


def test_budget_holds_in_memory_when_the_file_cannot_be_written(tmp_path):
    blocked = tmp_path / "launches.json"
    blocked.mkdir()  # a folder where the file should be: every write fails
    budget = cu.LaunchBudget(blocked)
    budget.record(NOW)  # never raises
    assert budget.launches_in_last_day(NOW + 1) == 1
    assert not budget.may_launch(NOW + 60, manual=False, first_of_process=False)
    assert [p.name for p in tmp_path.iterdir()] == ["launches.json"]  # no temp file left


# --- Task 4: the monitor ------------------------------------------------------------

import logging  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402


class _Clock:
    def __init__(self, now=NOW):
        self.now = now

    def __call__(self):
        return self.now


def _reading(pct, fetched_at, source="rollout", plan="pro"):
    return cu.CodexReading((cu.CodexWindow("7d", pct, 10080, float(RESETS_AT)),), plan, fetched_at, source)


class _AppServer:
    """Answers each launch with the next reply (the last repeats); records launches."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls = []

    def __call__(self, binary):
        self.calls.append(binary)
        reply = self.replies[min(len(self.calls), len(self.replies)) - 1]
        if isinstance(reply, Exception):
            raise reply
        return reply


def _monitor(tmp_path, *, rollouts=None, app=None, binary="codex", sessions=True, clock=None, logger=None):
    sessions_dir = tmp_path / "sessions"
    if sessions:
        sessions_dir.mkdir(exist_ok=True)
    reads = []

    def read_rollouts(folder, now):
        reads.append(now)
        return rollouts(now) if callable(rollouts) else rollouts

    monitor = cu.CodexMonitor(
        sessions_dir,
        cu.LaunchBudget(tmp_path / "launches.json"),
        Path(binary) if binary else None,
        logger or logging.getLogger("claude-swap"),
        read_rollouts=read_rollouts,
        read_app_server=app or _AppServer(cu.AppServerReply(None, None, "not used")),
        clock=clock or _Clock(),
    )
    return monitor, reads


def test_monitor_absent_codex_is_not_present_and_quiet(tmp_path, caplog):
    app = _AppServer(cu.AppServerReply(_reading(5, NOW, "app-server"), 2, None))
    monitor, reads = _monitor(tmp_path, app=app, binary=None, sessions=False)
    assert monitor.state.present is False
    with caplog.at_level(logging.DEBUG, logger="claude-swap"):
        for _ in range(3):
            state = monitor.tick()
    assert state == cu.CodexState(False, None, None, None, None)
    assert app.calls == [] and reads == []
    assert caplog.records == []


def test_monitor_present_but_empty(tmp_path):
    monitor, reads = _monitor(tmp_path, rollouts=None, binary=None)
    state = monitor.tick()
    assert state.present is True and state.reading is None and state.reset_credits is None
    assert len(reads) == 1


def test_monitor_merges_newest_source(tmp_path):
    clock = _Clock()
    rollout = _reading(70.0, NOW + 30)  # measured after the app-server answered
    app = _AppServer(cu.AppServerReply(_reading(56.0, NOW, "app-server"), 2, None))
    monitor, _reads = _monitor(tmp_path, rollouts=rollout, app=app, clock=clock)
    state = monitor.tick()
    assert state.reading is rollout
    assert (state.reset_credits, state.credits_at, state.error) == (2, NOW, None)
    # And the other way round: an older rollout record loses to the reply.
    (tmp_path / "b").mkdir()
    monitor2, _ = _monitor(tmp_path / "b", rollouts=_reading(70.0, NOW - 30), app=app, clock=clock)
    assert monitor2.tick().reading.source == "app-server"


def test_monitor_keeps_the_last_reading_when_the_rollouts_have_none(tmp_path):
    clock = _Clock()
    answers = {"value": _reading(40.0, NOW)}
    monitor, _reads = _monitor(tmp_path, rollouts=lambda now: answers["value"], binary=None, clock=clock)
    first = monitor.tick().reading
    answers["value"] = None  # the five newest files hold no limits yet
    clock.now += 60
    assert monitor.tick().reading is first


def test_monitor_keeps_credits_when_reply_lacks_them(tmp_path):
    clock = _Clock()
    app = _AppServer(
        cu.AppServerReply(_reading(56.0, NOW, "app-server"), 3, None),
        cu.AppServerReply(_reading(57.0, NOW + 7 * HOUR, "app-server"), None, None),
        cu.AppServerReply(None, None, "timeout"),
    )
    monitor, _reads = _monitor(tmp_path, app=app, clock=clock)
    assert monitor.tick().reset_credits == 3
    clock.now += 7 * HOUR
    state = monitor.tick()
    assert len(app.calls) == 2
    assert (state.reset_credits, state.credits_at) == (3, NOW)  # kept, with its own time
    assert state.reading.windows[0].pct == 57.0
    clock.now += 7 * HOUR
    state = monitor.tick()
    assert (state.reset_credits, state.error) == (3, "timeout")
    assert state.reading.windows[0].pct == 57.0  # a failed launch keeps the last reading


def test_monitor_credits_known_for_a_day():
    state = cu.CodexState(True, None, 2, NOW, None)
    assert state.credits_known(NOW + 24 * HOUR)
    assert not state.credits_known(NOW + 24 * HOUR + 1)
    assert not cu.CodexState(True, None, None, None, None).credits_known(NOW)


def test_monitor_respects_budget(tmp_path):
    clock = _Clock()
    app = _AppServer(cu.AppServerReply(_reading(56.0, NOW, "app-server"), 2, None))
    monitor, reads = _monitor(tmp_path, app=app, clock=clock)
    monitor.tick()
    clock.now += 10
    monitor.tick()
    assert len(app.calls) == 1 and len(reads) == 2  # one launch, every tick reads the rollouts
    clock.now += 29 * 60
    monitor.tick(manual=True)
    assert len(app.calls) == 1  # Refresh now inside the half hour keeps the last count
    clock.now += 60
    monitor.tick(manual=True)
    assert len(app.calls) == 2
    assert cu.LaunchBudget(tmp_path / "launches.json").launches_in_last_day(clock.now) == 2


def test_monitor_errors_do_not_raise(tmp_path, caplog):
    app = _AppServer(RuntimeError("boom"))

    def broken(now):
        raise OSError("disk gone")

    monitor, _reads = _monitor(tmp_path, rollouts=broken, app=app)
    with caplog.at_level(logging.DEBUG, logger="claude-swap"):
        state = monitor.tick()
    assert state.present and state.reading is None
    assert state.error == "failed: RuntimeError"
    messages = [r.getMessage() for r in caplog.records]
    assert any("Codex rollout read failed" in m for m in messages)
    assert any("Codex app-server read failed: failed: RuntimeError" in m for m in messages)
    assert all(r.levelno == logging.DEBUG for r in caplog.records)


def _wait_idle(monitor, timeout=5.0):
    deadline = time.monotonic() + timeout
    while monitor.busy and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not monitor.busy


def test_monitor_request_runs_once_and_queues_once(tmp_path):
    monitor, _reads = _monitor(tmp_path, binary=None)
    gate, started = threading.Event(), threading.Event()
    runs, done = [], []
    real_tick = monitor.tick

    def slow_tick(manual=False):
        runs.append(manual)
        if len(runs) == 1:
            started.set()
            gate.wait(5)
        return real_tick(manual)

    monitor.tick = slow_tick
    monitor.request(on_done=lambda state: done.append(threading.current_thread() is threading.main_thread()))
    assert started.wait(5)
    monitor.request()
    monitor.request(manual=True)
    monitor.request()
    gate.set()
    _wait_idle(monitor)
    assert runs == [False, True]  # two runs in all; the queued one is manual
    assert done == [False, False]  # on_done after each run, on the worker thread


def test_monitor_request_survives_a_failing_callback(tmp_path):
    monitor, _reads = _monitor(tmp_path, binary=None)

    def bad(_state):
        raise RuntimeError("callback broke")

    monitor.request(on_done=bad)
    _wait_idle(monitor)
    monitor.request()
    _wait_idle(monitor)  # still accepts work
