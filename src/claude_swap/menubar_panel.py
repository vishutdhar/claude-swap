"""Usage-bar panel for the menu bar dropdown: the model and its geometry.

The menu bar can draw each account as a small native panel (a header line and
one bar per usage window) instead of a single line of text. This module is the
pure half of that: it turns a snapshot account row into a panel *model* and
does the layout arithmetic. It imports neither rumps nor AppKit, so it is
unit-tested on any platform; ``menubar_panel_view`` draws the model with
AppKit.

Every number comes from the helpers the text row already uses (the live reset
countdown, the weekly roll-forward, ``pace.compute_pace``), and the colour
bands and the limit are the TUI's own ``WARN_PCT``/``CRIT_PCT``/``LIMIT_PCT``,
so the bars, the text row and the terminal dashboard always agree.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timezone

from claude_swap import oauth, pace
from claude_swap.json_output import USAGE_NO_CREDENTIALS, USAGE_RELOGIN_REQUIRED
from claude_swap.menubar import (
    GuardHold,
    _countdown_until,
    _live_countdown,
    _rolled_weekly_window,
    account_identity,
    format_account_label,
    guard_hold,
    out_of_rotation_word,
    sentinel_note,
)
from claude_swap.tui.data import format_duration
from claude_swap.tui.theme import CRIT_PCT, LIMIT_PCT, WARN_PCT

# ---- layout constants (points) --------------------------------------------------

PANEL_WIDTH = 340.0
LEFT_INSET = 14.0  # native menu text inset; the slot number starts here
RIGHT_INSET = 12.0
NUM_COLUMN = 12.0  # the slot number's gutter; the name and row labels start after it
NUM_GAP = 4.0  # least space between a number wider than the gutter and the name
BAR_HEIGHT = 5.0
TICK_WIDTH = 1.0
TICK_OVERHANG = 2.0  # the tick stands proud of the bar so it reads over the fill
LABEL_GAP = 10.0  # label column -> bar
PCT_GAP = 10.0  # bar -> percent
RESET_GAP = 12.0  # percent -> reset countdown
EMAIL_GAP = 8.0  # name -> email
EMAIL_MIN_W = 24.0  # an email squeezed below this is left out, not drawn as "…"
META_GAP = 10.0  # email -> the right-hand state/age text
AGE_GAP = 8.0  # between the right-hand pieces: state words, then the age
TOP_PAD = 7.0
HEADER_GAP = 4.0
BOTTOM_PAD = 9.0
CARD_INSET = 5.0  # the active account's tinted card, inset from the menu edges
CARD_RADIUS = 6.0
HAIRLINE = 0.5  # the rule under each inactive account

UNAVAILABLE = "usage unavailable"  # the text row's wording
STATE_SEP = " · "  # joins the state words ("active · held · 28m")

# A dead login: only the user can fix it (log in again, or add the account),
# so its row is urgent. The panel is handed the sentinel's display text, not
# the sentinel, so these are that text, made by the same helper the snapshot
# uses. Token expired retries on its own and a foreign credential is repaired
# by a switch; neither is here.
DEAD_LOGIN_NOTES = frozenset(sentinel_note(s) for s in (USAGE_RELOGIN_REQUIRED, USAGE_NO_CREDENTIALS))

STATES = ("ok", "warn", "crit", "over", "unknown")


# ---- model --------------------------------------------------------------------------

@dataclass(frozen=True)
class WindowRow:
    """One usage window as a bar row.

    ``fraction`` is the bar fill, clamped to 0..1. ``pace_fraction`` is where
    the pace tick goes: the share of the weekly window that had elapsed when
    the usage was measured, or None when pace does not apply (the 5h window,
    no pace data, or a window that has rolled over since it was measured).
    ``ahead`` is the text row's "(ahead)" signal for the same window. An
    ``unknown`` row has no bar; its ``note`` carries the text to show instead.
    Every other row's ``note`` is empty: an over-limit window shows as a red
    percent, and its spoken label says "over limit". ``urgent`` marks an
    unknown row whose note is a dead login (``DEAD_LOGIN_NOTES``).
    """

    label: str
    fraction: float
    pct_text: str
    reset_text: str
    state: str
    pace_fraction: float | None
    note: str
    ahead: bool = False
    urgent: bool = False


@dataclass(frozen=True)
class AccountPanel:
    """One account's block: header fields plus its window rows.

    The header shows ``name_text`` and, after it, the email (unless
    ``show_email`` is off); on the right, each of ``state_words`` and then
    ``age_text``.
    """

    num: str
    title: str  # the text row's identity: "alias  (email)" or the email
    name_text: str  # the alias, else the email's local part
    email: str
    is_active: bool
    disabled: bool  # out of rotation, paused or held: the name dims either way
    # ("active", "held · 28m"), ("paused",) or (); each word is drawn as one piece
    state_words: tuple[str, ...]
    spoken_states: tuple[str, ...]  # the same states as VoiceOver says them
    age_text: str | None  # "2m ago", or None when the reading's age is unknown
    rows: tuple[WindowRow, ...]
    text_label: str  # the plain text row, for accessibility and type-select
    alias: str | None = None  # kept so the accessibility label can name the email
    show_email: bool = True  # off where Show names only hides it in the text row

    @property
    def state_text(self) -> str:
        """The state words as one line: "active · held · 28m", "paused", or ""."""
        return STATE_SEP.join(self.state_words)

    @property
    def exhausted(self) -> bool:
        """Not the account in use, and at least one window at its limit.

        Switching to it would hit that limit at once; the view mutes it.
        """
        return not self.is_active and any(row.state == "over" for row in self.rows)


def usage_state(pct: float | None) -> str:
    """Colour band for a percentage, using the TUI's thresholds.

    "ok" (green) under ``WARN_PCT``, "warn" (yellow) from there, "crit" (red)
    from ``CRIT_PCT`` and "over" (red) at ``LIMIT_PCT``. "over" is the limit,
    not a colour edge: it alone is spoken "over limit", mutes an exhausted
    account and hides "(ahead)", so moving the red edge never changes when an
    account counts as over. While red starts at the limit, "crit" is never
    returned.
    """
    if pct is None:
        return "unknown"
    if pct >= LIMIT_PCT:
        return "over"
    if pct >= CRIT_PCT:
        return "crit"
    if pct >= WARN_PCT:
        return "warn"
    return "ok"


def _clamp01(value: float) -> float:
    return min(1.0, max(0.0, value))


def _numeric_pct(window) -> float | None:
    if isinstance(window, dict) and isinstance(window.get("pct"), (int, float)):
        return float(window["pct"])
    return None


def _row(label: str, window: dict, pct: float, now: float, pace_result=None) -> WindowRow:
    state = usage_state(pct)
    pace_fraction = None
    if pace_result is not None and pace_result.period_s > 0:
        pace_fraction = _clamp01(pace_result.elapsed_s / pace_result.period_s)
    return WindowRow(
        label=label,
        fraction=_clamp01(pct / 100.0),
        pct_text=f"{pct:.0f}%",
        reset_text=_live_countdown(window, now) or "",
        state=state,
        pace_fraction=pace_fraction,
        note="",
        ahead=bool(pace_result is not None and pace_result.ahead and state != "over"),
    )


def _weekly(window, now: float, fetched_at: float | None):
    """A weekly window rolled past a passed reset, and its pace (or None).

    A window that rolled over was measured in the cycle that has ended; its
    elapsed share belongs to that cycle, so it gets no pace at all.
    """
    rolled = _rolled_weekly_window(window, now)
    if rolled is not window:
        return rolled, None
    return rolled, pace.compute_pace(rolled, fetched_at=fetched_at)


def _unknown(note: str, urgent: bool = False) -> WindowRow:
    return WindowRow("", 0.0, "", "", "unknown", None, note, urgent=urgent)


def window_rows(usage, now: float, fetched_at: float | None) -> list[WindowRow]:
    """Bar rows for one account's display usage, in the text row's order.

    5h, 7d, each named per-model window, then spend. A sentinel string (the
    text row's note for an expired token and the like) becomes one ``unknown``
    row with that wording; no usable window at all reads "usage unavailable".
    Weekly windows are rolled forward past a passed reset and paced against
    the rolled window, exactly as ``menubar.usage_summary`` does.
    """
    if isinstance(usage, str):
        return [_unknown(usage, urgent=usage in DEAD_LOGIN_NOTES)]
    if not isinstance(usage, dict):
        return [_unknown(UNAVAILABLE)]
    rows: list[WindowRow] = []
    five = usage.get("five_hour")
    pct = _numeric_pct(five)
    if pct is not None:
        rows.append(_row("5h", five, pct, now, None))  # 5h never gets pace
    seven, seven_pace = _weekly(usage.get("seven_day"), now, fetched_at)
    pct = _numeric_pct(seven)
    if pct is not None:
        rows.append(_row("7d", seven, pct, now, seven_pace))
    for window in usage.get("scoped") or []:
        window, window_pace = _weekly(window, now, fetched_at)
        pct = _numeric_pct(window)
        if pct is not None and window.get("name"):
            rows.append(_row(str(window["name"]), window, pct, now, window_pace))
    spend = usage.get("spend")
    pct = _numeric_pct(spend)
    if pct is not None:
        rows.append(_row("$", spend, pct, now, None))
    return rows or [_unknown(UNAVAILABLE)]


def age_text(fetched_at: float | None, now: float) -> str | None:
    """How old the measurement is ("2m ago"), or None when unknown."""
    if fetched_at is None:
        return None
    return f"{format_duration(max(0.0, now - fetched_at))} ago"


def _spoken_out_of_rotation(hold: GuardHold | None, now: float) -> str:
    """The state word as VoiceOver says it: who holds the account, and until when."""
    if hold is None:
        return "paused"
    if hold.until is None or _countdown_until(hold.until, now) is None:
        return "held by the rotation guard"  # no countdown shown: no time said
    clock = oauth.reset_clock_string(
        datetime.fromtimestamp(hold.until, timezone.utc), datetime.fromtimestamp(now, timezone.utc)
    )
    return f"held by the rotation guard until {clock}"


def build_account_panel(entry: tuple, now: float, names_only: bool = False, holds=None) -> AccountPanel:
    """Panel model for one snapshot account row.

    ``entry`` is ``(num, email, is_active, display_usage, last_good, alias,
    disabled, fetched_at)`` as ``menubar._adapt_snapshot`` builds it; the
    panel reads the same display usage the text row reads. ``names_only``
    is Settings → Show names only, applied exactly as the text row applies it:
    an aliased account's email is hidden, an account without an alias keeps it.
    ``holds`` is the snapshot's read of the rotation guard's marker
    (``snap["holds"]``): a disabled account it holds is "held", any other
    disabled account "paused", as in the text row.
    """
    # Fields past the eighth are for other readers; a wider row still builds.
    num, email, is_active, display, _last_good, alias, disabled, fetched_at, *_extra = entry
    words, spoken = [], []
    if is_active:
        words.append("active")
        spoken.append("active")
    if disabled:
        hold = guard_hold(holds, num, email)
        words.append(out_of_rotation_word(hold, now))
        spoken.append(_spoken_out_of_rotation(hold, now))
    return AccountPanel(
        num=num,
        title=account_identity(email, alias, names_only=names_only),
        # account_identity's rule: an empty alias is no alias. Kept whole; the
        # view truncates by width.
        name_text=alias or email.split("@", 1)[0],
        email=email,
        is_active=bool(is_active),
        disabled=bool(disabled),
        state_words=tuple(words),
        spoken_states=tuple(spoken),
        age_text=age_text(fetched_at, now),
        rows=tuple(window_rows(display, now, fetched_at)),
        text_label=format_account_label(
            num, email, display, now, alias=alias, disabled=disabled,
            fetched_at=fetched_at, names_only=names_only, holds=holds,
        ),
        alias=alias,
        show_email=not (names_only and alias),  # account_identity's names-only rule
    )


# ---- geometry (flipped coordinates: y grows downward from the panel top) -------------

@dataclass(frozen=True)
class Columns:
    """Horizontal positions shared by every bar row in the menu."""

    label_x: float  # row labels, and the header's name
    label_w: float
    bar_x: float
    bar_w: float
    pct_right: float  # percent text is right-aligned to this
    reset_right: float  # reset countdown is right-aligned to this


def line_height(ascender: float, descender: float, leading: float) -> float:
    """Whole-point line height from a font's metrics (descender is negative)."""
    return float(math.ceil(ascender - descender + leading))


def row_columns(
    label_w: float,
    pct_w: float,
    reset_w: float,
    width: float = PANEL_WIDTH,
    num_w: float = 0.0,
) -> Columns:
    """Column layout: slot number gutter, label, bar, percent, reset countdown.

    Widths are measured by the caller from the real fonts (the widest label in
    the menu, "100%" in monospaced digits, the widest slot number, and so on),
    so every row in every account lines up. The gutter is NUM_COLUMN wide
    unless the widest number needs more.
    """
    label_x = LEFT_INSET + max(NUM_COLUMN, num_w + NUM_GAP)
    reset_right = width - RIGHT_INSET
    pct_right = reset_right - reset_w - RESET_GAP
    bar_right = pct_right - pct_w - PCT_GAP
    bar_x = label_x + label_w + LABEL_GAP
    return Columns(
        label_x=label_x,
        label_w=label_w,
        bar_x=bar_x,
        bar_w=max(0.0, bar_right - bar_x),
        pct_right=pct_right,
        reset_right=reset_right,
    )


def bar_rect(cols: Columns, row_top: float, row_height: float) -> tuple[float, float, float, float]:
    """(x, y, w, h) of a row's bar track, vertically centred in the row."""
    return (cols.bar_x, row_top + (row_height - BAR_HEIGHT) / 2, cols.bar_w, BAR_HEIGHT)


def fill_width(bar_w: float, fraction: float) -> float:
    """Width of the filled part. Any usage shows at least one round cap."""
    fraction = _clamp01(fraction)
    if fraction <= 0.0:
        return 0.0
    return min(bar_w, max(BAR_HEIGHT, fraction * bar_w))


def tick_x(cols: Columns, pace_fraction: float) -> float:
    """Left edge of the pace tick, centred on the elapsed fraction, kept in the bar."""
    centre = cols.bar_x + _clamp01(pace_fraction) * cols.bar_w
    left = centre - TICK_WIDTH / 2
    return min(max(left, cols.bar_x), cols.bar_x + cols.bar_w - TICK_WIDTH)


def tick_rect(cols: Columns, pace_fraction: float, bar_y: float) -> tuple[float, float, float, float]:
    """(x, y, w, h) of the pace tick for a bar whose top is ``bar_y``."""
    return (
        tick_x(cols, pace_fraction),
        bar_y - TICK_OVERHANG,
        TICK_WIDTH,
        BAR_HEIGHT + 2 * TICK_OVERHANG,
    )


def header_spans(cols: Columns, name_w: float, meta_w: float) -> tuple[float, float, float, float]:
    """(name_x, name_w, email_x, email_w) for the header line.

    The slot number sits in the gutter on its own. The name starts at the
    label column and keeps its measured width; the email follows and gets
    what is left before the right-hand state and age (``meta_w`` wide),
    truncated in the middle by the view. A name too long for the line is cut
    to it, and an email with less than EMAIL_MIN_W of room is left out
    (``email_w`` 0).
    """
    right = cols.reset_right - (meta_w + META_GAP if meta_w > 0 else 0.0)
    name_x = cols.label_x
    name_w = min(name_w, max(0.0, right - name_x))
    email_x = name_x + name_w + EMAIL_GAP
    email_w = right - email_x
    return name_x, name_w, email_x, (email_w if email_w >= EMAIL_MIN_W else 0.0)


def right_run(right: float, widths: list[float], gap: float) -> tuple[list[float], float]:
    """Left edges for pieces laid out leftward from ``right``, ``gap`` apart.

    ``widths`` are in reading order; the last piece ends at ``right``.
    Returns the left edges in the same order and the run's total width.
    """
    xs = []
    x = right
    for w in reversed(widths):
        x -= w
        xs.append(x)
        x -= gap
    total = right - xs[-1] if xs else 0.0
    return xs[::-1], total


def card_rect(width: float, height: float) -> tuple[float, float, float, float]:
    """(x, y, w, h) of the active account's card: inset like the menu selection."""
    return (CARD_INSET, 1.0, width - 2 * CARD_INSET, height - 2.0)


def hairline_rect(cols: Columns, height: float) -> tuple[float, float, float, float]:
    """(x, y, w, h) of the rule along the bottom of a panel, under the text columns."""
    return (cols.label_x, height - HAIRLINE, cols.reset_right - cols.label_x, HAIRLINE)


def panel_height(n_rows: int, header_h: float, row_h: float) -> float:
    """Total height of one account block, in whole points."""
    return float(math.ceil(TOP_PAD + header_h + HEADER_GAP + n_rows * row_h + BOTTOM_PAD))


def fits_in_place(shown, new) -> bool:
    """Whether an open menu showing ``shown`` rows can redraw them as ``new``.

    Each is the menu's account rows in order, as ``(account number,
    height)``; a text row's height is None. Only the same accounts in the
    same order at the same heights qualify: then every row keeps its place
    and only its drawing changes. Anything else (an account added, removed
    or moved, a panel that gained or lost a bar row) moves rows under the
    pointer, so the menu is rebuilt after it closes instead.
    """
    return [(str(num), h) for num, h in shown] == [(str(num), h) for num, h in new]


# ---- accessibility ---------------------------------------------------------------

_UNITS = {"d": "day", "h": "hour", "m": "minute", "s": "second"}
_SPOKEN_LABELS = {"5h": "5 hour", "7d": "7 day", "$": "Spend"}


def spoken_duration(text: str) -> str:
    """"4d 22h" -> "4 days 22 hours"; zero parts are dropped ("4d 0h" -> "4 days")."""
    parts = []
    for token in text.split():
        value, unit = token[:-1], _UNITS.get(token[-1:])
        if not value.isdigit() or unit is None:
            return text
        if int(value) == 0 and parts:
            continue
        parts.append(f"{int(value)} {unit}{'' if int(value) == 1 else 's'}")
    return " ".join(parts) or text


def _spoken_meta(panel: AccountPanel) -> str:
    out = list(panel.spoken_states)
    if panel.age_text:
        age = panel.age_text
        if age.endswith(" ago"):
            age = f"{spoken_duration(age[: -len(' ago')])} ago"
        out.append(age)
    return ", ".join(out)


def accessibility_label(panel: AccountPanel) -> str:
    """What VoiceOver reads for a panel: who, their state, then every window."""
    # Always the full identity, whatever the display setting: a listener
    # has no other way to tell which email an alias belongs to.
    head = f"Account {panel.num}, {account_identity(panel.email, panel.alias)}"
    meta = _spoken_meta(panel)
    if meta:
        head += f", {meta}"
    sentences = [head]
    for row in panel.rows:
        if row.state == "unknown":
            sentences.append(row.note)
            continue
        parts = [_SPOKEN_LABELS.get(row.label, row.label), f"{row.pct_text.rstrip('%')} percent"]
        if row.state == "over":
            parts.append("over limit")
        elif row.ahead:
            parts.append("ahead of pace")
        if row.reset_text:
            parts.append(f"resets in {spoken_duration(row.reset_text)}")
        sentences.append(", ".join(parts))
    return ". ".join(sentences) + "."
