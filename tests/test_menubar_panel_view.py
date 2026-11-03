"""AppKit layer of the menu bar usage bars (macOS with the menubar extra only).

The drawing itself is checked by eye (see the PR); these tests pin what can
be pinned without a screen: sizes, that both appearances render, that a
click, Return or an accessibility press reaches the stable account target,
that a failure anywhere leaves plain text rows, and that rebuilding the menu
does not leak views or rumps callback entries.
"""

from __future__ import annotations

import gc
import logging
import sys
import threading
import time

import pytest

pytest.importorskip("AppKit")
rumps = pytest.importorskip("rumps")

import AppKit  # noqa: E402
import objc  # noqa: E402

from claude_swap import menubar, menubar_panel as mp  # noqa: E402
from claude_swap import menubar_panel_view as pv  # noqa: E402

pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="AppKit drawing")

_NOW = 1_000_000.0


def _panels():
    usages = [
        {"five_hour": {"pct": 46.0}, "seven_day": {"pct": 73.0},
         "scoped": [{"name": "Fable", "pct": 85.0}]},
        {"five_hour": {"pct": 100.0}},
        "token expired",
    ]
    entries = [
        (str(i + 1), f"user{i}@example.com", i == 0, u, u if isinstance(u, dict) else None,
         None, False, _NOW - 60)
        for i, u in enumerate(usages)
    ]
    return [mp.build_account_panel(e, _NOW) for e in entries]


def _items(panels, active_first=True):
    items = [rumps.MenuItem(p.text_label, callback=lambda _s: None) for p in panels]
    if active_first:
        items[0].state = 1
    return items


def _text_state(items):
    return [(i._menuitem.view(), i._menuitem.title(), i._menuitem.state()) for i in items]


@pytest.fixture
def run_now(monkeypatch):
    monkeypatch.setattr(pv.AppHelper, "callAfter", lambda fn, *a, **k: fn(*a, **k))


class _Menu:
    """A real NSMenu whose panels are attached, plus the open flag the app keeps."""

    def __init__(self, panels, handler):
        self.open = True
        self.menu = AppKit.NSMenu.alloc().init()
        self.target = pv.make_account_target(handler, is_open=lambda: self.open, menu=self.menu)
        layout = pv.PanelLayout(panels)
        self.items, self.views = [], []
        for panel in panels:
            item = AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(panel.text_label, None, "")
            view = pv.make_panel_view(panel, layout, target=self.target)
            item.setView_(view)
            self.menu.addItem_(item)
            self.items.append(item)
            self.views.append(view)


@pytest.fixture
def switched():
    return []


@pytest.fixture
def on_screen(monkeypatch):
    # A view only has a window while AppKit displays the menu, which no unit
    # test can do; the live pass checks the real thing. Everything else in
    # the attachment check (item still in the app's current menu) is real.
    monkeypatch.setattr(pv, "_on_screen", lambda view: True)


@pytest.fixture
def target(switched):
    return pv.make_account_target(switched.append)


def _mouse_up(view, inside=True):
    b = view.bounds()
    # Window coordinates for a windowless view: y is measured from the bottom.
    x = b.size.width / 2 if inside else b.size.width + 40.0
    y = b.size.height / 2
    return AppKit.NSEvent.mouseEventWithType_location_modifierFlags_timestamp_windowNumber_context_eventNumber_clickCount_pressure_(
        AppKit.NSEventTypeLeftMouseUp, (x, y), 0, 0, 0, None, 0, 1, 0.0
    )


def _render(view):
    rep = view.bitmapImageRepForCachingDisplayInRect_(view.bounds())
    view.cacheDisplayInRect_toBitmapImageRep_(view.bounds(), rep)
    return rep


# --- layout and rendering ------------------------------------------------------------

def test_layout_uses_the_fixed_width_and_model_heights():
    panels = _panels()
    layout = pv.PanelLayout(panels)
    assert layout.width == mp.PANEL_WIDTH
    for panel in panels:
        view = pv.make_panel_view(panel, layout)
        frame = view.frame()
        assert frame.size.width == mp.PANEL_WIDTH
        assert frame.size.height == mp.panel_height(len(panel.rows), layout.header_h, layout.row_h)


def test_an_over_row_does_not_narrow_the_bars():
    # The "(!)" column is retired: a menu with an over-limit row (the second
    # panel) lays out exactly like one without.
    with_over = pv.PanelLayout(_panels())
    without = pv.PanelLayout(_panels()[:1])
    assert any(row.state == "over" for row in _panels()[1].rows)
    assert with_over.columns == without.columns


def test_a_two_digit_slot_number_widens_the_gutter():
    one = mp.build_account_panel(("9", "a@x.com", False, {"five_hour": {"pct": 1.0}}, None, None, False, None), _NOW)
    two = mp.build_account_panel(("12", "b@x.com", False, {"five_hour": {"pct": 1.0}}, None, None, False, None), _NOW)
    num_font = pv._fonts()["num"]
    assert pv.PanelLayout([one]).columns.label_x == mp.LEFT_INSET + mp.NUM_COLUMN
    wide = pv.PanelLayout([one, two]).columns
    assert wide.label_x >= mp.LEFT_INSET + pv._width("12", num_font) + mp.NUM_GAP


# --- fonts and colours ------------------------------------------------------------------

def _weight(font):
    traits = font.fontDescriptor().objectForKey_(AppKit.NSFontTraitsAttribute)
    return float(traits[AppKit.NSFontWeightTrait])


def test_fonts():
    fonts = pv._fonts()
    menu = AppKit.NSFont.menuFontOfSize_(0.0).pointSize()
    small = AppKit.NSFont.smallSystemFontSize()
    sizes = {name: font.pointSize() for name, font in fonts.items()}
    assert sizes == {
        "title": menu, "title_active": menu, "label": small, "digits": small,
        "num": small, "email": small - 1.0, "meta": small, "tag": small - 2.0,
    }
    weights = {name: _weight(font) for name, font in fonts.items()}
    assert weights["title"] == pytest.approx(AppKit.NSFontWeightMedium, abs=0.01)
    assert weights["title_active"] == pytest.approx(AppKit.NSFontWeightSemibold, abs=0.01)
    assert weights["num"] == pytest.approx(AppKit.NSFontWeightMedium, abs=0.01)
    assert weights["tag"] == pytest.approx(AppKit.NSFontWeightSemibold, abs=0.01)  # the approved look
    for name in ("label", "digits", "email", "meta"):
        assert weights[name] == pytest.approx(AppKit.NSFontWeightRegular, abs=0.01)


_BAND_COLOURS = {
    "ok": "systemGreenColor", "warn": "systemYellowColor",
    "crit": "systemRedColor", "over": "systemRedColor",
}


@pytest.mark.parametrize("state, colour", list(_BAND_COLOURS.items()))
def test_fill_colour_by_state(state, colour):
    assert pv._fill_color(state).isEqual_(getattr(AppKit.NSColor, colour)())


def test_every_state_with_a_bar_has_a_band_colour():
    # An unknown row draws a note instead of a bar; every other state is a band.
    assert set(_BAND_COLOURS) == set(mp.STATES) - {"unknown"}


def _bar_fills(monkeypatch, panel):
    """The colours the view fills bars with, in drawing order (band colours only)."""
    bands = [getattr(AppKit.NSColor, name)() for name in
             ("systemGreenColor", "systemYellowColor", "systemOrangeColor", "systemRedColor")]
    fills = []
    real = pv._fill_rounded

    def spy(rect, radius, color):
        if any(color.isEqual_(band) for band in bands):
            fills.append(color)
        return real(rect, radius, color)

    view = pv.make_panel_view(panel, pv.PanelLayout([panel]))
    with monkeypatch.context() as patch:
        patch.setattr(pv, "_fill_rounded", spy)
        _render(view)
    return fills


@pytest.mark.parametrize(
    "pct, colour",
    [(74.9, "systemGreenColor"), (75.0, "systemYellowColor"), (92.0, "systemYellowColor"),
     (99.9, "systemYellowColor"), (100.0, "systemRedColor"), (130.0, "systemRedColor")],
)
def test_the_drawn_bar_follows_the_bands(monkeypatch, pct, colour):
    # Green under 75%, yellow from 75% up to the limit, red at 100% and over.
    usage = {"five_hour": {"pct": pct}}
    panel = mp.build_account_panel(("1", "a@x.com", True, usage, usage, None, False, None), _NOW)
    (fill,) = _bar_fills(monkeypatch, panel)
    assert _is(fill, colour)


def test_a_red_row_below_the_limit_has_a_red_bar_but_a_plain_percent(monkeypatch):
    # With red moved below the limit, 95% draws a red bar; only a window at
    # its limit gets the red percent.
    monkeypatch.setattr(mp, "CRIT_PCT", 90.0)
    usage = {"five_hour": {"pct": 95.0}}
    panel = mp.build_account_panel(("1", "a@x.com", True, usage, usage, None, False, None), _NOW)
    (fill,) = _bar_fills(monkeypatch, panel)
    assert _is(fill, "systemRedColor")
    assert _is(_colour_of(_drawn(monkeypatch, panel), "95%"), "labelColor")
    usage = {"five_hour": {"pct": 100.0}}
    panel = mp.build_account_panel(("1", "a@x.com", True, usage, usage, None, False, None), _NOW)
    assert _is(_colour_of(_drawn(monkeypatch, panel), "100%"), "systemRedColor")


def _states():
    """An active, an exhausted and a disabled account, as the redesign draws them."""
    def entry(num, email, active, usage, alias=None, disabled=False):
        return (num, email, active, usage, usage, alias, disabled, _NOW - 120)

    entries = [
        entry("1", "me@example.com", True,
              {"five_hour": {"pct": 17.0}, "seven_day": {"pct": 87.0},
               "scoped": [{"name": "Fable", "pct": 92.0}]}, alias="proton"),
        entry("2", "spent@example.com", False,
              {"five_hour": {"pct": 0.0}, "seven_day": {"pct": 100.0},
               "scoped": [{"name": "Fable", "pct": 75.0}]}, alias="meg"),
        entry("3", "off@example.com", False, {"five_hour": {"pct": 5.0}}, disabled=True),
    ]
    return [mp.build_account_panel(e, _NOW) for e in entries]


@pytest.mark.parametrize("highlighted", [False, True])
@pytest.mark.parametrize("appearance", ["aqua", "darkAqua"])
def test_active_exhausted_and_disabled_render_at_the_model_height(tmp_path, appearance, highlighted):
    panels = _states()
    assert [(p.is_active, p.exhausted, p.disabled) for p in panels] == [
        (True, False, False), (False, True, False), (False, False, True),
    ]
    layout = pv.PanelLayout(panels)
    expected = sum(mp.panel_height(len(p.rows), layout.header_h, layout.row_h) for p in panels)
    path = tmp_path / f"{appearance}-{highlighted}.png"
    assert pv.render_png(panels, str(path), appearance, highlighted=highlighted) == (mp.PANEL_WIDTH, expected)
    rep = AppKit.NSBitmapImageRep.imageRepWithContentsOfFile_(str(path))
    assert rep.pixelsWide() / rep.pixelsHigh() == pytest.approx(mp.PANEL_WIDTH / expected, rel=1e-3)


def _alpha_at(view, rep, x, y):
    """Alpha of the pixel at view point (x, y), y measured from the panel top."""
    scale = rep.pixelsWide() / view.bounds().size.width
    return rep.colorAtX_y_(int(x * scale), int(y * scale)).alphaComponent()


def test_only_the_active_panel_sits_on_the_accent_card():
    # Left of the slot number, inside the card's inset: tinted for the active
    # account, untouched for the others.
    panels = _states()
    layout = pv.PanelLayout(panels)
    alphas = []
    for panel in panels:
        view = pv.make_panel_view(panel, layout)
        rep = _render(view)
        alphas.append(_alpha_at(view, rep, mp.CARD_INSET + 2.0, view.bounds().size.height / 2))
    assert 0.08 < alphas[0] < 0.3  # accent at 0.16
    assert alphas[1] == 0.0 and alphas[2] == 0.0


def _rgba(color):
    color = color.colorUsingColorSpace_(AppKit.NSColorSpace.sRGBColorSpace())
    return tuple(round(v, 3) for v in (
        color.redComponent(), color.greenComponent(), color.blueComponent(), color.alphaComponent()
    ))


def test_inactive_panels_end_in_a_hairline_and_the_active_one_does_not():
    panels = _states()
    layout = pv.PanelLayout(panels)
    x = layout.columns.label_x + 40.0  # under the bars, clear of any text
    plain = []
    for panel in panels:
        view = pv.make_panel_view(panel, layout)
        rep = _render(view)
        scale = rep.pixelsWide() / view.bounds().size.width
        bottom = rep.colorAtX_y_(int(x * scale), rep.pixelsHigh() - 1).alphaComponent()
        above = _alpha_at(view, rep, x, view.bounds().size.height - 2.5)
        plain.append((bottom > 0.02, above))
    assert [has for has, _above in plain] == [False, True, True]
    # A thin line, not a band (the active panel's card covers this spot).
    assert all(above == 0.0 for _has, above in plain[1:])


def _bottom_alpha(view, x):
    rep = _render(view)
    scale = rep.pixelsWide() / view.bounds().size.width
    return rep.colorAtX_y_(int(x * scale), rep.pixelsHigh() - 1).alphaComponent()


def test_the_last_panel_has_no_hairline_whatever_its_state():
    # A native separator follows the account rows; a hairline under the last
    # panel would stack on it. Any other inactive panel keeps its hairline.
    panels = _states()
    layout = pv.PanelLayout(panels)
    x = layout.columns.label_x + 40.0
    for panel in panels[1:]:  # the inactive ones: exhausted, disabled
        assert _bottom_alpha(pv.make_panel_view(panel, layout), x) > 0.02
        assert _bottom_alpha(pv.make_panel_view(panel, layout, is_last=False), x) > 0.02
        assert _bottom_alpha(pv.make_panel_view(panel, layout, is_last=True), x) == 0.0
    assert _bottom_alpha(pv.make_panel_view(panels[0], layout, is_last=True), x) == 0.0


def test_attach_marks_only_the_last_panel_as_last():
    panels = _panels()
    items = _items(panels)
    pv.attach_usage_panels(list(zip(items, panels)))
    assert [item._menuitem.view().is_last for item in items] == [False, False, True]


@pytest.mark.parametrize("appearance", ["aqua", "darkAqua"])
def test_render_png_draws_no_hairline_under_an_inactive_last_panel(tmp_path, appearance):
    panels = _states()  # active, exhausted, disabled: an inactive panel last
    layout = pv.PanelLayout(panels)
    path = tmp_path / "last.png"
    pv.render_png(panels, str(path), appearance)
    rep = AppKit.NSBitmapImageRep.imageRepWithContentsOfFile_(str(path))
    scale = rep.pixelsWide() / mp.PANEL_WIDTH
    x = int((layout.columns.label_x + 40.0) * scale)

    def at(y_pt):
        return _rgba(rep.colorAtX_y_(x, int(y_pt * scale)))

    heights = [layout.height(p) for p in panels]
    second_end = heights[0] + heights[1]
    assert at(second_end - 0.25) != at(second_end - 3.0)  # inactive, not last: a hairline
    bottom = _rgba(rep.colorAtX_y_(x, rep.pixelsHigh() - 1))
    assert bottom == at(sum(heights) - 3.0)  # last: plain background to the edge


def test_highlighted_panels_have_no_hairline_over_the_selection():
    # The bottom pixel row is plain selection, the same colour as the empty
    # bottom padding just above it.
    panels = _states()
    layout = pv.PanelLayout(panels)
    x = layout.columns.label_x + 40.0
    for panel in panels:
        view = pv.make_panel_view(panel, layout)
        view.force_highlight = True
        rep = _render(view)
        scale = rep.pixelsWide() / view.bounds().size.width
        bottom = rep.colorAtX_y_(int(x * scale), rep.pixelsHigh() - 1)
        above = rep.colorAtX_y_(int(x * scale), int((view.bounds().size.height - 4.0) * scale))
        assert _rgba(bottom) == _rgba(above)


def test_an_exhausted_account_draws_its_bars_dimmed():
    # Same usage, active versus not: the full red bar is opaque on the
    # active card and see-through for the exhausted account.
    usage = {"seven_day": {"pct": 100.0}}
    active = mp.build_account_panel(("1", "a@x.com", True, usage, usage, None, False, None), _NOW)
    spent = mp.build_account_panel(("2", "b@x.com", False, usage, usage, None, False, None), _NOW)
    layout = pv.PanelLayout([active, spent])
    alphas = []
    for panel in (active, spent):
        view = pv.make_panel_view(panel, layout)
        rep = _render(view)
        bx, by, bw, bh = mp.bar_rect(layout.columns, mp.TOP_PAD + layout.header_h + mp.HEADER_GAP, layout.row_h)
        alphas.append(_alpha_at(view, rep, bx + bw / 2, by + bh / 2))
    assert alphas[0] > 0.97
    assert 0.55 < alphas[1] < 0.8  # 0.62 over the faint track


# --- what each panel draws, and in which colour --------------------------------------

def _drawn(monkeypatch, panel, layout=None, highlighted=False):
    """Every string the view draws for ``panel``, with its font and colour.

    The layout is measured before the spy goes in, so only drawing counts.
    """
    calls = []
    real = pv._text
    layout = layout or pv.PanelLayout([panel])
    view = pv.make_panel_view(panel, layout)
    view.force_highlight = highlighted

    def spy(string, font, color, truncate=None):
        calls.append((string, font, color))
        return real(string, font, color, truncate)

    with monkeypatch.context() as patch:
        patch.setattr(pv, "_text", spy)
        _render(view)
    return calls


def _colour_of(calls, string):
    (color,) = [c for s, _f, c in calls if s == string]
    return color


def _is(color, name, alpha=None):
    want = getattr(AppKit.NSColor, name)()
    if alpha is not None:
        want = want.colorWithAlphaComponent_(alpha)
    return color.isEqual_(want)


def test_active_header_draws_number_name_email_tag_and_age(monkeypatch):
    panel = _states()[0]
    calls = _drawn(monkeypatch, panel)
    strings = [s for s, _f, _c in calls]
    for text in ("1", "proton", "me@example.com", "ACTIVE", "2m ago"):
        assert text in strings
    assert "proton  (me@example.com)" not in strings  # the old one-piece title
    assert _is(_colour_of(calls, "1"), "tertiaryLabelColor")
    assert _is(_colour_of(calls, "proton"), "labelColor")
    assert _is(_colour_of(calls, "me@example.com"), "tertiaryLabelColor")
    assert _is(_colour_of(calls, "ACTIVE"), "controlAccentColor")
    assert _is(_colour_of(calls, "2m ago"), "tertiaryLabelColor")
    fonts = pv._fonts()
    name_font = [f for s, f, _c in calls if s == "proton"][0]
    assert name_font.isEqual_(fonts["title_active"])


def test_exhausted_panel_is_muted_with_a_red_percent_and_no_marker(monkeypatch):
    panel = _states()[1]
    calls = _drawn(monkeypatch, panel)
    strings = [s for s, _f, _c in calls]
    assert "(!)" not in strings
    assert "active" not in strings and "ACTIVE" not in strings
    assert _is(_colour_of(calls, "meg"), "secondaryLabelColor")
    assert _is(_colour_of(calls, "100%"), "systemRedColor", 0.85)
    assert _is(_colour_of(calls, "75%"), "secondaryLabelColor")
    name_font = [f for s, f, _c in calls if s == "meg"][0]
    assert name_font.isEqual_(pv._fonts()["title"])


def test_disabled_panel_says_paused_in_the_meta_colour(monkeypatch):
    # Disabled with no hold of the rotation guard on it: the owner's pause.
    panel = _states()[2]
    calls = _drawn(monkeypatch, panel)
    assert _is(_colour_of(calls, "paused"), "secondaryLabelColor")
    assert [f for s, f, _c in calls if s == "paused"][0].isEqual_(pv._fonts()["meta"])
    assert "disabled" not in [s for s, _f, _c in calls]
    assert "off@example.com" in [s for s, _f, _c in calls]


_HELD_ENTRY = ("3", "off@example.com", False, {"five_hour": {"pct": 5.0}}, None, None, True, _NOW - 120)
_HOLDS = {"3": ("off@example.com", _NOW + 28 * 60, None)}


def test_a_held_panel_draws_its_countdown_with_the_word(monkeypatch):
    panel = mp.build_account_panel(_HELD_ENTRY, _NOW, holds=_HOLDS)
    calls = _drawn(monkeypatch, panel)
    strings = [s for s, _f, _c in calls]
    # One piece, so the countdown never reads as the age beside it.
    assert "held · 28m" in strings and "held" not in strings and "28m" not in strings
    assert _is(_colour_of(calls, "held · 28m"), "secondaryLabelColor")
    assert [f for s, f, _c in calls if s == "held · 28m"][0].isEqual_(pv._fonts()["meta"])
    assert "2m ago" in strings
    assert _is(_colour_of(calls, "off"), "secondaryLabelColor")  # the name dims, as for a pause
    assert _is(_colour_of(calls, "5%"), "labelColor")  # the numbers do not


def test_an_active_held_panel_draws_the_tag_then_the_hold(monkeypatch):
    entry = ("3",) + _HELD_ENTRY[1:2] + (True,) + _HELD_ENTRY[3:]
    calls = _drawn(monkeypatch, mp.build_account_panel(entry, _NOW, holds=_HOLDS))
    strings = [s for s, _f, _c in calls]
    assert strings.index("ACTIVE") < strings.index("held · 28m") < strings.index("2m ago")


def test_a_disabled_account_keeps_its_dimmed_name(monkeypatch):
    # Out of rotation reads as muted, as before the redesign; only the name
    # dims, the numbers stay full strength (it is not exhausted).
    panel = _states()[2]
    assert panel.disabled and not panel.exhausted
    calls = _drawn(monkeypatch, panel)
    assert _is(_colour_of(calls, "off"), "secondaryLabelColor")  # name_text: the local part
    assert _is(_colour_of(calls, "5%"), "labelColor")


def _sentinel_panel(sentinel, num="5"):
    from claude_swap.usage_store import UsageEntry

    display = menubar._account_display_usage(UsageEntry(sentinel=sentinel))
    return mp.build_account_panel((num, "dead@example.com", False, display, None, None, False, None), _NOW)


@pytest.mark.parametrize("sentinel", ["re-login needed", "no credentials"])
def test_a_dead_login_note_is_red(monkeypatch, sentinel):
    panel = _sentinel_panel(sentinel)
    (row,) = panel.rows
    assert row.urgent
    assert _is(_colour_of(_drawn(monkeypatch, panel), row.note), "systemRedColor")
    assert _is(_colour_of(_drawn(monkeypatch, panel, highlighted=True), row.note), "selectedMenuItemTextColor")


@pytest.mark.parametrize("sentinel", ["keychain unavailable", "api key", "token expired"])
def test_other_unknown_notes_stay_secondary(monkeypatch, sentinel):
    panel = _sentinel_panel(sentinel)
    (row,) = panel.rows
    assert not row.urgent
    assert _is(_colour_of(_drawn(monkeypatch, panel), row.note), "secondaryLabelColor")


@pytest.mark.parametrize("appearance", ["aqua", "darkAqua"])
def test_render_png_with_a_dead_login_row(tmp_path, appearance):
    panels = _states() + [_sentinel_panel("re-login needed", num="4")]
    assert panels[-1].rows[0].urgent
    layout = pv.PanelLayout(panels)
    expected = sum(layout.height(p) for p in panels)
    path = tmp_path / f"dead-{appearance}.png"
    assert pv.render_png(panels, str(path), appearance) == (mp.PANEL_WIDTH, expected)
    assert path.stat().st_size > 1000


def test_an_active_disabled_account_dims_its_name_too(monkeypatch):
    entry = ("1", "on@example.com", True, {"five_hour": {"pct": 5.0}}, None, "on", True, None)
    calls = _drawn(monkeypatch, mp.build_account_panel(entry, _NOW))
    assert _is(_colour_of(calls, "on"), "secondaryLabelColor")


def test_an_over_row_on_the_active_account_has_a_full_strength_red_percent(monkeypatch):
    usage = {"five_hour": {"pct": 100.0}}
    panel = mp.build_account_panel(("1", "a@x.com", True, usage, usage, None, False, None), _NOW)
    calls = _drawn(monkeypatch, panel)
    assert _is(_colour_of(calls, "100%"), "systemRedColor")


def test_highlighted_panel_draws_every_text_in_the_selection_colour(monkeypatch):
    for panel in _states():
        calls = _drawn(monkeypatch, panel, highlighted=True)
        assert calls
        assert all(_is(c, "selectedMenuItemTextColor") for _s, _f, c in calls)


def test_names_only_panel_draws_no_email_for_an_aliased_account(monkeypatch):
    entry = ("4", "me@x.com", False, {"five_hour": {"pct": 1.0}}, None, "work", False, None)
    shown = [s for s, _f, _c in _drawn(monkeypatch, mp.build_account_panel(entry, _NOW))]
    hidden = [s for s, _f, _c in _drawn(monkeypatch, mp.build_account_panel(entry, _NOW, names_only=True))]
    assert "work" in shown and "me@x.com" in shown
    assert "work" in hidden and "me@x.com" not in hidden


@pytest.mark.parametrize("appearance", ["aqua", "darkAqua"])
def test_offscreen_render_in_both_appearances(tmp_path, appearance):
    path = tmp_path / f"{appearance}.png"
    size = pv.render_png(_panels(), str(path), appearance)
    assert path.stat().st_size > 1000
    width, height = size
    assert width == mp.PANEL_WIDTH
    assert height > 0


def test_render_highlighted_does_not_crash(tmp_path):
    pv.render_png(_panels(), str(tmp_path / "hl.png"), "darkAqua", highlighted=True)


def test_very_long_identity_renders(tmp_path):
    email = "an.extremely.long.address.for.testing.truncation@some-very-long-domain.example.com"
    entry = ("12", email, True, {"five_hour": {"pct": 10.0}}, None, "work", False, _NOW - 60)
    panel = mp.build_account_panel(entry, _NOW)
    pv.render_png([panel], str(tmp_path / "long.png"), "darkAqua")


def test_view_stretches_to_the_menu_width():
    # NSMenu widens width-sizable item views to the menu's width; the panel
    # then lays its right-hand columns out against that width.
    panels = _panels()
    view = pv.make_panel_view(panels[0], pv.PanelLayout(panels))
    assert view.autoresizingMask() & AppKit.NSViewWidthSizable


def test_columns_follow_the_actual_width_with_a_floor():
    layout = pv.PanelLayout(_panels())
    wide = layout.columns_for(400.0)
    assert wide.reset_right == 400.0 - mp.RIGHT_INSET
    assert wide.bar_w == layout.columns.bar_w + 60.0  # the bar takes the extra width
    narrow = layout.columns_for(200.0)
    assert narrow == layout.columns  # never narrower than the fixed width


# --- activation: only a live, highlighted panel in the open menu can switch ---------

def test_click_inside_a_highlighted_panel_switches_that_account(run_now, on_screen, switched):
    m = _Menu(_panels(), switched.append)
    view = m.views[1]
    view.force_highlight = True
    view.mouseUp_(_mouse_up(view, inside=True))
    assert switched == ["2"]


def test_release_outside_the_panel_does_nothing(run_now, on_screen, switched):
    # Press, drag out of the row, release: the native "changed my mind".
    m = _Menu(_panels(), switched.append)
    view = m.views[1]
    view.force_highlight = True
    view.mouseUp_(_mouse_up(view, inside=False))
    assert switched == []


def test_release_on_a_panel_that_is_not_highlighted_does_nothing(run_now, on_screen, switched):
    m = _Menu(_panels(), switched.append)
    view = m.views[1]
    view.mouseUp_(_mouse_up(view, inside=True))
    assert switched == []


def test_release_without_an_event_does_nothing(run_now, on_screen, switched):
    m = _Menu(_panels(), switched.append)
    m.views[0].force_highlight = True
    m.views[0].mouseUp_(None)
    assert switched == []


def test_press_after_the_menu_closed_does_nothing(run_now, on_screen, switched):
    # An accessibility client can still hold the panel after dismissal.
    m = _Menu(_panels(), switched.append)
    m.open = False
    assert m.views[1].accessibilityPerformPress() is False
    assert switched == []


def test_press_from_a_detached_panel_does_nothing(run_now, on_screen, switched):
    m = _Menu(_panels(), switched.append)
    m.menu.removeItem_(m.items[1])
    m.views[1].accessibilityPerformPress()
    assert switched == []


def test_press_from_a_panel_that_is_not_on_screen_does_nothing(run_now, switched):
    m = _Menu(_panels(), switched.append)  # real _on_screen: no window in a test
    m.views[1].accessibilityPerformPress()
    assert switched == []


def test_press_from_an_earlier_rebuilds_panel_while_open_again_does_nothing(run_now, on_screen, switched):
    m = _Menu(_panels(), switched.append)
    stale = m.views[1]
    m.menu.removeAllItems()  # the next rebuild replaces every row
    layout = pv.PanelLayout(_panels())
    for panel in _panels():
        item = AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(panel.text_label, None, "")
        item.setView_(pv.make_panel_view(panel, layout, target=m.target))
        m.menu.addItem_(item)
    m.open = True
    stale.accessibilityPerformPress()
    assert switched == []
    m.menu.itemArray()[1].view().accessibilityPerformPress()  # the current panel still works
    assert switched == ["2"]


def test_press_from_a_panel_in_some_other_menu_does_nothing(run_now, on_screen, switched):
    m = _Menu(_panels(), switched.append)
    other = AppKit.NSMenu.alloc().init()
    m.menu.removeItem_(m.items[0])
    other.addItem_(m.items[0])  # attached and on screen, but not the app's menu
    m.views[0].accessibilityPerformPress()
    assert switched == []


def test_request_closes_the_menu_before_the_switch_runs(on_screen, switched, monkeypatch):
    queued, cancelled = [], []
    monkeypatch.setattr(pv.AppHelper, "callAfter", lambda fn, *a, **k: queued.append((fn, a)))
    m = _Menu(_panels(), switched.append)
    monkeypatch.setattr(pv, "_cancel_tracking", lambda menu: cancelled.append(menu))
    assert m.views[0].accessibilityPerformPress() is True
    assert cancelled == [m.menu] and switched == []  # queued, not yet run
    fn, args = queued.pop()
    fn(*args)
    assert switched == ["1"]


def test_activation_without_a_target_does_nothing(run_now):
    panels = _panels()
    view = pv.make_panel_view(panels[0], pv.PanelLayout(panels))
    view.force_highlight = True
    view.mouseUp_(_mouse_up(view))  # must not raise


def test_switch_receives_the_number_as_a_string(run_now, on_screen, switched):
    entry = (7, "n@x.com", False, {"five_hour": {"pct": 1.0}}, None, None, False, None)
    panel = mp.build_account_panel(entry, _NOW)
    menu = AppKit.NSMenu.alloc().init()
    t = pv.make_account_target(switched.append, is_open=lambda: True, menu=menu)
    item = AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_("x", None, "")
    view = pv.make_panel_view(panel, pv.PanelLayout([panel]), target=t)
    item.setView_(view)
    menu.addItem_(item)
    view.accessibilityPerformPress()
    assert switched == ["7"]


def test_switch_errors_are_logged_not_raised(run_now, on_screen, caplog):
    def boom(_num):
        raise RuntimeError("switch failed")

    m = _Menu(_panels(), boom)
    with caplog.at_level(logging.WARNING, logger="claude-swap"):
        m.views[0].accessibilityPerformPress()  # must not raise out of an AppKit callback
    assert any(r.exc_info for r in caplog.records)


# --- keyboard: Return on a highlighted panel acts like Return on a text row -------

def _key_event(code: int):
    return AppKit.NSEvent.keyEventWithType_location_modifierFlags_timestamp_windowNumber_context_characters_charactersIgnoringModifiers_isARepeat_keyCode_(
        AppKit.NSEventTypeKeyDown, (0, 0), 0, 0, 0, None, "\r", "\r", False, code
    )


def test_panel_takes_focus_only_when_the_menu_highlights_it():
    # A panel that always accepted first responder became the menu window's
    # initial focus: the menu opened with the first account highlighted, and
    # Return right after opening would have switched to it. Native menus open
    # with nothing selected, so focus is granted only on highlight.
    panels = _panels()
    layout = pv.PanelLayout(panels)
    menu = AppKit.NSMenu.alloc().init()
    delegate = pv.install_menu_delegate(menu)
    items, views = [], []
    for panel in panels[:2]:
        item = AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(panel.text_label, None, "")
        view = pv.make_panel_view(panel, layout)
        item.setView_(view)
        menu.addItem_(item)
        items.append(item)
        views.append(view)
    native = AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_("Quit", None, "")
    menu.addItem_(native)

    assert [v.acceptsFirstResponder() for v in views] == [False, False]  # menu just opened
    delegate.menu_willHighlightItem_(menu, items[1])
    assert [v.acceptsFirstResponder() for v in views] == [False, True]
    delegate.menu_willHighlightItem_(menu, items[0])
    assert [v.acceptsFirstResponder() for v in views] == [True, False]
    delegate.menu_willHighlightItem_(menu, native)
    assert [v.acceptsFirstResponder() for v in views] == [False, False]
    delegate.menu_willHighlightItem_(menu, items[1])
    delegate.menuDidClose_(menu)
    assert [v.acceptsFirstResponder() for v in views] == [False, False]  # next open starts clean


@pytest.mark.parametrize("code", [36, 76])  # Return, keypad Enter
def test_return_on_the_highlighted_panel_activates_it(run_now, on_screen, switched, code):
    m = _Menu(_panels(), switched.append)
    m.views[0].force_highlight = True
    m.views[0].keyDown_(_key_event(code))
    assert switched == ["1"]


def test_return_on_a_panel_that_is_not_highlighted_does_nothing(run_now, on_screen, switched):
    # A stale first responder must never fire when another item is selected.
    m = _Menu(_panels(), switched.append)
    m.views[0].keyDown_(_key_event(36))
    assert switched == []


def test_other_keys_do_not_activate(run_now, on_screen, switched):
    m = _Menu(_panels(), switched.append)
    m.views[0].force_highlight = True
    m.views[0].keyDown_(_key_event(125))  # Down arrow
    assert switched == []


# --- accessibility ---------------------------------------------------------------------

def test_panel_is_an_accessible_button_with_a_spoken_label():
    panels = _panels()
    view = pv.make_panel_view(panels[0], pv.PanelLayout(panels))
    assert view.isAccessibilityElement()
    assert view.accessibilityRole() == AppKit.NSAccessibilityButtonRole
    assert view.accessibilityLabel() == mp.accessibility_label(panels[0])


def test_accessibility_press_in_the_open_menu_switches(run_now, on_screen, switched):
    m = _Menu(_panels(), switched.append)
    assert m.views[2].accessibilityPerformPress() is True
    assert switched == ["3"]


# --- attach: all or nothing ---------------------------------------------------------------

def test_attach_sets_views_and_keeps_text_titles():
    panels = _panels()
    items = _items(panels)
    pv.attach_usage_panels(list(zip(items, panels)))
    for item, panel in zip(items, panels):
        assert item._menuitem.view() is not None
        assert item._menuitem.title() == panel.text_label


def test_attached_items_drop_the_checkmark_state():
    # The accent card marks the active account; a state column would push the
    # native items' text right of the panels' text.
    panels = _panels()
    items = _items(panels)
    pv.attach_usage_panels(list(zip(items, panels)))
    assert all(item._menuitem.state() == 0 for item in items)


def test_failure_at_the_second_item_rolls_every_item_back_to_text(monkeypatch):
    panels = _panels()
    items = _items(panels)
    before = _text_state(items)
    real = pv._set_item_view
    calls = []

    def flaky(nsitem, view):
        calls.append(nsitem)
        if len(calls) == 2:
            raise RuntimeError("setView failed")
        real(nsitem, view)

    monkeypatch.setattr(pv, "_set_item_view", flaky)
    with pytest.raises(RuntimeError):
        pv.attach_usage_panels(list(zip(items, panels)))
    assert _text_state(items) == before


def test_failure_after_attach_rolls_every_item_back_to_text():
    panels = _panels()
    items = _items(panels)
    before = _text_state(items)

    def install_fails():
        raise RuntimeError("delegate install failed")

    with pytest.raises(RuntimeError):
        pv.attach_usage_panels(list(zip(items, panels)), after_attach=install_fails)
    assert _text_state(items) == before


def test_views_are_built_before_any_item_is_touched(monkeypatch):
    panels = _panels()
    items = _items(panels)
    before = _text_state(items)
    real = pv.make_panel_view
    built = []
    touched_when_failing = []

    def flaky(panel, layout, **kw):
        built.append(panel)
        if len(built) == 3:
            # At the moment the last view fails to build, no item may have
            # been given a view yet: building happens before attaching.
            touched_when_failing.extend(i for i in items if i._menuitem.view() is not None)
            raise RuntimeError("measure failed")
        return real(panel, layout, **kw)

    monkeypatch.setattr(pv, "make_panel_view", flaky)
    with pytest.raises(RuntimeError):
        pv.attach_usage_panels(list(zip(items, panels)))
    assert touched_when_failing == []
    assert _text_state(items) == before


# --- drawing failures fall back to text and report once ---------------------------------

def test_draw_failure_draws_text_and_reports(monkeypatch):
    panels = _panels()
    reports = []
    view = pv.make_panel_view(panels[0], pv.PanelLayout(panels), on_draw_failure=lambda: reports.append(1))

    def boom(_view, _dirty):
        raise RuntimeError("draw failed")

    fallback = []
    real_fallback = pv._draw_fallback
    monkeypatch.setattr(pv, "_draw_panel", boom)
    monkeypatch.setattr(pv, "_draw_fallback", lambda v: fallback.append(v) or real_fallback(v))
    _render(view)  # must not raise
    assert reports == [1]
    assert fallback == [view]


def _png(view):
    rep = _render(view)
    return bytes(rep.representationUsingType_properties_(AppKit.NSBitmapImageFileTypePNG, {}))


def _raise(*_a, **_k):
    raise RuntimeError("draw failed")


@pytest.mark.parametrize("highlighted", [False, True])
def test_draw_failure_mid_panel_leaves_only_the_fallback(monkeypatch, highlighted):
    # The header draws, then a row throws: the header's pixels must not show
    # through the fallback text. Compare with the fallback drawn on its own.
    panels = _panels()
    layout = pv.PanelLayout(panels)

    def view():
        v = pv.make_panel_view(panels[0], layout)
        v.force_highlight = highlighted
        return v

    with monkeypatch.context() as patch:
        patch.setattr(pv, "_draw_panel", _raise)  # nothing drawn before the fallback
        clean = _png(view())
    with monkeypatch.context() as patch:
        patch.setattr(pv, "_draw_panel", lambda *_a: None)
        blank = _png(view())
    with monkeypatch.context() as patch:
        patch.setattr(pv, "_draw_row", _raise)  # header drawn, first row throws
        broken = _png(view())
    assert clean != blank  # the reference really has the fallback text in it
    assert broken == clean


def test_fallback_keeps_the_selection_when_highlighted(monkeypatch):
    # Sample a pixel between the selection's inset and the text: the accent
    # selection when highlighted, cleared (transparent) otherwise.
    panels = _panels()
    layout = pv.PanelLayout(panels)
    monkeypatch.setattr(pv, "_draw_panel", _raise)
    alphas = {}
    for highlighted in (False, True):
        view = pv.make_panel_view(panels[0], layout)
        view.force_highlight = highlighted
        rep = _render(view)
        scale = rep.pixelsWide() / view.bounds().size.width
        x = int((pv.CswapUsagePanelView.HIGHLIGHT_INSET + 3.0) * scale)
        y = int(view.bounds().size.height / 2 * scale)
        alphas[highlighted] = rep.colorAtX_y_(x, y).alphaComponent()
    assert alphas[True] > 0.9
    assert alphas[False] < 0.1


def test_draw_failure_without_a_handler_does_not_raise(monkeypatch):
    panels = _panels()
    view = pv.make_panel_view(panels[0], pv.PanelLayout(panels))
    monkeypatch.setattr(pv, "_draw_panel", lambda *_a: (_ for _ in ()).throw(RuntimeError("x")))
    _render(view)


# --- menu delegate: open/close for deferred rebuilds, focus for Return -----------------

def test_menu_delegate_reports_open_and_close():
    events = []
    menu = AppKit.NSMenu.alloc().init()
    pv.install_menu_delegate(menu, on_open=lambda: events.append("open"), on_close=lambda: events.append("close"))
    menu.delegate().menuWillOpen_(menu)
    menu.delegate().menuDidClose_(menu)
    assert events == ["open", "close"]


def test_menu_delegate_is_retained_and_idempotent():
    menu = AppKit.NSMenu.alloc().init()
    first = pv.install_menu_delegate(menu)
    gc.collect()
    assert menu.delegate() is not None  # NSMenu holds its delegate weakly
    assert pv.install_menu_delegate(menu) is first


def test_delegate_tolerates_native_and_missing_items():
    menu = AppKit.NSMenu.alloc().init()
    pv.install_menu_delegate(menu)
    native = AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_("Quit", None, "")
    menu.addItem_(native)
    menu.delegate().menu_willHighlightItem_(menu, native)
    menu.delegate().menu_willHighlightItem_(menu, None)


# --- rebuild leak check ----------------------------------------------------------------------

def _rebuild(root, registry, panels, target):
    """What rebuild_menu does to the account rows: purge, clear, re-add."""
    menubar.purge_menu_callbacks(registry, root._menu)
    root.clear()
    items = [rumps.MenuItem(p.text_label, callback=lambda _s: None) for p in panels]
    pv.attach_usage_panels(list(zip(items, panels)), target=target)
    root.update(items)


def test_purge_covers_view_backed_items(target):
    registry = rumps.rumps.NSApp._ns_to_py_and_callback
    root = rumps.rumps.Menu()
    panels = _panels()
    _rebuild(root, registry, panels, target)
    natives = list(root._menu.itemArray())
    assert all(n in registry for n in natives)
    assert all(n.view() is not None for n in natives)
    menubar.purge_menu_callbacks(registry, root._menu)
    assert not any(n in registry for n in natives)


def test_no_growth_across_200_rebuilds(target):
    registry = rumps.rumps.NSApp._ns_to_py_and_callback
    root = rumps.rumps.Menu()
    panels = _panels()
    with objc.autorelease_pool():
        _rebuild(root, registry, panels, target)
    gc.collect()
    base_views, base_registry = pv.live_view_count(), len(registry)
    for _ in range(200):
        with objc.autorelease_pool():
            _rebuild(root, registry, panels, target)
    gc.collect()
    assert pv.live_view_count() == base_views
    assert len(registry) == base_registry
    # Tear down: nothing of ours stays alive once the menu is gone.
    with objc.autorelease_pool():
        menubar.purge_menu_callbacks(registry, root._menu)
        root.clear()
    gc.collect()
    assert pv.live_view_count() == base_views - len(panels)


# --- Refresh now: a native-looking row whose press keeps the menu open ---------------

class _RefreshMenu:
    """A real NSMenu: the refresh row above a native Quit, and the open flag the app keeps."""

    def __init__(self, on_refresh):
        self.open = True
        self.menu = AppKit.NSMenu.alloc().init()
        self.target = pv.make_account_target(
            lambda _num: None, is_open=lambda: self.open, menu=self.menu, on_refresh=on_refresh
        )
        self.item = AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_("Refresh now", None, "")
        self.view = pv.make_refresh_row_view(target=self.target)
        self.item.setView_(self.view)
        self.menu.addItem_(self.item)
        self.quit = AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_("Quit", "terminate:", "")
        self.menu.addItem_(self.quit)


@pytest.fixture
def pressed():
    return []


@pytest.fixture
def cancelled(monkeypatch):
    calls = []
    monkeypatch.setattr(pv, "_cancel_tracking", lambda menu: calls.append(menu))
    return calls


def test_refresh_row_click_asks_the_app_and_keeps_the_menu_open(on_screen, pressed, cancelled):
    m = _RefreshMenu(lambda: pressed.append(1))
    m.view.force_highlight = True
    m.view.mouseUp_(_mouse_up(m.view, inside=True))
    assert pressed == [1]
    assert cancelled == []  # tracking goes on: the menu stays open


def test_refresh_row_release_outside_or_unhighlighted_does_nothing(on_screen, pressed, cancelled):
    m = _RefreshMenu(lambda: pressed.append(1))
    m.view.mouseUp_(_mouse_up(m.view, inside=True))  # not highlighted
    m.view.force_highlight = True
    m.view.mouseUp_(_mouse_up(m.view, inside=False))
    m.view.mouseUp_(None)
    assert pressed == [] and cancelled == []


def test_refresh_row_ignores_presses_while_refreshing(on_screen, pressed):
    m = _RefreshMenu(lambda: pressed.append(1))
    m.view.set_busy(True)
    m.view.force_highlight = True
    m.view.mouseUp_(_mouse_up(m.view))
    m.view.keyDown_(_key_event(36))
    assert m.view.accessibilityPerformPress() is False
    assert pressed == []
    m.view.set_busy(False)
    assert m.view.accessibilityPerformPress() is True
    assert pressed == [1]


def test_refresh_row_press_after_the_menu_closed_does_nothing(on_screen, pressed):
    m = _RefreshMenu(lambda: pressed.append(1))
    m.open = False
    assert m.view.accessibilityPerformPress() is False
    assert pressed == []


def test_refresh_row_press_from_a_detached_row_does_nothing(on_screen, pressed):
    m = _RefreshMenu(lambda: pressed.append(1))
    m.menu.removeItem_(m.item)
    assert m.view.accessibilityPerformPress() is False
    assert pressed == []


def test_refresh_row_handler_errors_are_logged_not_raised(on_screen, caplog):
    def boom():
        raise RuntimeError("refresh failed")

    m = _RefreshMenu(boom)
    with caplog.at_level(logging.DEBUG, logger="claude-swap"):
        assert m.view.accessibilityPerformPress() is False  # must not raise out of AppKit
    assert any(r.exc_info for r in caplog.records)


@pytest.mark.parametrize("code", [36, 76])  # Return, keypad Enter
def test_return_on_the_refresh_row_works_through_the_delegate(on_screen, pressed, cancelled, code):
    m = _RefreshMenu(lambda: pressed.append(1))
    delegate = pv.install_menu_delegate(m.menu)
    assert not m.view.acceptsFirstResponder()  # the menu just opened: nothing selected
    delegate.menu_willHighlightItem_(m.menu, m.item)
    assert m.view.acceptsFirstResponder()
    m.view.force_highlight = True
    m.view.keyDown_(_key_event(code))
    assert pressed == [1] and cancelled == []
    delegate.menu_willHighlightItem_(m.menu, m.quit)
    assert not m.view.acceptsFirstResponder()  # Return on Quit is the menu's own
    delegate.menu_willHighlightItem_(m.menu, m.item)
    delegate.menuDidClose_(m.menu)
    assert not m.view.acceptsFirstResponder()  # the next open starts clean


def test_highlighting_the_refresh_row_takes_focus_from_a_panel():
    panels = _panels()
    menu = AppKit.NSMenu.alloc().init()
    delegate = pv.install_menu_delegate(menu)
    panel_item = AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_("p", None, "")
    panel_view = pv.make_panel_view(panels[0], pv.PanelLayout(panels))
    panel_item.setView_(panel_view)
    menu.addItem_(panel_item)
    row_item = AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_("Refresh now", None, "")
    row = pv.make_refresh_row_view()
    row_item.setView_(row)
    menu.addItem_(row_item)
    delegate.menu_willHighlightItem_(menu, panel_item)
    assert [panel_view.acceptsFirstResponder(), row.acceptsFirstResponder()] == [True, False]
    delegate.menu_willHighlightItem_(menu, row_item)
    assert [panel_view.acceptsFirstResponder(), row.acceptsFirstResponder()] == [False, True]


def test_a_panel_still_closes_the_menu_on_its_own_press(on_screen, switched, cancelled, monkeypatch):
    monkeypatch.setattr(pv.AppHelper, "callAfter", lambda fn, *a, **k: fn(*a, **k))
    m = _Menu(_panels(), switched.append)
    assert m.views[1].accessibilityPerformPress() is True
    assert cancelled == [m.menu] and switched == ["2"]


def test_refresh_row_is_an_accessible_button():
    view = pv.make_refresh_row_view()
    assert view.isAccessibilityElement()
    assert view.accessibilityRole() == AppKit.NSAccessibilityButtonRole
    assert view.accessibilityLabel() == "Refresh now"
    view.set_busy(True)
    assert view.accessibilityLabel() == "Refreshing\u2026"
    view.set_busy(False)
    assert view.accessibilityLabel() == "Refresh now"


def _row_drawn(monkeypatch, view):
    """(text, colour) for every string the view draws, and whether it drew the selection."""
    texts, selections = [], []
    real_text, real_selection = pv._text, pv._draw_selection

    def text_spy(string, font, color, truncate=None):
        texts.append((string, color))
        return real_text(string, font, color, truncate)

    def selection_spy(bounds):
        selections.append(bounds)
        return real_selection(bounds)

    with monkeypatch.context() as patch:
        patch.setattr(pv, "_text", text_spy)
        patch.setattr(pv, "_draw_selection", selection_spy)
        _render(view)
    return texts, bool(selections)


def _same_colour(a, b):
    return a.isEqual_(b)


def test_refresh_row_draws_its_title_like_a_native_item(monkeypatch):
    view = pv.make_refresh_row_view()
    (text, colour), = _row_drawn(monkeypatch, view)[0]
    assert text == "Refresh now" and _same_colour(colour, AppKit.NSColor.labelColor())
    view.force_highlight = True
    texts, selected = _row_drawn(monkeypatch, view)
    assert selected
    assert texts[0][0] == "Refresh now"
    assert _same_colour(texts[0][1], AppKit.NSColor.selectedMenuItemTextColor())


@pytest.mark.parametrize("highlighted", [False, True])
def test_a_refreshing_row_reads_refreshing_in_the_secondary_colour(monkeypatch, highlighted):
    view = pv.make_refresh_row_view()
    view.set_busy(True)
    view.force_highlight = highlighted
    texts, selected = _row_drawn(monkeypatch, view)
    assert [t for t, _c in texts] == ["Refreshing\u2026"]
    assert _same_colour(texts[0][1], AppKit.NSColor.secondaryLabelColor())
    assert not selected  # an inert row, like a disabled item: no selection


def _in_window(view):
    """Put ``view`` in an offscreen window: AppKit only tracks needsDisplay there."""
    window = AppKit.NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
        view.frame(), AppKit.NSWindowStyleMaskBorderless, AppKit.NSBackingStoreBuffered, False
    )
    window.setReleasedWhenClosed_(False)
    window.contentView().addSubview_(view)
    view.display()  # drawn: nothing pending
    assert not view.needsDisplay()
    return window


def test_set_busy_marks_the_row_for_display():
    view = pv.make_refresh_row_view()
    window = _in_window(view)
    view.set_busy(True)
    assert view.needsDisplay()
    window.close()


def _menu_height(*items):
    menu = AppKit.NSMenu.alloc().init()
    for item in items:
        menu.addItem_(item)
    return menu.size().height


def test_refresh_row_is_exactly_a_native_item_tall():
    # Measured against AppKit itself: a menu holding the row is as tall as
    # one holding a native item in its place.
    def native(title):
        return AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(title, "terminate:", "")

    row_item = AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_("Refresh now", None, "")
    row_item.setView_(pv.make_refresh_row_view())
    assert _menu_height(native("Quit"), row_item) == _menu_height(native("Quit"), native("Refresh now"))


def _first_ink_column(view) -> float:
    """The leftmost column, in points, where the view drew anything."""
    rep = _render(view)
    scale = rep.pixelsWide() / view.bounds().size.width
    for x in range(rep.pixelsWide()):
        for y in range(rep.pixelsHigh()):
            if rep.colorAtX_y_(x, y).alphaComponent() > 0.3:
                return x / scale
    raise AssertionError("nothing drawn")


# Measured live (Oct 22, 2026, 2x capture, bars on, no checkmarks): the row's
# text began 1.5 pt left of the native "Settings" and "Quit" titles.
_LIVE_NATIVE_OFFSET = 1.5


def _ink_offset(text) -> float:
    """How far right of its origin ``text`` in the menu font starts to show ink."""
    view = AppKit.NSView.alloc().initWithFrame_(AppKit.NSMakeRect(0.0, 0.0, 120.0, 24.0))
    rep = view.bitmapImageRepForCachingDisplayInRect_(view.bounds())
    scale = rep.pixelsWide() / view.bounds().size.width
    AppKit.NSGraphicsContext.saveGraphicsState()
    AppKit.NSGraphicsContext.setCurrentContext_(AppKit.NSGraphicsContext.graphicsContextWithBitmapImageRep_(rep))
    pv._text(text, AppKit.NSFont.menuFontOfSize_(0.0), AppKit.NSColor.blackColor()).drawAtPoint_((10.0, 4.0))
    AppKit.NSGraphicsContext.restoreGraphicsState()
    for x in range(rep.pixelsWide()):
        for y in range(rep.pixelsHigh()):
            if rep.colorAtX_y_(x, y).alphaComponent() > 0.3:
                return x / scale - 10.0
    raise AssertionError("nothing drawn")


def test_refresh_row_text_starts_where_native_titles_do():
    # The row's text begins at LEFT_INSET plus the offset measured live, so
    # its ink lines up with the native items above and below it.
    m = _RefreshMenu(lambda: None)
    expected = mp.LEFT_INSET + _LIVE_NATIVE_OFFSET + _ink_offset("Refresh now")
    assert _first_ink_column(m.view) == pytest.approx(expected, abs=0.5)


def test_refresh_row_text_moves_with_the_native_state_column():
    # A checkmark anywhere in the menu reserves a state column that moves
    # every native title right, so the row's text moves by exactly the
    # width AppKit adds for it.
    m = _RefreshMenu(lambda: None)
    plain = _first_ink_column(m.view)
    # The column's width, from a menu of native items alone (in m.menu the
    # wider row sets the menu's width, so the column would not show in it).
    native = AppKit.NSMenu.alloc().init()
    quit_item = AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_("Quit", "terminate:", "")
    native.addItem_(quit_item)
    without = native.size().width
    quit_item.setState_(1)
    column = native.size().width - without
    assert column > 0
    m.quit.setState_(1)
    assert _first_ink_column(m.view) - plain == pytest.approx(column, abs=0.5)


# --- in place: a panel view takes a new model without being rebuilt ------------------

def test_update_panel_view_swaps_the_model_and_redraws():
    panels = _panels()
    layout = pv.PanelLayout(panels)
    view = pv.make_panel_view(panels[0], layout)
    usage = {"five_hour": {"pct": 12.0}, "seven_day": {"pct": 34.0}, "scoped": [{"name": "Fable", "pct": 56.0}]}
    newer = mp.build_account_panel(("1", "user0@example.com", True, usage, usage, None, False, _NOW), _NOW)
    new_layout = pv.PanelLayout([newer, *panels[1:]])
    window = _in_window(view)
    pv.update_panel_view(view, newer, new_layout)
    assert view.panel is newer and view.layout is new_layout
    assert view.needsDisplay()
    assert view.accessibilityLabel() == mp.accessibility_label(newer)
    window.close()


# --- the hop back to the main thread: it must run while a menu is tracking ------------

def _pump_tracking(until, timeout=3.0):
    """Run the main run loop the way an open menu does, until ``until()``."""
    # The running app always has its NSApplication, and it is what makes
    # event tracking one of the common modes; without it nothing queued in
    # the common modes would run in this mode.
    AppKit.NSApplication.sharedApplication()
    deadline = time.monotonic() + timeout
    while not until() and time.monotonic() < deadline:
        AppKit.NSRunLoop.currentRunLoop().runMode_beforeDate_(
            AppKit.NSEventTrackingRunLoopMode, AppKit.NSDate.dateWithTimeIntervalSinceNow_(0.02)
        )
    return until()


def test_a_call_from_a_worker_runs_on_the_main_thread_while_a_menu_tracks():
    AppKit.NSApplication.sharedApplication()
    ran = []
    worker = threading.Thread(
        target=pv.call_on_main, args=(lambda: ran.append(threading.current_thread() is threading.main_thread()),)
    )
    worker.start()
    worker.join()
    assert _pump_tracking(lambda: ran)  # the rumps timers never fire in this mode
    assert ran == [True]


_HOP_SPY: list = []  # an Objective-C class can be defined once per process


def _hop_spy():
    if not _HOP_SPY:
        class _HopSpy(pv.CswapMainThreadCall):
            def performSelectorOnMainThread_withObject_waitUntilDone_modes_(self, selector, arg, wait, modes):
                _HopSpy.posted.append((selector, arg, wait, list(modes)))

        _HOP_SPY.append(_HopSpy)
    spy = _HOP_SPY[0]
    spy.posted = []
    return spy


def test_the_hop_is_queued_in_the_common_run_loop_modes():
    spy = _hop_spy()
    spy.alloc().init().post()
    ((selector, arg, wait, modes),) = spy.posted
    name = selector.decode() if isinstance(selector, bytes) else str(selector)
    assert (name, arg, wait, modes) == ("run:", None, False, [AppKit.NSRunLoopCommonModes])


def test_a_failing_call_on_the_main_thread_is_logged_not_raised(caplog):
    def boom():
        raise RuntimeError("apply failed")

    AppKit.NSApplication.sharedApplication()
    with caplog.at_level(logging.DEBUG, logger="claude-swap"):
        pv.call_on_main(boom)
        assert _pump_tracking(lambda: any(r.exc_info for r in caplog.records))


# --- the Codex block ------------------------------------------------------------------

import dataclasses  # noqa: E402
import math  # noqa: E402
from typing import NamedTuple  # noqa: E402

from claude_swap import codex_usage as cu  # noqa: E402


def _codex_panel(credits=2, credits_age=60.0, alias=None, names_only=False, plan="pro"):
    window = cu.CodexWindow("7d", 56.0, 10080, _NOW + 2 * 86400)
    reading = cu.CodexReading((window,), plan, _NOW - 9, "rollout")
    state = cu.CodexState(True, reading, credits, None if credits is None else _NOW - credits_age, None)
    return mp.build_codex_panel(state, _NOW, alias=alias, names_only=names_only)


def _png(view) -> bytes:
    rep = _render(view)
    return bytes(rep.representationUsingType_properties_(AppKit.NSBitmapImageFileTypePNG, {}))


def _max_alpha(view, rep, x0, y0, x1, y1):
    scale = rep.pixelsWide() / view.bounds().size.width
    return max(
        rep.colorAtX_y_(x, y).alphaComponent()
        for x in range(int(x0 * scale), int(x1 * scale))
        for y in range(int(y0 * scale), int(y1 * scale))
    )


def test_codex_panel_renders_badge_and_no_slot_number():
    import dataclasses

    account = mp.build_account_panel(
        ("4", "a@x.com", False, {"seven_day": {"pct": 30.0}}, None, None, False, _NOW - 60), _NOW
    )
    codex = _codex_panel()
    layout = pv.PanelLayout([account, codex])
    with_badge = pv.make_panel_view(codex, layout)
    without = pv.make_panel_view(dataclasses.replace(codex, badge_text=None), layout)
    assert _png(with_badge) != _png(without)
    # The slot number's column, across the header line: ink for the account,
    # nothing at all for Codex.
    box = (mp.LEFT_INSET, mp.TOP_PAD, mp.LEFT_INSET + mp.NUM_COLUMN, mp.TOP_PAD + layout.header_h)
    account_view = pv.make_panel_view(account, layout)
    assert _max_alpha(account_view, _render(account_view), *box) > 0.1  # tertiary ink
    assert _max_alpha(with_badge, _render(with_badge), *box) == 0.0


def test_badge_states_render_differently():
    # A count, nothing left (no pill at all) and unknown: three pictures.
    panels = [_codex_panel(credits=2), _codex_panel(credits=0), _codex_panel(credits=None)]
    assert [(p.badge_text, p.badge_state) for p in panels] == [("UR 2", "ok"), (None, "ok"), ("UR ?", "unknown")]
    layout = pv.PanelLayout(panels)
    images = [_png(pv.make_panel_view(p, layout)) for p in panels]
    assert len(set(images)) == 3
    # Nothing left draws exactly what a panel with no badge draws.
    bare = dataclasses.replace(panels[0], badge_text=None)
    assert images[1] == _png(pv.make_panel_view(bare, layout))


def test_codex_header_draws_name_plan_badge_and_age_not_its_key(monkeypatch):
    calls = _drawn(monkeypatch, _codex_panel())
    strings = [s for s, _f, _c in calls]
    for text in ("Codex", "pro", "UR 2", "9s ago"):
        assert text in strings
    assert "codex" not in strings  # the key is never drawn as a slot number
    fonts = pv._fonts()
    assert [f for s, f, _c in calls if s == "UR 2"][0].isEqual_(fonts["tag"])  # the ACTIVE tag's font
    assert _is(_colour_of(calls, "UR 2"), "secondaryLabelColor")
    assert _is(_colour_of(calls, "Codex"), "labelColor")
    assert strings.index("UR 2") < strings.index("9s ago")  # the badge, then the age


def test_codex_with_nothing_left_draws_no_pill(monkeypatch):
    calls = _drawn(monkeypatch, _codex_panel(credits=0))
    strings = [s for s, _f, _c in calls]
    assert not any(s.startswith("UR") for s in strings)
    assert "9s ago" in strings  # the rest of the header as before
    for highlighted in (False, True):
        drawn = _drawn(monkeypatch, _codex_panel(credits=0), highlighted=highlighted)
        assert not any(s.startswith("UR") for s, _f, _c in drawn)
    _view, _spots, strokes = _header_geometry(monkeypatch, _codex_panel(credits=0))
    assert strokes == []  # no outline either


def test_no_badge_is_ever_red(monkeypatch):
    # The red pill for a zero count is withdrawn (the owner, Nov 3): nothing
    # left draws nothing at all, neither text nor outline, plain or
    # highlighted; a pill that does draw is the secondary colour, fainter
    # while unknown.
    for spent in (
        _codex_panel(credits=0),
        _account_with_resets(left=0),
        _account_with_resets(left=0, usable=False),
    ):
        _view, spots, strokes = _header_geometry(monkeypatch, spent)
        assert strokes == []
        assert not any(text.startswith("UR") for text in spots)
        for highlighted in (False, True):
            drawn = _drawn(monkeypatch, spent, highlighted=highlighted)
            assert not any(s.startswith("UR") for s, _f, _c in drawn)
    for shown in (
        _codex_panel(credits=2),
        _codex_panel(credits=1),
        _codex_panel(credits=None),
        _account_with_resets(left=2),
        _account_with_resets(left=2, usable=False),
    ):
        (colour,) = [c for s, _f, c in _drawn(monkeypatch, shown) if s.startswith("UR ")]
        assert not _is(colour, "systemRedColor")
    assert "over" not in mp.BADGE_STATES


def test_unknown_badge_is_the_secondary_colour_a_little_fainter(monkeypatch):
    # Not the tertiary colour: that falls below 3:1 on the menu (see the
    # contrast tests). The drawing resolves the colour for its appearance;
    # this test resolves it for the same, the app's own.
    calls = _drawn(monkeypatch, _codex_panel(credits=None))
    drawn = _colour_of(calls, "UR ?").colorUsingColorSpace_(AppKit.NSColorSpace.sRGBColorSpace())
    secondary = AppKit.NSColor.secondaryLabelColor().colorUsingColorSpace_(AppKit.NSColorSpace.sRGBColorSpace())
    assert 0.0 < pv.UNKNOWN_BADGE_ALPHA < 1.0
    for part in ("redComponent", "greenComponent", "blueComponent"):
        assert getattr(drawn, part)() == pytest.approx(getattr(secondary, part)())
    assert drawn.alphaComponent() == pytest.approx(secondary.alphaComponent() * pv.UNKNOWN_BADGE_ALPHA)


def test_highlighted_badge_uses_the_selection_colour(monkeypatch):
    for credits in (2, None):
        calls = _drawn(monkeypatch, _codex_panel(credits=credits), highlighted=True)
        (badge,) = [c for s, _f, c in calls if s.startswith("UR ")]
        assert _is(badge, "selectedMenuItemTextColor")


def test_badge_pill_is_stroked_in_the_badge_colour(monkeypatch):
    strokes = []
    real = pv._stroke_rounded

    def spy(rect, radius, color, *width):
        strokes.append((rect, color))
        return real(rect, radius, color, *width)

    monkeypatch.setattr(pv, "_stroke_rounded", spy)
    for credits, text in ((2, "UR 2"), (None, "UR ?")):
        panel = _codex_panel(credits=credits)
        strokes.clear()
        calls = _drawn(monkeypatch, panel)  # one render: the stroke and the text it outlines
        (rect, color) = strokes[-1]
        assert color.isEqual_(_colour_of(calls, text))  # the outline is the text's colour
        x, y, w, h = rect
        assert w > pv._width(text, pv._fonts()["tag"]) + mp.BADGE_PAD_X  # the text has room inside


class _Drawn(NamedTuple):
    font: object
    x: float
    y: float
    in_rect: bool  # drawn into a rect, so cut to it; a whole string is drawn at a point


class _Spot:
    """A string to draw that notes where and how it was drawn, by string."""

    def __init__(self, real, string, font, log):
        self._real, self._string, self._font, self._log = real, string, font, log

    def size(self):
        return self._real.size()

    def drawAtPoint_(self, point):
        self._log[self._string] = _Drawn(self._font, point[0], point[1], False)
        self._real.drawAtPoint_(point)

    def drawInRect_(self, rect):
        self._log[self._string] = _Drawn(self._font, rect.origin.x, rect.origin.y, True)
        self._real.drawInRect_(rect)


def _geometry(monkeypatch, view):
    """Draw ``view`` once: where each string went, and the pill strokes.

    Each stroke is (rect, radius, line width), the width None when the
    drawing passed none.
    """
    spots, strokes = {}, []
    real_text, real_stroke = pv._text, pv._stroke_rounded

    def text_spy(string, font, color, truncate=None):
        return _Spot(real_text(string, font, color, truncate), string, font, spots)

    def stroke_spy(rect, radius, color, *width):
        strokes.append((rect, radius, width[0] if width else None))
        return real_stroke(rect, radius, color, *width)

    with monkeypatch.context() as patch:
        patch.setattr(pv, "_text", text_spy)
        patch.setattr(pv, "_stroke_rounded", stroke_spy)
        _render(view)
    return spots, strokes


def _header_geometry(monkeypatch, panel, layout=None):
    """``_geometry`` for a new view of ``panel``; returns the view first."""
    view = pv.make_panel_view(panel, layout or pv.PanelLayout([panel]))
    spots, strokes = _geometry(monkeypatch, view)
    return view, spots, strokes


def _changed_rows(a, b):
    """First and last pixel rows where two same-sized renders differ."""
    stride = a.bytesPerRow()
    pa, pb = bytes(a.bitmapData()), bytes(b.bitmapData())
    rows = [r for r in range(a.pixelsHigh()) if pa[r * stride:(r + 1) * stride] != pb[r * stride:(r + 1) * stride]]
    return rows[0], rows[-1]


def _on_grid(value, pixel):
    return value / pixel == pytest.approx(round(value / pixel))


def test_badge_pill_renders_no_taller_than_the_tag_line_inside_the_header():
    # Measured on the pixels, not the arithmetic: the rows the badge adds
    # are its pill and its text.
    codex = _codex_panel()
    layout = pv.PanelLayout([codex])
    view = pv.make_panel_view(codex, layout)
    shown = _render(view)
    bare = _render(pv.make_panel_view(dataclasses.replace(codex, badge_text=None), layout))
    scale = shown.pixelsWide() / view.bounds().size.width
    first, last = _changed_rows(shown, bare)
    tag_h = pv._line_h(pv._fonts()["tag"])
    assert (last + 1 - first) / scale <= tag_h + 2.0  # the ACTIVE tag's line and a point each side
    assert first / scale >= mp.TOP_PAD
    assert (last + 1) / scale <= mp.TOP_PAD + layout.header_h  # never taller than the header line


def test_badge_text_is_in_the_tag_font_on_the_age_text_baseline(monkeypatch):
    _view, spots, _strokes = _header_geometry(monkeypatch, _codex_panel())
    badge, age = spots["UR 2"], spots["9s ago"]
    assert badge.font.isEqual_(pv._fonts()["tag"])  # the ACTIVE tag's cap height
    assert badge.y + badge.font.ascender() == pytest.approx(age.y + age.font.ascender())


def test_badge_pill_is_a_one_pixel_outline_round_its_text_an_age_gap_before_the_age(monkeypatch):
    view, spots, strokes = _header_geometry(monkeypatch, _codex_panel())
    pixel = view.convertSizeFromBacking_((1.0, 1.0)).width
    (((x, y, w, h), radius, width),) = strokes
    assert (radius, width) == (mp.BADGE_RADIUS, pixel)  # one device pixel: a hairline on Retina
    badge = spots["UR 2"]
    text_w = pv._width("UR 2", badge.font)
    # The padding and the gap hold to the nearest pixel, where the outline is snapped.
    assert badge.x - x == pytest.approx(mp.BADGE_PAD_X, abs=pixel / 2)
    assert x + w - (badge.x + text_w) == pytest.approx(mp.BADGE_PAD_X, abs=pixel / 2)
    assert badge.y - y == pytest.approx(mp.BADGE_PAD_Y, abs=pixel / 2)
    assert spots["9s ago"].x - (x + w) == pytest.approx(mp.AGE_GAP, abs=pixel / 2)


@pytest.mark.parametrize("scale", [1, 1.5, 2, 3])
@pytest.mark.parametrize("credits, text", [(1, "UR 1"), (None, "UR ?"), (12, "UR 12"), (999, "UR 999")])
def test_badge_geometry_by_state_and_scale(monkeypatch, credits, text, scale):
    pixel = 1.0 / scale
    monkeypatch.setattr(pv, "_device_pixel", lambda _view: pixel)
    view, spots, strokes = _header_geometry(monkeypatch, _codex_panel(credits=credits))
    (((x, y, w, h), _radius, width),) = strokes
    assert width == pixel
    for edge in (x, y, x + w, y + h):
        assert _on_grid(edge, pixel)
    line_top, line_bottom = mp.TOP_PAD, mp.TOP_PAD + view.layout.header_h
    assert line_top - 1e-9 <= y and y + h <= line_bottom + 1e-9  # inside the header line
    badge = spots[text]
    assert x <= badge.x and badge.x + pv._width(text, badge.font) <= x + w  # the text inside its pill
    assert spots["9s ago"].x - (x + w) == pytest.approx(mp.AGE_GAP, abs=pixel / 2)


def test_badge_header_containment_with_oversized_font(monkeypatch):
    panel = _codex_panel()
    layout = pv.PanelLayout([panel])
    layout.fonts["tag"] = AppKit.NSFont.systemFontOfSize_weight_(30.0, AppKit.NSFontWeightSemibold)
    assert pv._line_h(layout.fonts["tag"]) + 2 * mp.BADGE_PAD_Y > layout.header_h  # it cannot fit
    _view, _spots, strokes = _header_geometry(monkeypatch, panel, layout)
    (((_x, y, _w, h), _radius, _width),) = strokes
    assert h <= layout.header_h
    assert mp.TOP_PAD <= y and y + h <= mp.TOP_PAD + layout.header_h


# --- the pill as rasterised ----------------------------------------------------------

def _render_at(view, scale, appearance):
    """``view`` drawn at ``scale`` pixels a point over its appearance's window background.

    The drawing reads its pixel size from AppKit, which answers for the
    main screen when a view has no window; a caller drawing at another
    scale tells the drawing the scale it draws at.
    """
    bounds = view.bounds()
    rep = AppKit.NSBitmapImageRep.alloc().initWithBitmapDataPlanes_pixelsWide_pixelsHigh_bitsPerSample_samplesPerPixel_hasAlpha_isPlanar_colorSpaceName_bytesPerRow_bitsPerPixel_(
        None, round(bounds.size.width * scale), round(bounds.size.height * scale),
        8, 4, True, False, AppKit.NSDeviceRGBColorSpace, 0, 0,
    )
    rep.setSize_(bounds.size)
    name = AppKit.NSAppearanceNameDarkAqua if appearance == "darkAqua" else AppKit.NSAppearanceNameAqua
    look = AppKit.NSAppearance.appearanceNamed_(name)
    view.setAppearance_(look)
    previous = AppKit.NSAppearance.currentDrawingAppearance()
    AppKit.NSAppearance.setCurrentAppearance_(look)
    try:
        AppKit.NSGraphicsContext.saveGraphicsState()
        AppKit.NSGraphicsContext.setCurrentContext_(AppKit.NSGraphicsContext.graphicsContextWithBitmapImageRep_(rep))
        AppKit.NSColor.windowBackgroundColor().setFill()
        AppKit.NSBezierPath.fillRect_(bounds)
        AppKit.NSGraphicsContext.restoreGraphicsState()
        view.cacheDisplayInRect_toBitmapImageRep_(bounds, rep)
    finally:
        AppKit.NSAppearance.setCurrentAppearance_(previous)
    return rep


class _Pixels:
    def __init__(self, rep):
        self.width, self.height = rep.pixelsWide(), rep.pixelsHigh()
        self._stride, self._step = rep.bytesPerRow(), rep.bitsPerPixel() // 8
        self._data = bytes(rep.bitmapData())

    def at(self, col, row):
        i = row * self._stride + col * self._step
        return tuple(self._data[i:i + 3])


def _badge_pixels(monkeypatch, credits, scale, appearance):
    """The Codex panel drawn at ``scale`` with its badge, and without: the badge's pixels.

    Returns (shown, bare, changed): both renders and the set of (col, row)
    the badge changed.
    """
    monkeypatch.setattr(pv, "_device_pixel", lambda _view: 1.0 / scale)
    panel = _codex_panel(credits=credits)
    layout = pv.PanelLayout([panel])
    shown = _Pixels(_render_at(pv.make_panel_view(panel, layout), scale, appearance))
    bare = _Pixels(_render_at(pv.make_panel_view(dataclasses.replace(panel, badge_text=None), layout), scale, appearance))
    changed = {
        (c, r) for r in range(shown.height) for c in range(shown.width) if shown.at(c, r) != bare.at(c, r)
    }
    return shown, bare, changed


@pytest.mark.parametrize("scale", [1, 2])
@pytest.mark.parametrize("appearance", ["darkAqua", "aqua"])
def test_badge_stroke_pixels_by_appearance_and_scale(monkeypatch, appearance, scale):
    shown, _bare, changed = _badge_pixels(monkeypatch, 2, scale, appearance)
    assert changed  # the badge is drawn at all
    left, right = min(c for c, _r in changed), max(c for c, _r in changed)
    top, bottom = min(r for _c, r in changed), max(r for _c, r in changed)
    mid_col, mid_row = (left + right) // 2, (top + bottom) // 2

    def thickness(cells):  # inked pixels from the outline inward, up to the padding
        count = 0
        for cell in cells:
            if cell not in changed:
                break
            count += 1
        return count

    # One device pixel on each side, and each a full pixel of one colour:
    # a line between two pixels would ink two, each at half strength.
    assert thickness((mid_col, r) for r in range(top, bottom)) == 1
    assert thickness((mid_col, r) for r in range(bottom, top, -1)) == 1
    assert thickness((c, mid_row) for c in range(left, right)) == 1
    assert thickness((c, mid_row) for c in range(right, left, -1)) == 1
    edges = {shown.at(mid_col, top), shown.at(mid_col, bottom), shown.at(left, mid_row), shown.at(right, mid_row)}
    assert len(edges) == 1


def _contrast(a, b):
    """WCAG contrast ratio of two sRGB colours, channels 0 to 255."""
    def luminance(rgb):
        def channel(v):
            v /= 255.0
            return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4
        r, g, b = (channel(v) for v in rgb)
        return 0.2126 * r + 0.7152 * g + 0.0722 * b

    hi, lo = sorted((luminance(a), luminance(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


# WCAG 2.2 success criterion 1.4.11 (non-text contrast): a control's
# outline, or state shown by graphics, reads at 3:1 against what is behind it.
BADGE_MIN_CONTRAST = 3.0


def _badge_contrast(monkeypatch, credits, appearance):
    """The strongest contrast a badge pixel makes with the panel behind it, at 2x."""
    shown, bare, changed = _badge_pixels(monkeypatch, credits, 2, appearance)
    return max(_contrast(shown.at(c, r), bare.at(c, r)) for c, r in changed)


@pytest.mark.parametrize("appearance", ["darkAqua", "aqua"])
def test_unknown_badge_contrast(monkeypatch, appearance):
    unknown = _badge_contrast(monkeypatch, None, appearance)
    assert unknown >= BADGE_MIN_CONTRAST
    assert unknown < _badge_contrast(monkeypatch, 2, appearance)  # still fainter than a known count


@pytest.mark.parametrize("appearance", ["darkAqua", "aqua"])
@pytest.mark.parametrize("credits", [2, 12])  # a zero draws no pill to measure
def test_known_badges_meet_the_same_contrast(monkeypatch, credits, appearance):
    assert _badge_contrast(monkeypatch, credits, appearance) >= BADGE_MIN_CONTRAST


def test_badge_renders_without_window_and_with_zero_conversion(monkeypatch):
    failures = []
    panel = _codex_panel()
    layout = pv.PanelLayout([panel])

    def view():
        return pv.make_panel_view(panel, layout, on_draw_failure=lambda: failures.append(panel))

    # No window: AppKit answers for the main screen, and the outline is that pixel.
    windowless = view()
    assert windowless.window() is None
    spots, strokes = _geometry(monkeypatch, windowless)
    ((_rect, _radius, width),) = strokes
    assert width == windowless.convertSizeFromBacking_((1.0, 1.0)).width
    assert "UR 2" in spots

    # No usable answer: a whole point stands in for the pixel, and the badge is still drawn.
    for answer in (AppKit.NSMakeSize(0.0, 0.0), None, AppKit.NSMakeSize(math.nan, math.nan),
                   AppKit.NSMakeSize(-1.0, -1.0), AppKit.NSMakeSize(math.inf, math.inf)):
        broken = view()
        broken.convertSizeFromBacking_ = lambda _size, answer=answer: answer
        spots, strokes = _geometry(monkeypatch, broken)
        (((x, y, w, h), _radius, width),) = strokes
        assert width == 1.0
        assert all(edge == round(edge) for edge in (x, y, x + w, y + h))
        assert "UR 2" in spots
    assert failures == []  # never the plain text fallback


def test_smaller_badge_restores_name_space_at_truncation_boundary(monkeypatch):
    # The pill before it became a tag: the meta font and five points at each
    # end. A name that the old pill cut is drawn whole now.
    fonts = pv._fonts()
    age_w = pv._width("9s ago", fonts["meta"])
    old_meta_w = pv._width("UR 2", fonts["meta"]) + 2 * 5.0 + mp.AGE_GAP + age_w
    new_meta_w = pv._width("UR 2", fonts["tag"]) + 2 * mp.BADGE_PAD_X + mp.AGE_GAP + age_w
    cols = pv.PanelLayout([_codex_panel()]).columns

    def room(meta_w):  # the widest name the header keeps whole
        return mp.header_spans(cols, 10_000.0, meta_w)[1]

    name = next(
        s for s in ("n" * a + "i" * b for a in range(1, 60) for b in range(4))
        if room(old_meta_w) < pv._width(s, fonts["title"]) <= room(new_meta_w)
    )
    panel = _codex_panel(alias=name, names_only=True)  # names only: no plan type beside it
    _view, spots, _strokes = _header_geometry(monkeypatch, panel)
    assert not spots[name].in_rect  # drawn whole at its point, not cut into a rect


def test_codex_panel_does_not_widen_the_gutter():
    account = mp.build_account_panel(
        ("1", "a@x.com", False, {"seven_day": {"pct": 30.0}}, None, None, False, _NOW - 60), _NOW
    )
    assert pv.PanelLayout([account, _codex_panel()]).columns == pv.PanelLayout([account]).columns


def test_codex_panel_is_not_a_switch(run_now, on_screen, switched):
    menu = _Menu([_codex_panel()], switched.append)
    view = menu.views[0]
    assert view.accessibilityRole() != AppKit.NSAccessibilityButtonRole
    assert view.accessibilityLabel().startswith("Codex, pro")
    assert not view.accessibilityPerformPress()
    view.force_highlight = True
    view.mouseUp_(_mouse_up(view))
    view.keyDown_(_key_event(36))
    assert switched == []


def test_codex_block_renders_in_both_appearances(tmp_path):
    account = mp.build_account_panel(
        ("1", "a@x.com", True, {"five_hour": {"pct": 30.0}}, None, None, False, _NOW - 60), _NOW
    )
    for appearance in ("aqua", "darkAqua"):
        for highlighted in (False, True):
            path = tmp_path / f"{appearance}-{highlighted}.png"
            pv.render_png([account, _codex_panel(alias="a-twelve-chr")], str(path), appearance, highlighted=highlighted)
            assert path.stat().st_size > 1000


# --- the UR pill on a Claude account -----------------------------------------------------

def _account_with_resets(left=2, active=True, usable=True, disabled=False):
    usage = {
        "five_hour": {"pct": 12.0}, "seven_day": {"pct": 40.0},
        "limit_resets": {
            "grants": [{
                "id": "g", "label": "Free limit reset", "left": left, "total": max(left, 1),
                "ends_at": "2099-01-01T00:00:00+00:00", "usable_now": usable, "paused": False,
            }],
            "eligible": True, "fetched_at": _NOW - 60,
        },
    }
    return mp.build_account_panel(("1", "me@example.com", active, usage, usage, "proton", disabled, _NOW - 9), _NOW)


def test_account_pill_draws_before_active_then_the_age(monkeypatch):
    view, spots, strokes = _header_geometry(monkeypatch, _account_with_resets())
    pixel = view.convertSizeFromBacking_((1.0, 1.0)).width
    (((x, y, w, h), radius, width),) = strokes  # one pill, the account's
    assert (radius, width) == (mp.BADGE_RADIUS, pixel)
    badge, active, age = spots["UR 2"], spots["ACTIVE"], spots["9s ago"]
    assert badge.x < active.x < age.x  # the pill, then ACTIVE, then the age
    assert badge.font.isEqual_(pv._fonts()["tag"])
    assert badge.x - x == pytest.approx(mp.BADGE_PAD_X, abs=pixel / 2)
    assert active.x - (x + w) == pytest.approx(mp.AGE_GAP, abs=pixel / 2)  # one gap to ACTIVE
    assert badge.y + badge.font.ascender() == pytest.approx(age.y + age.font.ascender())


def test_account_pill_has_the_codex_pills_geometry_and_the_codex_pill_stays(monkeypatch):
    account, codex = _account_with_resets(left=2), _codex_panel(credits=2)
    layout = pv.PanelLayout([account, codex])
    view, a_spots, a_strokes = _header_geometry(monkeypatch, account, layout)
    _v, c_spots, c_strokes = _header_geometry(monkeypatch, codex, layout)
    pixel = view.convertSizeFromBacking_((1.0, 1.0)).width
    ((a_rect, a_radius, a_width),) = a_strokes
    ((c_rect, c_radius, c_width),) = c_strokes
    assert (a_rect[1], a_rect[2], a_rect[3]) == (c_rect[1], c_rect[2], c_rect[3])  # same y, width, height
    assert (a_radius, a_width) == (c_radius, c_width)
    assert a_spots["UR 2"].y - a_rect[1] == pytest.approx(c_spots["UR 2"].y - c_rect[1])
    # Each pill's left edge is snapped to its own device pixel: the text
    # inset agrees to within half a pixel, as the Codex tests hold it.
    assert a_spots["UR 2"].x - a_rect[0] == pytest.approx(c_spots["UR 2"].x - c_rect[0], abs=pixel / 2)
    # The Codex pill is where it was: its right end one gap before the age,
    # the age at the right edge of the header.
    cols = layout.columns
    age = c_spots["9s ago"]
    assert age.x + pv._width("9s ago", pv._fonts()["meta"]) == pytest.approx(cols.reset_right)
    assert age.x - (c_rect[0] + c_rect[2]) == pytest.approx(mp.AGE_GAP, abs=0.5)


def test_account_without_a_grant_draws_no_pill(monkeypatch):
    plain = mp.build_account_panel(
        ("1", "me@example.com", True, {"five_hour": {"pct": 12.0}}, None, "proton", False, _NOW - 9), _NOW
    )
    _view, spots, strokes = _header_geometry(monkeypatch, plain)
    assert strokes == []
    assert not any(text.startswith("UR ") for text in spots)


def test_account_pill_before_a_held_or_paused_word(monkeypatch):
    _view, spots, _strokes = _header_geometry(monkeypatch, _account_with_resets(active=False, disabled=True))
    assert spots["UR 2"].x < spots["paused"].x < spots["9s ago"].x


def test_account_pill_colours(monkeypatch):
    assert _is(_colour_of(_drawn(monkeypatch, _account_with_resets(left=2)), "UR 2"), "secondaryLabelColor")
    dim = _colour_of(_drawn(monkeypatch, _account_with_resets(usable=False)), "UR 2")
    secondary = AppKit.NSColor.secondaryLabelColor().colorUsingColorSpace_(AppKit.NSColorSpace.sRGBColorSpace())
    resolved = dim.colorUsingColorSpace_(AppKit.NSColorSpace.sRGBColorSpace())
    assert resolved.alphaComponent() == pytest.approx(secondary.alphaComponent() * pv.UNKNOWN_BADGE_ALPHA)
    highlighted = _drawn(monkeypatch, _account_with_resets(left=2), highlighted=True)
    assert _is(_colour_of(highlighted, "UR 2"), "selectedMenuItemTextColor")


@pytest.mark.parametrize("usable", [True, False])
def test_account_with_nothing_left_draws_no_pill(monkeypatch, usable):
    spent = _account_with_resets(left=0, usable=usable)
    _view, spots, strokes = _header_geometry(monkeypatch, spent)
    assert strokes == []
    assert not any(text.startswith("UR") for text in spots)
    assert "ACTIVE" in spots  # the state words as before
    highlighted = _drawn(monkeypatch, spent, highlighted=True)
    assert not any(s.startswith("UR") for s, _f, _c in highlighted)
