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
posix_only = pytest.mark.skipif(sys.platform == "win32", reason="POSIX links, modes, locks and shell scripts")
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
def test_a_client_that_fails_after_the_launch_still_ends_the_child(tmp_path, monkeypatch):
    started = []
    real_popen = cu.subprocess.Popen

    def popen(*args, **kwargs):
        started.append(real_popen(*args, **kwargs))
        return started[-1]

    def broken_selector():
        raise RuntimeError("no selector")

    monkeypatch.setattr(cu.subprocess, "Popen", popen)
    monkeypatch.setattr(cu.selectors, "DefaultSelector", broken_selector)
    server, _log_path = _server(tmp_path, "ok")
    reply = cu.read_rate_limits(server, timeout=10, now=NOW)
    assert reply == cu.AppServerReply(None, None, "failed: RuntimeError")
    (proc,) = started
    assert proc.returncode is not None  # killed and reaped before returning


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


def test_budget_counts_a_launch_dated_a_little_ahead_as_just_made(tmp_path):
    budget = _budget(tmp_path)
    budget.record(NOW + 200)  # the clock was set back a little since
    assert budget.launches_in_last_day(NOW) == 1
    assert not budget.may_launch(NOW, manual=True, first_of_process=False)


def test_full_budget_recovers_after_clock_rollback(tmp_path):
    path = tmp_path / "launches.json"
    wrong = cu.LaunchBudget(path)
    ahead = NOW + 2 * 86400  # a clock wrongly two days fast
    for i in range(8):
        assert wrong.record(ahead + i * HOUR)
    assert not wrong.reserve(ahead + 8 * HOUR, manual=True, first_of_process=True)  # full, then
    # The clock is put right: those entries are a wrong clock's, not launches
    # to wait out, in this process and the next.
    assert wrong.launches_in_last_day(NOW) == 0
    fresh = cu.LaunchBudget(path)
    assert fresh.launches_in_last_day(NOW) == 0
    assert fresh.reserve(NOW, manual=False, first_of_process=True)
    assert _logged(path)["launches"] == [NOW]  # written back without them


def test_a_reset_count_dated_ahead_is_not_trusted(tmp_path):
    path = tmp_path / "launches.json"
    cu.LaunchBudget(path).save_credits(2, NOW + 3 * 86400)
    assert cu.LaunchBudget(path).load_credits(NOW) == (None, None)
    assert cu.LaunchBudget(path).load_credits(NOW + 3 * 86400) == (2, NOW + 3 * 86400)
    monitor, _reads = _monitor(tmp_path)  # starts on the fake clock, NOW
    assert monitor.state.reset_credits is None


def test_budget_persists_across_instances(tmp_path):
    _budget(tmp_path).record(NOW)
    again = _budget(tmp_path)
    assert again.launches_in_last_day(NOW + 1) == 1
    assert not again.may_launch(NOW + 1, manual=False, first_of_process=False)


def test_budget_file_keeps_a_day_only(tmp_path):
    budget = _budget(tmp_path)
    budget.record(NOW - 25 * HOUR)
    budget.record(NOW)
    saved = _logged(tmp_path / "launches.json")
    assert saved == {"launches": [NOW]}


def test_budget_refuses_a_launch_it_cannot_write_down(tmp_path):
    # No launch without a written reservation: a log that cannot be read or
    # written admits nothing, so no restart can start with a clean slate.
    blocked = tmp_path / "launches.json"
    blocked.mkdir()  # a folder where the file should be: it reads as no log at all
    budget = cu.LaunchBudget(blocked)
    assert budget.reserve(NOW, manual=False, first_of_process=True) is False  # never raises
    assert budget.last_failure == "launch log unreadable"
    assert budget.record(NOW) is False
    assert budget.launches_in_last_day(NOW + 1) == budget.daily_cap  # not trusted: counts as full
    assert sorted(p.name for p in tmp_path.iterdir()) == ["launches.json", "launches.json.lock"]  # no temp file


@posix_only
def test_budget_refuses_a_launch_it_cannot_write_down_in_a_read_only_folder(tmp_path):
    home = tmp_path / "backup"
    home.mkdir()
    path = home / "launches.json"
    budget = cu.LaunchBudget(path)
    assert budget.reserve(NOW, manual=False, first_of_process=True)
    home.chmod(0o500)  # the log stays readable, nothing in the folder can be replaced
    try:
        assert budget.reserve(NOW + 7 * HOUR, manual=True, first_of_process=True) is False
        assert budget.last_failure == "launch log unwritable"
        assert budget.launches_in_last_day(NOW + 7 * HOUR) == 1
    finally:
        home.chmod(0o700)


@posix_only
def test_concurrent_budget_reservations_enforce_limits(tmp_path, monkeypatch):
    import threading
    import time

    real_history = cu.LaunchBudget._history

    def slow_history(self, now):
        found = real_history(self, now)
        time.sleep(0.02)  # widen the gap between reading the log and writing it
        return found

    monkeypatch.setattr(cu.LaunchBudget, "_history", slow_history)

    def race(attempts):
        barrier, results = threading.Barrier(len(attempts)), []

        def go(budget, now, manual, first):
            barrier.wait()
            results.append(budget.reserve(now, manual=manual, first_of_process=first))

        threads = [threading.Thread(target=go, args=a) for a in attempts]
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)
        return results

    path = tmp_path / "launches.json"
    a, b = cu.LaunchBudget(path), cu.LaunchBudget(path)  # two menu bars on one file
    results = race([((a, b)[i % 2], NOW + i, True, True) for i in range(10)])
    assert results.count(True) == 8  # the daily cap, whoever asks
    assert cu.LaunchBudget(path).launches_in_last_day(NOW + 20) == 8

    spaced = tmp_path / "manual.json"
    assert cu.LaunchBudget(spaced).reserve(NOW - 31 * 60, manual=False, first_of_process=True)
    c, d = cu.LaunchBudget(spaced), cu.LaunchBudget(spaced)
    assert sorted(race([(c, NOW, True, False), (d, NOW + 30, True, False)])) == [False, True]


@posix_only
@pytest.mark.parametrize("how", ["folder-in-the-way", "read-only-folder"])
def test_unwritable_budget_survives_restart_without_extra_launches(tmp_path, how):
    home = tmp_path / "backup"
    home.mkdir()
    log = home / "launches.json"
    if how == "folder-in-the-way":
        log.mkdir()
    else:
        home.chmod(0o500)
    try:
        app = _AppServer(cu.AppServerReply(_reading(56.0, NOW, "app-server"), 2, None))
        clock = _Clock()
        states = []
        for _restart in range(12):  # twelve starts in twelve seconds
            sessions = tmp_path / "sessions"
            sessions.mkdir(exist_ok=True)
            monitor = cu.CodexMonitor(
                sessions, cu.LaunchBudget(log), Path("codex"), logging.getLogger("claude-swap"),
                read_rollouts=lambda folder, now: None, read_app_server=app, clock=clock,
            )
            states.append(monitor.tick())
            clock.now += 1
        assert app.calls == []
        expected = {"folder-in-the-way": "launch log unreadable", "read-only-folder": "launch log unwritable"}
        assert {s.error for s in states} == {expected[how]}
    finally:
        home.chmod(0o700)


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

    def __call__(self, binary, abort=None, child=None):
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


# --- the reset count survives a restart (kept in the launch budget's file) -----------

def test_budget_saves_and_loads_the_reset_count(tmp_path):
    path = tmp_path / "launches.json"
    budget = cu.LaunchBudget(path)
    assert budget.load_credits() == (None, None)
    budget.record(NOW)
    budget.save_credits(2, NOW + 5)
    assert _logged(path) == {"launches": [NOW], "reset_credits": 2, "credits_at": NOW + 5}
    again = cu.LaunchBudget(path)
    assert again.load_credits() == (2, NOW + 5)
    again.record(NOW + 7 * HOUR)  # a later launch keeps the count
    assert _logged(path) == {
        "launches": [NOW, NOW + 7 * HOUR], "reset_credits": 2, "credits_at": NOW + 5,
    }


@pytest.mark.parametrize("count, at", [(-1, NOW), ("2", NOW), (True, NOW), (10_000_000, NOW), (2, "x"), (2, None)])
def test_budget_ignores_a_bad_saved_count(tmp_path, count, at):
    path = tmp_path / "launches.json"
    path.write_text(json.dumps({"launches": [], "reset_credits": count, "credits_at": at}))
    budget = cu.LaunchBudget(path)
    assert budget.load_credits() == (None, None)
    budget.record(NOW)
    assert _logged(path) == {"launches": [NOW]}  # not carried forward either


def test_monitor_seeds_the_count_from_disk(tmp_path):
    cu.LaunchBudget(tmp_path / "launches.json").save_credits(2, NOW - 3 * HOUR)
    monitor, reads = _monitor(tmp_path)
    state = monitor.state  # before any read: what the menu shows at start
    assert (state.present, state.reading, state.reset_credits, state.credits_at) == (True, None, 2, NOW - 3 * HOUR)
    assert cu.ur_badge(state.reset_credits, state.credits_known(NOW)) == ("UR 2", "ok")
    assert reads == []


def test_monitor_stale_seed_shows_unknown(tmp_path):
    cu.LaunchBudget(tmp_path / "launches.json").save_credits(3, NOW - 25 * HOUR)
    monitor, _reads = _monitor(tmp_path, binary=None)
    state = monitor.tick()
    assert state.reset_credits == 3  # kept, only too old to show
    assert cu.ur_badge(state.reset_credits, state.credits_known(NOW)) == ("UR ?", "unknown")


def test_monitor_reply_updates_the_file(tmp_path):
    path = tmp_path / "launches.json"
    cu.LaunchBudget(path).save_credits(2, NOW - 3 * HOUR)
    clock = _Clock()
    app = _AppServer(
        cu.AppServerReply(_reading(56.0, NOW, "app-server"), 4, None),
        cu.AppServerReply(_reading(57.0, NOW + 7 * HOUR, "app-server"), None, None),
    )
    monitor, _reads = _monitor(tmp_path, app=app, clock=clock)
    assert monitor.tick().reset_credits == 4
    saved = _logged(path)
    assert (saved["reset_credits"], saved["credits_at"]) == (4, NOW)
    clock.now += 7 * HOUR
    monitor.tick()  # a reply without the count leaves the saved one alone
    saved = _logged(path)
    assert (saved["reset_credits"], saved["credits_at"]) == (4, NOW)
    assert saved["launches"] == [NOW, NOW + 7 * HOUR]
    fresh, _ = _monitor(tmp_path, app=app, clock=clock)  # a restart
    assert (fresh.state.reset_credits, fresh.state.credits_at) == (4, NOW)


# --- quitting during a read ends the child -----------------------------------------------

@posix_only
def test_quit_during_hung_read_terminates_child_group(tmp_path):
    server, log = _server(tmp_path, "hang")
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    monitor = cu.CodexMonitor(
        sessions, cu.LaunchBudget(tmp_path / "launches.json"), server, logging.getLogger("claude-swap"),
        read_rollouts=lambda folder, now: None,  # the real client, 20 s timeout
    )
    monitor.request()
    deadline = time.monotonic() + 5
    while "initialize" not in _methods(log) and time.monotonic() < deadline:
        time.sleep(0.02)
    pid = _pid(log)
    assert not _gone(pid)  # hung, mid-conversation
    started = time.monotonic()
    assert monitor.close(timeout=2.0) is True
    assert time.monotonic() - started < 2.5
    assert _gone(pid)  # its group was ended and it was reaped
    assert not monitor.busy
    monitor.request()  # closed: nothing more runs
    assert not monitor.busy
    assert cu.LaunchBudget(tmp_path / "launches.json").launches_in_last_day(time.time()) == 1


def test_close_before_any_read_launches_nothing(tmp_path):
    app = _AppServer(cu.AppServerReply(_reading(56.0, NOW, "app-server"), 2, None))
    monitor, _reads = _monitor(tmp_path, app=app)
    assert monitor.close(timeout=1.0) is True
    monitor.request()
    assert monitor.tick().reset_credits is None
    assert app.calls == []


# --- numbers too large for a float -----------------------------------------------------

HUGE = "1" + "0" * 400  # a valid JSON integer that no float can hold


def test_oversized_rollout_number_does_not_hide_valid_readings(tmp_path):
    bad_line = _line(_record(pct=1.0, stamp="2026-11-01T11:59:00Z")).replace(
        '"used_percent": 1.0', f'"used_percent": {HUGE}'
    )
    assert HUGE in bad_line
    assert cu.parse_rollout_line(bad_line) is None
    sessions = tmp_path / "sessions"
    today = _day_folder(sessions, NOW)
    good = _rollout(today, "rollout-good.jsonl", _line(_record(pct=44.0, stamp="2026-11-01T11:00:00Z")))
    bad = _rollout(today, "rollout-bad.jsonl", _line(_record(pct=12.0, stamp="2026-11-01T10:00:00Z")), bad_line)
    os.utime(good, (NOW - 600, NOW - 600))
    os.utime(bad, (NOW - 60, NOW - 60))
    assert cu.read_rollout_tail(bad).windows[0].pct == 12.0  # the line before it still counts
    assert cu.newest_rollout_reading(sessions, NOW).windows[0].pct == 44.0  # the scan survives


@pytest.mark.parametrize("field", ["window_minutes", "resets_at", "timestamp"])
def test_oversized_numbers_in_any_rollout_field(field):
    record = _record()
    if field == "timestamp":
        record["timestamp"] = 1
        text = _line(record).replace('"timestamp": 1', f'"timestamp": {HUGE}')
    else:
        record["payload"]["rate_limits"]["primary"][field] = 1
        text = _line(record).replace(f'"{field}": 1', f'"{field}": {HUGE}')
    reading = cu.parse_rollout_line(text)
    if field == "resets_at":
        assert reading.windows[0].resets_at is None  # only that field is lost
    else:
        assert reading is None


def test_oversized_numbers_in_a_reply_and_the_launch_log(tmp_path):
    reply = json.loads(
        '{"rateLimits": {"primary": {"usedPercent": %s, "windowDurationMins": 10080}},'
        ' "rateLimitResetCredits": {"availableCount": %s}}' % (HUGE, HUGE)
    )
    assert cu.parse_rate_limits_result(reply, NOW) == (None, None)
    path = tmp_path / "launches.json"
    path.write_text('{"launches": [%s, %s], "reset_credits": 2, "credits_at": %s}' % (HUGE, NOW, HUGE))
    budget = cu.LaunchBudget(path)
    assert budget.load_credits() == (None, None)
    # An entry that is not a launch: the whole log is not trusted (fail closed).
    assert budget.reserve(NOW + 7 * HOUR, manual=False, first_of_process=False) is False
    assert budget.last_failure == "launch log unreadable"


@pytest.mark.parametrize("value, expected", [
    (2, 2.0), (2.5, 2.5), (10 ** 400, None), (float("nan"), None), (float("inf"), None),
    (True, None), ("2", None), (None, None),
])
def test_finite_float(value, expected):
    assert cu.finite_float(value) == expected


# --- Show Codex off stops reads that were already asked for ----------------------------

def test_disable_codex_cancels_queued_and_not_yet_started_reads(tmp_path):
    gate, started = threading.Event(), threading.Event()

    def slow_rollouts(now):
        started.set()
        gate.wait(5)
        return None

    app = _AppServer(cu.AppServerReply(_reading(56.0, NOW, "app-server"), 2, None))
    monitor, reads = _monitor(tmp_path, rollouts=slow_rollouts, app=app)
    monitor.request()  # reading the rollouts; the launch at start would come next
    assert started.wait(5)
    monitor.request(manual=True)  # queued behind it
    monitor.enabled = False  # Show Codex off
    gate.set()
    _wait_idle(monitor)
    assert app.calls == []  # no child after the toggle
    assert len(reads) == 1  # the queued read was dropped
    assert cu.LaunchBudget(tmp_path / "launches.json").launches_in_last_day(NOW) == 0  # nor reserved
    monitor.request()
    assert not monitor.busy  # off: nothing starts
    monitor.enabled = True  # on again: the launch at start is still owed
    monitor.request()
    _wait_idle(monitor)
    assert len(app.calls) == 1


# --- Quit during the cleanup after a reply ----------------------------------------------

def _wait_for(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while not predicate() and time.monotonic() < deadline:
        time.sleep(0.02)
    return predicate()


@posix_only
def test_close_during_post_reply_cleanup_kills_at_once(tmp_path):
    server, log = _server(tmp_path, "answer-then-hang-ignore-term")
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    monitor = cu.CodexMonitor(
        sessions, cu.LaunchBudget(tmp_path / "launches.json"), server, logging.getLogger("claude-swap"),
        read_rollouts=lambda folder, now: None,
    )
    monitor.request()
    assert _wait_for(lambda: "ANSWERED" in _log(log))  # answered; the cleanup now waits on it
    pid = _pid(log)
    started = time.monotonic()
    assert monitor.close(timeout=2.0) is True
    assert time.monotonic() - started < 1.0  # no grace periods once Quit has asked
    assert _gone(pid)


def test_close_kills_the_childs_group_itself_when_the_worker_is_stuck(tmp_path, monkeypatch):
    # The worker cannot get to its own cleanup in time: close ends the
    # child's group from outside, through the group the monitor holds.
    killed = []
    monkeypatch.setattr(cu, "_signal_group", lambda pid, sig: killed.append((pid, sig)))
    monitor, _reads = _monitor(tmp_path, binary=None)
    group = cu.ChildGroup()
    monitor._child = group
    group.spawned(4242)
    monitor._running = True  # a worker that never finishes
    try:
        assert monitor.close(timeout=0.2) is False
    finally:
        monitor._running = False
    assert killed == [(4242, cu.signal.SIGKILL)]
    group.reaping()
    assert group.kill() is False  # reaped: its group id is no longer ours to signal


# --- Show Codex off while a launch is being reserved --------------------------------------

def test_disable_during_budget_reservation_prevents_spawn(tmp_path):
    from claude_swap.locking import FileLock

    # 1. Off while the worker waits for another menu bar's lock: nothing is written or started.
    app = _AppServer(cu.AppServerReply(_reading(56.0, NOW, "app-server"), 2, None))
    monitor, _reads = _monitor(tmp_path, app=app)
    waiting = threading.Event()
    real_acquire = monitor.budget._acquire

    def acquire():
        waiting.set()
        return real_acquire()

    monitor.budget._acquire = acquire
    holder = FileLock(monitor.budget.lock_path)
    assert holder.acquire()
    try:
        monitor.request()
        assert waiting.wait(5)
        monitor.enabled = False
    finally:
        holder.release()
    _wait_idle(monitor)
    assert app.calls == []
    assert cu.LaunchBudget(tmp_path / "launches.json").launches_in_last_day(NOW) == 0


@posix_only
def test_disable_right_after_the_reservation_releases_it_unused(tmp_path):
    # 2. Off between the written reservation and the launch: no child, and
    # the reservation is taken back, so it does not count.
    server, log = _server(tmp_path, "ok")
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    budget = cu.LaunchBudget(tmp_path / "launches.json")
    monitor = cu.CodexMonitor(sessions, budget, server, logging.getLogger("claude-swap"),
                              read_rollouts=lambda folder, now: None)
    real_reserve = budget.reserve

    def reserve(*args, **kwargs):
        taken = real_reserve(*args, **kwargs)
        monitor.enabled = False  # the toggle lands just after
        return taken

    budget.reserve = reserve
    monitor.tick()
    assert not log.exists()  # never launched
    assert cu.LaunchBudget(tmp_path / "launches.json").launches_in_last_day(time.time()) == 0


@posix_only
def test_disable_during_a_read_ends_the_child(tmp_path):
    # 3. Off with the child already running: it is ended at once (it counts:
    # it was launched).
    server, log = _server(tmp_path, "hang")
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    monitor = cu.CodexMonitor(sessions, cu.LaunchBudget(tmp_path / "launches.json"), server,
                              logging.getLogger("claude-swap"), read_rollouts=lambda folder, now: None)
    monitor.request()
    assert _wait_for(lambda: "initialize" in _methods(log))
    pid = _pid(log)
    monitor.enabled = False
    _wait_idle(monitor, timeout=2.0)
    assert _gone(pid)
    assert monitor.state.error == "aborted"
    monitor.enabled = True  # on again: reads work as before
    assert not monitor._abort.is_set()


# --- the time is read inside the lock ---------------------------------------------------

def test_forward_clock_jump_during_scan_preserves_newer_log_entries(tmp_path):
    path = tmp_path / "launches.json"
    clock = _Clock()
    app = _AppServer(cu.AppServerReply(_reading(56.0, NOW, "app-server"), 3, None))
    monitor, _reads = _monitor(tmp_path, app=app, clock=clock)  # its log is `path`
    acquired = []
    real_acquire = monitor.budget._acquire

    def acquire():
        # Before each lock this worker takes, the clock jumps ten minutes
        # ahead and another menu bar, on that clock, writes first.
        acquired.append(1)
        clock.now += 600
        other = cu.LaunchBudget(path)
        assert other.record(clock.now - 1)
        if len(acquired) == 1:
            other.save_credits(5, clock.now - 1)
        return real_acquire()

    monitor.budget._acquire = acquire
    monitor.tick()  # took its time at NOW, before both jumps
    assert len(acquired) == 2 and len(app.calls) == 1  # the reservation, then the count
    saved = _logged(path)
    assert saved["launches"] == [NOW + 599, NOW + 600, NOW + 1199]  # the other's two kept
    assert (saved["reset_credits"], saved["credits_at"]) == (3, NOW + 600)  # the newer count wins


# --- launches at the same second are still separate launches ----------------------------

def test_equal_timestamp_reservations_count_as_distinct_launches(tmp_path):
    path = tmp_path / "launches.json"
    a, b = cu.LaunchBudget(path), cu.LaunchBudget(path)
    results, tokens = [], []
    for i in range(10):
        budget = (a, b)[i % 2]
        results.append(budget.reserve(NOW, manual=True, first_of_process=True))
        tokens.append(budget.last_reservation)
    assert results.count(True) == 8
    assert len({t for t in tokens if t}) == 8  # each launch its own token
    assert _logged(path)["launches"] == [NOW] * 8
    assert cu.LaunchBudget(path).launches_in_last_day(NOW) == 8
    assert a.launches_in_last_day(NOW) == 8  # counted from the log alone
    a.release(tokens[0], now=NOW)  # one unused reservation taken back: one entry, not all
    assert _logged(path)["launches"] == [NOW] * 7


# --- the log is the only record of launches (no copy kept in memory) ----------------------

def test_trimmed_future_launches_never_reappear_from_memory(tmp_path):
    path = tmp_path / "launches.json"
    fast = cu.LaunchBudget(path)
    ahead = NOW + 2 * 86400  # its clock two days fast
    for i in range(8):
        assert fast.record(ahead + i * HOUR)
    right = cu.LaunchBudget(path)  # another menu bar, its clock right, trims them
    assert right.reserve(NOW, manual=False, first_of_process=True)
    later = ahead + 8 * HOUR  # those dates come round: they were trimmed, they stay gone
    assert fast.launches_in_last_day(later) == cu.LaunchBudget(path).launches_in_last_day(later) == 0
    assert fast.reserve(later, manual=False, first_of_process=True)


def test_a_trimmed_count_never_reappears_from_memory(tmp_path):
    path = tmp_path / "launches.json"
    fast = cu.LaunchBudget(path)
    fast.save_credits(4, NOW + 2 * 86400)  # reported under a clock two days fast
    right = cu.LaunchBudget(path)
    assert right.reserve(NOW, manual=False, first_of_process=True)  # writes the log without it
    assert "reset_credits" not in json.loads(path.read_text())
    assert fast.reserve(NOW + 2 * 86400, manual=False, first_of_process=True)
    assert "reset_credits" not in json.loads(path.read_text())  # not written back from memory


# --- each reservation is its owner's: released by its token, not its time --------------

def _logged(path):
    """The log with each launch reduced to its time (entries are {"t": time, "id": token})."""
    data = json.loads(path.read_text())
    return {**data, "launches": [entry["t"] for entry in data["launches"]]}


def test_release_after_clock_trim_preserves_other_process_same_timestamp(tmp_path):
    path = tmp_path / "launches.json"
    a, b = cu.LaunchBudget(path), cu.LaunchBudget(path)
    assert a.reserve(NOW, manual=False, first_of_process=True)
    unused = a.last_reservation  # taken; its child never starts
    assert b.reserve(NOW + 2 * 86400, manual=False, first_of_process=True)  # b two days fast: a's entry trimmed
    assert b.reserve(NOW, manual=False, first_of_process=True)  # b put right: it launches in the same second
    a.release(unused, now=NOW)  # a takes back its own reservation, which is no longer there
    assert _logged(path)["launches"] == [NOW]  # b's launch still counts
    assert cu.LaunchBudget(path).launches_in_last_day(NOW) == 1


def test_legacy_bare_entries_count_but_are_never_released(tmp_path):
    path = tmp_path / "launches.json"
    path.write_text(json.dumps({"launches": [NOW, NOW]}))  # the log as earlier builds wrote it
    budget = cu.LaunchBudget(path)
    assert budget.launches_in_last_day(NOW) == 2
    budget.release(None, now=NOW)
    assert budget.launches_in_last_day(NOW) == 2
    assert budget.reserve(NOW + 7 * HOUR, manual=False, first_of_process=False)
    entries = json.loads(path.read_text())["launches"]
    assert [e["id"] for e in entries[:2]] == [None, None] and isinstance(entries[2]["id"], str)
    assert entries[0] != entries[2]["id"]


def test_prespawn_abort_preserves_startup_allowance(tmp_path):
    path = tmp_path / "launches.json"
    assert cu.LaunchBudget(path).record(NOW - 60)  # another menu bar launched a minute ago
    calls = []

    def app(binary, abort=None, child=None):
        calls.append(binary)
        if len(calls) == 1:
            monitor.enabled = False  # off between the written reservation and the spawn
            return cu.AppServerReply(None, None, "aborted")  # the client's check: no child started
        return cu.AppServerReply(_reading(56.0, NOW, "app-server"), 2, None)

    monitor, _reads = _monitor(tmp_path, app=app)
    monitor.tick()
    assert cu.LaunchBudget(path).launches_in_last_day(NOW) == 1  # taken back
    monitor.enabled = True
    state = monitor.tick()  # the read at start is still owed, whatever the six hour cadence says
    assert len(calls) == 2 and state.reset_credits == 2


def test_live_credits_become_unknown_after_clock_rollback(tmp_path):
    fast = NOW + 2 * 86400
    state = cu.CodexState(True, None, 2, fast, None)  # reported under a clock two days fast
    assert not state.credits_known(NOW)  # the clock put right: a wrong clock's count
    assert state.credits_known(fast + 60)
    assert cu.CodexState(True, None, 2, NOW + 200, None).credits_known(NOW)  # a little ahead is fine
    # Live, in a running monitor: no restart, no new count since.
    clock = _Clock(fast)
    app = _AppServer(
        cu.AppServerReply(_reading(56.0, fast, "app-server"), 2, None),
        cu.AppServerReply(_reading(57.0, NOW + 60, "app-server"), None, None),  # no count this time
    )
    monitor, _reads = _monitor(tmp_path, app=app, clock=clock)
    assert monitor.tick().credits_known(clock.now)
    clock.now = NOW + 60
    state = monitor.tick()
    assert state.reset_credits == 2 and not state.credits_known(clock.now)
    assert cu.ur_badge(state.reset_credits, state.credits_known(clock.now)) == ("UR ?", "unknown")


def test_one_rule_for_dated_ahead():
    assert cu.dated_ahead(NOW + cu.FUTURE_SLACK_S + 1, NOW)
    assert not cu.dated_ahead(NOW + cu.FUTURE_SLACK_S, NOW)
    assert not cu.dated_ahead(NOW - 86400, NOW)



# --- what the server started dies with it ----------------------------------------------

@posix_only
def test_cleanup_kills_descendants_after_leader_exits(tmp_path):
    server, log = _server(tmp_path, "spawn-descendant")
    reply = cu.read_rate_limits(server, timeout=10, now=NOW)
    assert reply.error is None and reply.reset_credits == 2
    (descendant,) = [int(line.split()[1]) for line in _log(log) if line.startswith("DESCENDANT ")]
    assert _gone(_pid(log))  # the server left on its own when its input closed
    assert _wait_for(lambda: _gone(descendant), 3.0)  # and what it started did not outlive it



# --- a log that cannot be trusted admits nothing ------------------------------------------

CORRUPT_LOGS = [
    '{"launches": [{"t": 1, "id": "x"}',  # cut off mid-write
    "{ not json",
    "[1, 2]",
    '{"launches": "x"}',
    '{"launches": [true]}',
    '{"launches": [{"t": "soon", "id": "x"}]}',
    "{}",
]


def test_corrupt_or_deleted_launch_history_fails_closed(tmp_path):
    path = tmp_path / "launches.json"
    budget = cu.LaunchBudget(path)
    assert budget.reserve(NOW, manual=False, first_of_process=True)  # first run: no log yet
    token = budget.last_reservation  # a real reservation's token, kept for the releases below
    for text in CORRUPT_LOGS:
        path.write_text(text)
        assert budget.reserve(NOW + 7 * HOUR, manual=True, first_of_process=True) is False, text
        assert budget.last_failure == "launch log unreadable"
        assert budget.record(NOW + 7 * HOUR) is False
        budget.save_credits(2, NOW + 7 * HOUR)
        budget.release(token, now=NOW + 7 * HOUR)
        assert path.read_text() == text  # never written over with an emptied history
    path.unlink()  # deleted while this process runs: lost, not a first run
    assert budget.reserve(NOW + 7 * HOUR, manual=True, first_of_process=True) is False
    assert budget.last_failure == "launch log missing"
    assert not path.exists()
    assert cu.LaunchBudget(path).reserve(NOW + 7 * HOUR, manual=False, first_of_process=True)  # a new process


def test_a_corrupt_log_is_reported_once_and_launches_nothing(tmp_path, caplog):
    (tmp_path / "launches.json").write_text("{ not json")
    app = _AppServer(cu.AppServerReply(_reading(56.0, NOW, "app-server"), 2, None))
    monitor, _reads = _monitor(tmp_path, app=app)
    with caplog.at_level(logging.DEBUG, logger="claude-swap"):
        states = [monitor.tick(manual=True) for _ in range(3)]
    assert app.calls == []
    assert {s.error for s in states} == {"launch log unreadable"}
    assert sum("launch log unreadable" in r.getMessage() for r in caplog.records) == 1


def test_disappearing_log_directory_fails_closed(tmp_path):
    home = tmp_path / "backup"
    home.mkdir()
    path = home / "launches.json"
    budget = cu.LaunchBudget(path)
    assert budget.reserve(NOW, manual=False, first_of_process=True)
    path.unlink()
    budget.lock_path.unlink()
    home.rmdir()  # the whole folder goes
    assert budget.reserve(NOW + 7 * HOUR, manual=True, first_of_process=True) is False
    assert budget.last_failure == "launch log missing"
    budget.save_credits(2, NOW + 7 * HOUR)
    assert budget.record(NOW + 7 * HOUR) is False
    assert not home.exists()  # never made again by the budget
    assert cu.LaunchBudget(path).reserve(NOW + 7 * HOUR, manual=False, first_of_process=True) is False
    assert not home.exists()


# --- a lock on a sidecar that is no longer at its path guards nothing ---------------------

@posix_only
def test_unlinked_held_sidecar_cannot_admit_concurrent_reservations(tmp_path):
    path = tmp_path / "launches.json"
    seed = cu.LaunchBudget(path)
    for i in range(7):
        assert seed.record(NOW - (7 - i) * 60)  # oldest first, as a clock records them
    assert seed.launches_in_last_day(NOW) == 7
    a, b = cu.LaunchBudget(path), cu.LaunchBudget(path)
    real_allows = a._allows
    taken = []

    def allows_while_the_sidecar_is_replaced(history, now, manual, first_of_process):
        os.unlink(a.lock_path)  # someone deletes the sidecar a is holding
        assert b.reserve(NOW, manual=True, first_of_process=True)  # b locks a new file there, takes the eighth
        taken.append(b.last_reservation)
        return real_allows(history, now, manual, first_of_process)  # a had read seven

    a._allows = allows_while_the_sidecar_is_replaced
    assert a.reserve(NOW, manual=True, first_of_process=True) is False  # its lock no longer guards the log
    assert a.last_failure == "launch log busy"
    entries = json.loads(path.read_text())["launches"]
    assert len(entries) == 8 and taken[0] in [e["id"] for e in entries]  # eight launched, eight written


@posix_only
def test_a_lock_on_a_replaced_sidecar_is_retried_then_refused(tmp_path, monkeypatch):
    from claude_swap.locking import FileLock

    real_acquire = FileLock.acquire
    attempts = []

    def acquire_then_lose_the_path(self, timeout=None):
        got = real_acquire(self, timeout)
        attempts.append(1)
        os.unlink(self.lock_path)  # deleted between the open and the check
        return got

    monkeypatch.setattr(FileLock, "acquire", acquire_then_lose_the_path)
    budget = cu.LaunchBudget(tmp_path / "launches.json")
    assert budget.reserve(NOW, manual=False, first_of_process=True) is False
    assert budget.last_failure == "launch log busy"
    assert len(attempts) == cu.LOCK_ATTEMPTS == 5
    assert not (tmp_path / "launches.json").exists()


def test_duplicate_token_release_never_removes_multiple_entries(tmp_path):
    path = tmp_path / "launches.json"
    copied = [{"t": NOW - 30, "id": "abc"}] * 3 + [{"t": NOW - 20, "id": "x1"}, {"t": NOW - 10, "id": "x2"}]
    path.write_text(json.dumps({"launches": copied}))  # a log copied with a token repeated
    budget = cu.LaunchBudget(path)
    budget.release("abc", now=NOW)
    assert _logged(path)["launches"] == [NOW - 30, NOW - 30, NOW - 20, NOW - 10]  # one taken back
    assert budget.launches_in_last_day(NOW) == 4


def test_save_credits_orders_ties_and_future_counts(tmp_path):
    def log(name):
        path = tmp_path / name
        budget = cu.LaunchBudget(path)
        assert budget.record(NOW - 60)
        return path, budget

    path, budget = log("newer.json")  # a newer count in the log survives an older reply
    budget.save_credits(5, NOW + 100, now=NOW + 100)
    budget.save_credits(3, NOW, now=NOW + 100)
    assert cu.LaunchBudget(path).load_credits(NOW + 100) == (5, NOW + 100)

    path, budget = log("tie.json")  # the same time: the reply wins
    budget.save_credits(5, NOW, now=NOW)
    budget.save_credits(3, NOW, now=NOW)
    assert cu.LaunchBudget(path).load_credits(NOW) == (3, NOW)

    path, budget = log("ahead.json")  # dated 300 s ahead: kept
    budget.save_credits(2, NOW + 300, now=NOW)
    assert cu.LaunchBudget(path).load_credits(NOW) == (2, NOW + 300)

    path, budget = log("too-far.json")  # 301 s ahead: a wrong clock's, not written
    budget.save_credits(2, NOW + 301, now=NOW)
    assert "reset_credits" not in json.loads(path.read_text())


def test_repeated_aborts_and_queued_requests_allow_one_startup_launch(tmp_path):
    path = tmp_path / "launches.json"
    assert cu.LaunchBudget(path).record(NOW - 60)  # another menu bar launched a minute ago
    calls = []

    def app(binary, abort=None, child=None):
        calls.append(1)
        if len(calls) <= 3:
            monitor.enabled = False  # the toggle lands before the spawn, again and again
            return cu.AppServerReply(None, None, "aborted")
        return cu.AppServerReply(_reading(56.0, NOW, "app-server"), 2, None)

    monitor, _reads = _monitor(tmp_path, app=app)
    for _ in range(3):
        monitor.enabled = True
        monitor.request()
        monitor.request(manual=True)  # queued behind it, dropped with the toggle
        monitor.request()
        _wait_idle(monitor)
    assert len(calls) == 3
    monitor.enabled = True
    for _ in range(3):
        monitor.request(manual=True)
        _wait_idle(monitor)
    assert len(calls) == 4  # one start launch; then Refresh now waits its half hour
    assert cu.LaunchBudget(path).launches_in_last_day(NOW) == 2  # the other's and this one's


def test_later_abort_does_not_restore_consumed_startup_allowance(tmp_path):
    clock = _Clock()
    calls = []

    def app(binary, abort=None, child=None):
        calls.append(1)
        if len(calls) == 2:
            monitor.enabled = False
            return cu.AppServerReply(None, None, "aborted")
        return cu.AppServerReply(_reading(56.0, clock.now, "app-server"), 2, None)

    monitor, _reads = _monitor(tmp_path, app=app, clock=clock)
    monitor.tick()  # the start launch: the allowance is used
    clock.now += 31 * 60
    monitor.tick(manual=True)  # Refresh now half an hour on, stopped before its spawn
    assert len(calls) == 2
    monitor.enabled = True
    monitor.tick()  # an ordinary tick, within six hours of the start launch
    assert len(calls) == 2  # the allowance used at start does not come back


def test_a_stale_corrupt_log_self_heals(tmp_path):
    stale = tmp_path / "launches.json"
    stale.write_text("{ not json")
    os.utime(stale, (NOW - 25 * HOUR, NOW - 25 * HOUR))  # last written over a day ago
    budget = cu.LaunchBudget(stale)
    assert budget.launches_in_last_day(NOW) == 0  # nothing in it can be inside the day
    assert budget.reserve(NOW, manual=False, first_of_process=True)  # one launch
    assert _logged(stale)["launches"] == [NOW]  # written over as a valid log
    assert not budget.reserve(NOW + 60, manual=False, first_of_process=False)  # then the rules as ever

    young = tmp_path / "young.json"
    young.write_text("{ not json")
    os.utime(young, (NOW - HOUR, NOW - HOUR))  # could hold this day's launches
    other = cu.LaunchBudget(young)
    assert other.reserve(NOW, manual=False, first_of_process=True) is False
    assert other.last_failure == "launch log unreadable"
    assert young.read_text() == "{ not json"  # untouched

    folder = tmp_path / "folder.json"
    folder.mkdir()  # not a file at all: never written over, however old
    os.utime(folder, (NOW - 25 * HOUR, NOW - 25 * HOUR))
    assert cu.LaunchBudget(folder).reserve(NOW, manual=False, first_of_process=True) is False


# --- any read of the log is first contact -------------------------------------------------

def _eight_launch_log(path):
    entries = [{"t": NOW - (8 - i) * 60, "id": f"t{i}"} for i in range(8)]
    path.write_text(json.dumps({"launches": entries, "reset_credits": 2, "credits_at": NOW - 600}))


def test_delete_after_startup_credit_load_fails_closed(tmp_path):
    path = tmp_path / "launches.json"
    _eight_launch_log(path)
    budget = cu.LaunchBudget(path)
    assert budget.load_credits(NOW) == (2, NOW - 600)  # the read at start
    path.unlink()  # gone before the first reservation
    assert budget.reserve(NOW, manual=False, first_of_process=True) is False  # not a fresh install
    assert budget.last_failure == "launch log missing"
    assert not path.exists()
    # The same through a monitor, which reads the count when it is made.
    _eight_launch_log(path)
    app = _AppServer(cu.AppServerReply(_reading(56.0, NOW, "app-server"), 2, None))
    monitor, _reads = _monitor(tmp_path, app=app)
    path.unlink()
    assert monitor.tick().error == "launch log missing"
    assert app.calls == [] and not path.exists()


def test_delete_after_initial_corrupt_read_fails_closed(tmp_path):
    path = tmp_path / "launches.json"
    path.write_text("{ not json")  # written just now: not stale
    now = time.time()
    budget = cu.LaunchBudget(path)
    assert budget.reserve(now, manual=False, first_of_process=True) is False
    assert budget.last_failure == "launch log unreadable"
    path.unlink()
    assert budget.reserve(now, manual=False, first_of_process=True) is False
    assert budget.last_failure == "launch log missing"
    assert not path.exists()



def test_release_with_real_token_preserves_corrupt_history(tmp_path):
    path = tmp_path / "launches.json"
    budget = cu.LaunchBudget(path)
    assert budget.reserve(NOW, manual=False, first_of_process=True)
    token = budget.last_reservation  # kept: the failing reserve below clears last_reservation
    data = json.loads(path.read_text())
    data["launches"].append(True)  # the token's entry is still there, beside one that is not a launch
    text = json.dumps(data)
    path.write_text(text)
    assert budget.reserve(NOW + 60, manual=True, first_of_process=True) is False
    assert budget.last_reservation is None
    budget.release(token, now=NOW + 60)
    assert path.read_text() == text  # a log that cannot be trusted is never written over


# --- where the self-heal stops ---------------------------------------------------------

def _set_mtime(path, when):
    os.utime(path, ns=(int(when) * 10**9, int(when) * 10**9))


def test_self_heal_mtime_boundaries(tmp_path):
    def corrupt(name, mtime):
        path = tmp_path / name
        path.write_text("{ not json")
        _set_mtime(path, mtime)
        return path

    exact = corrupt("exact.json", NOW - 86400)  # exactly a day: could still hold a launch
    assert cu.LaunchBudget(exact).reserve(NOW, manual=False, first_of_process=True) is False
    assert exact.read_text() == "{ not json"
    past = corrupt("past.json", NOW - 86401)  # a second more: cannot
    assert cu.LaunchBudget(past).reserve(NOW, manual=False, first_of_process=True) is True
    ahead = corrupt("ahead.json", NOW + 25 * HOUR)  # dated ahead of the clock: never stale
    assert cu.LaunchBudget(ahead).reserve(NOW, manual=False, first_of_process=True) is False
    assert ahead.read_text() == "{ not json"


def test_valid_history_bypasses_self_heal(tmp_path):
    full = tmp_path / "full.json"  # valid, eight launches this day, but touched back a day
    entries = [{"t": NOW - (8 - i) * 60, "id": f"t{i}"} for i in range(8)]
    full.write_text(json.dumps({"launches": entries}))
    _set_mtime(full, NOW - 25 * HOUR)
    text = full.read_text()
    budget = cu.LaunchBudget(full)
    assert budget.launches_in_last_day(NOW) == 8  # its launches count: it is a valid log
    assert budget.reserve(NOW, manual=True, first_of_process=True) is False
    assert budget.last_failure is None  # refused by the cap, not as a broken log
    assert full.read_text() == text

    empty = tmp_path / "empty.json"  # valid and empty, touched back a day
    empty.write_text(json.dumps({"launches": [], "reset_credits": 3, "credits_at": NOW - 100}))
    _set_mtime(empty, NOW - 25 * HOUR)
    text = empty.read_text()
    budget = cu.LaunchBudget(empty)
    assert budget.launches_in_last_day(NOW) == 0
    assert budget.load_credits(NOW) == (3, NOW - 100)
    assert empty.read_text() == text  # reading never writes
    assert budget.reserve(NOW, manual=False, first_of_process=True)
    assert _logged(empty) == {"launches": [NOW], "reset_credits": 3, "credits_at": NOW - 100}


def test_mixed_legacy_corruption_fails_closed(tmp_path):
    content = json.dumps({"launches": [NOW - 120, NOW - 60, "x"]})  # two old-style entries, one broken
    young = tmp_path / "young.json"
    young.write_text(content)
    _set_mtime(young, NOW - HOUR)
    budget = cu.LaunchBudget(young)
    assert budget.reserve(NOW, manual=False, first_of_process=True) is False
    assert budget.last_failure == "launch log unreadable"
    assert budget.launches_in_last_day(NOW) == budget.daily_cap
    assert young.read_text() == content
    stale = tmp_path / "stale.json"
    stale.write_text(content)
    _set_mtime(stale, NOW - 25 * HOUR)
    assert cu.LaunchBudget(stale).reserve(NOW, manual=False, first_of_process=True)  # heals only when stale
    assert _logged(stale)["launches"] == [NOW]


# --- what the rollout reader actually reads ------------------------------------------------

class _OsSpy:
    """``os`` for codex_usage, counting the bytes read and, once, running ``after_lstat``
    between the reader's lstat and its open."""

    def __init__(self, after_lstat=None):
        self.after_lstat = after_lstat
        self.bytes_read = 0

    def __getattr__(self, name):
        return getattr(os, name)

    def lstat(self, path, *args, **kwargs):
        found = os.lstat(path, *args, **kwargs)
        hook, self.after_lstat = self.after_lstat, None
        if hook is not None:
            hook()
        return found

    def read(self, fd, n):
        data = os.read(fd, n)
        self.bytes_read += len(data)
        return data


def test_rollout_tail_caps_actual_bytes_read(tmp_path, monkeypatch):
    filler = _line({"timestamp": "2026-11-01T11:00:00Z", "payload": {"type": "agent_message", "text": "x" * 1000}})
    lines = [filler] * (10 * 1024 * 1024 // (len(filler) + 1) + 2) + [_line(_record(pct=61.0))]
    path = _rollout(tmp_path, "rollout-big.jsonl", *lines)
    assert path.stat().st_size > 10 * 1024 * 1024
    spy = _OsSpy()
    monkeypatch.setattr(cu, "os", spy)
    reading = cu.read_rollout_tail(path, max_bytes=cu.ROLLOUT_TAIL_BYTES)
    assert reading.windows[0].pct == 61.0
    assert 0 < spy.bytes_read <= cu.ROLLOUT_TAIL_BYTES  # the tail only, never the file


@posix_only
@pytest.mark.parametrize("swap", ["other-file", "hard-link", "fifo"])
def test_rollout_tail_refuses_file_changed_between_lstat_and_open(tmp_path, monkeypatch, swap):
    path = _rollout(tmp_path, "rollout-a.jsonl", _line(_record(pct=12.0)))
    other = _rollout(tmp_path / "elsewhere", "rollout-b.jsonl", _line(_record(pct=99.0)))

    def substitute():
        if swap == "other-file":
            os.replace(other, path)  # another file now has the name
        elif swap == "hard-link":
            os.link(path, tmp_path / "second-name.jsonl")  # the same file, now with two names
        else:
            os.unlink(path)
            os.mkfifo(path)  # opened as it is, a pipe would block the reader forever

    spy = _OsSpy(after_lstat=substitute)
    monkeypatch.setattr(cu, "os", spy)
    result = []
    reader = threading.Thread(target=lambda: result.append(cu.read_rollout_tail(path)), daemon=True)
    reader.start()
    reader.join(5)  # a reader stuck in open() fails here, in bounded time, instead of hanging the run
    if reader.is_alive():
        if swap == "fifo":
            try:  # a writer's open lets the stuck reader's open return
                os.close(os.open(path, os.O_WRONLY | os.O_NONBLOCK))
            except OSError:
                pass
        pytest.fail(f"the reader blocked opening the substituted {swap}")
    assert result == [None]
    assert spy.bytes_read == 0  # refused before a byte was read


# --- no os.waitid: macOS before Python 3.13 has none -----------------------------------

@posix_only
@pytest.mark.parametrize("mode", ["ok", "hang", "spawn-descendant", "answer-then-hang-ignore-term"])
def test_the_client_works_without_waitid(tmp_path, monkeypatch, mode):
    monkeypatch.delattr(os, "waitid", raising=False)
    server, log = _server(tmp_path, mode)
    started = time.monotonic()
    reply = cu.read_rate_limits(server, timeout=2, now=NOW)
    if mode == "hang":
        assert reply == cu.AppServerReply(None, None, "timeout")
    else:
        assert reply.error is None and reply.reset_credits == 2  # the reply survives the cleanup
    assert time.monotonic() - started < 8
    assert _gone(_pid(log))  # killed if need be, and reaped
    descendants = [int(line.split()[1]) for line in _log(log) if line.startswith("DESCENDANT ")]
    assert all(_wait_for(lambda pid=pid: _gone(pid), 3.0) for pid in descendants)
