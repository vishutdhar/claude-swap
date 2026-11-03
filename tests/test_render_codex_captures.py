"""The capture script writes only into the Codex captures folder.

``render_codex_captures`` is a helper, not a test, so pytest does not
collect it; these tests import it and pin where it may write. Every case
runs against a stand-in captures folder under ``tmp_path``, and every
refusal happens before anything is drawn.
"""

from __future__ import annotations

import pytest

from tests import render_codex_captures as captures


@pytest.fixture
def root(tmp_path, monkeypatch):
    """A stand-in for ~/Code/cswap-captures/codex-block, made the script's folder."""
    folder = tmp_path / "cswap-captures" / "codex-block"
    folder.mkdir(parents=True)
    monkeypatch.setattr(captures, "CAPTURES", folder, raising=False)
    return folder


def _refused(argv):
    with pytest.raises(SystemExit) as exit_info:
        captures.main(argv)
    assert exit_info.value.code not in (0, None)  # a refusal, not a clean exit


def test_capture_rejects_output_outside_codex_block(root, tmp_path):
    codex_home = tmp_path / ".codex"  # the kind of folder a typo could name
    _refused(["render", str(codex_home)])
    assert not codex_home.exists()

    escape = root / ".." / "beside"  # a path that climbs out
    _refused(["render", str(escape)])
    assert not (root.parent / "beside").exists()

    # A symlink inside the folder that points out of it: refused, and
    # nothing is made at the other end, however deep the path goes.
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "link").symlink_to(outside, target_is_directory=True)
    _refused(["render", str(root / "link")])
    _refused(["render", str(root / "link" / "deeper")])
    assert list(outside.iterdir()) == []

    # A capture file that is a symlink out of the folder is refused too,
    # before a single picture is drawn.
    target = outside / "auth.json"
    target.write_text("keep")
    (root / "dropdown-dark.png").symlink_to(target)
    _refused(["render"])
    assert target.read_text() == "keep"


def test_capture_never_creates_the_captures_folder_or_above_it(tmp_path, monkeypatch):
    missing = tmp_path / "nowhere" / "codex-block"
    monkeypatch.setattr(captures, "CAPTURES", missing, raising=False)
    _refused(["render"])
    _refused(["render", str(missing / "sub")])
    assert not (tmp_path / "nowhere").exists()


def test_capture_folder_accepts_itself_and_folders_inside_it(root):
    assert captures.output_dir(None, root) == root.resolve()
    assert captures.output_dir(str(root), root) == root.resolve()
    inside = root / "pill" / "r2"
    assert captures.output_dir(str(inside), root) == inside.resolve()
    assert not inside.exists()  # deciding where to write creates nothing


def test_claude_resets_scenes_show_each_pill_state():
    # The dropdown as the owner has it: the active account holds a grant, one
    # account has used its reset (no pill, like the account with none), one
    # holds one, one has none; then Codex.
    dropdown, highlighted = captures.SCENES["claude-resets"]
    assert highlighted is False
    got = [(p.kind, p.num, p.badge_text, p.badge_state, p.is_active) for p in dropdown()]
    assert got == [
        ("account", "1", "UR 1", "ok", True),
        ("account", "2", None, "ok", False),
        ("account", "3", "UR 1", "ok", False),
        ("account", "4", None, "ok", False),
        ("codex", "codex", "UR 2", "ok", False),
    ]
    states, _hl = captures.SCENES["claude-resets-states"]
    assert [(p.name_text, p.badge_text, p.badge_state) for p in states()] == [
        ("ok", "UR 1", "ok"), ("used", None, "ok"), ("later", "UR 1", "unknown"),
        ("paused", "UR 1", "unknown"), ("many", "UR 12", "ok"),
    ]
    panels, highlighted = captures.SCENES["claude-resets-highlighted"]
    assert highlighted is True and [p.badge_text for p in panels()] == ["UR 1"]


def test_codex_states_scene_shows_nothing_left_as_no_pill():
    states, _hl = captures.SCENES["codex-states"]
    badges = [(p.badge_text, p.badge_state) for p in states()]
    assert badges[:3] == [("UR 2", "ok"), (None, "ok"), ("UR ?", "unknown")]
    assert all(state != "over" for _text, state in badges)  # no red pill in any scene
