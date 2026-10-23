"""AppKit drawing for the menu bar usage bars (macOS, menubar extra only).

Turns an ``AccountPanel`` model (``menubar_panel``) into a view that an
``NSMenuItem`` shows in place of its title. There is no data logic here: the
model decides every number and state, and ``menubar_panel`` decides every
position. This module only measures fonts, picks semantic colours and draws.
It also draws the Refresh now row, a native-looking item whose press keeps
the menu open while the panels above it redraw in place.

Only semantic ``NSColor``s and the system font are used, so light mode, dark
mode and Increase Contrast all follow the system with nothing hard-coded.
Imported lazily by the rumps glue, like rumps itself.
"""

from __future__ import annotations

import logging
import math

import objc
from AppKit import (
    NSAccessibilityButtonRole,
    NSAppearance,
    NSAppearanceNameAqua,
    NSAppearanceNameDarkAqua,
    NSAttributedString,
    NSBezierPath,
    NSBitmapImageFileTypePNG,
    NSColor,
    NSCompositingOperationClear,
    NSFont,
    NSFontAttributeName,
    NSFontWeightMedium,
    NSFontWeightRegular,
    NSFontWeightSemibold,
    NSForegroundColorAttributeName,
    NSLineBreakByTruncatingMiddle,
    NSLineBreakByTruncatingTail,
    NSMenu,
    NSMenuItem,
    NSMouseInRect,
    NSMutableParagraphStyle,
    NSObject,
    NSParagraphStyleAttributeName,
    NSRectFillUsingOperation,
    NSView,
    NSViewWidthSizable,
)
from Foundation import NSMakeRect, NSRunLoopCommonModes
from PyObjCTools import AppHelper

from claude_swap import menubar_panel as mp

_LIVE_VIEWS = [0]  # views alive right now; see live_view_count()
_ACTIVATE_KEYS = (36, 76)  # Return, keypad Enter

CARD_ALPHA = 0.16  # the active account's accent card
EXHAUSTED_FILL_ALPHA = 0.62  # an exhausted account's bar fills
EXHAUSTED_OVER_ALPHA = 0.85  # an exhausted account's red over-limit percent
TICK_ALPHA = 0.55  # the pace tick, label colour


def live_view_count() -> int:
    """How many panel views exist, for the rebuild leak check."""
    return _LIVE_VIEWS[0]


# ---- fonts and colours -------------------------------------------------------------

def _fonts() -> dict:
    menu = NSFont.menuFontOfSize_(0.0).pointSize()
    small = NSFont.smallSystemFontSize()
    return {
        "title": NSFont.systemFontOfSize_weight_(menu, NSFontWeightMedium),
        "title_active": NSFont.systemFontOfSize_weight_(menu, NSFontWeightSemibold),
        "label": NSFont.systemFontOfSize_(small),
        "digits": NSFont.monospacedDigitSystemFontOfSize_weight_(small, NSFontWeightRegular),
        "num": NSFont.monospacedDigitSystemFontOfSize_weight_(small, NSFontWeightMedium),
        "email": NSFont.systemFontOfSize_(small - 1.0),
        "meta": NSFont.systemFontOfSize_(small),
        "tag": NSFont.systemFontOfSize_weight_(small - 2.0, NSFontWeightSemibold),
    }


def _fill_color(state: str):
    # The usage bands as system colours: green, yellow while climbing, red
    # from the red edge ("crit") and at the limit ("over").
    if state in ("crit", "over"):
        return NSColor.systemRedColor()
    if state == "warn":
        return NSColor.systemYellowColor()
    return NSColor.systemGreenColor()


def _line_h(font) -> float:
    return mp.line_height(font.ascender(), font.descender(), font.leading())


def _text(string: str, font, color, truncate=None):
    attrs = {NSFontAttributeName: font, NSForegroundColorAttributeName: color}
    if truncate is not None:
        style = NSMutableParagraphStyle.alloc().init()
        style.setLineBreakMode_(truncate)
        attrs[NSParagraphStyleAttributeName] = style
    return NSAttributedString.alloc().initWithString_attributes_(string, attrs)


def _width(string: str, font) -> float:
    return _text(string, font, NSColor.labelColor()).size().width


def _fill_rounded(rect, radius: float, color) -> None:
    color.setFill()
    NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(NSMakeRect(*rect), radius, radius).fill()


# ---- layout shared by every panel in one menu -----------------------------------------

class PanelLayout:
    """Fonts, heights and columns shared by all account panels in a menu.

    Built once per menu rebuild from every panel, so columns line up across
    accounts: the label column fits the widest label, and the slot number
    gutter the widest number.
    """

    LABEL_MAX = 64.0  # a long model name is truncated rather than eating the bar

    def __init__(self, panels):
        self.fonts = _fonts()
        self.width = mp.PANEL_WIDTH
        self.header_h = _line_h(self.fonts["title"])
        self.row_h = _line_h(self.fonts["digits"]) + 3.0
        rows = [row for panel in panels for row in panel.rows if row.state != "unknown"]
        label_w = max((_width(r.label, self.fonts["label"]) for r in rows), default=0.0)
        reset_w = max(
            [_width("00h 00m", self.fonts["digits"])]
            + [_width(r.reset_text, self.fonts["digits"]) for r in rows]
        )
        num_w = max((_width(str(p.num), self.fonts["num"]) for p in panels), default=0.0)
        self._widths = dict(
            label_w=min(self.LABEL_MAX, float(label_w)),
            pct_w=_width("100%", self.fonts["digits"]),
            reset_w=float(reset_w),
            num_w=float(num_w),
        )
        self.columns = self.columns_for(self.width)

    def columns_for(self, width: float):
        """Columns for a view ``width`` wide (the menu's width), never below the fixed width."""
        return mp.row_columns(width=max(self.width, float(width)), **self._widths)

    def height(self, panel) -> float:
        return mp.panel_height(len(panel.rows), self.header_h, self.row_h)


# ---- activation: one stable target, never rumps' per-item registry ----------------

class CswapAccountTarget(NSObject):
    """Where a panel's or the Refresh now row's click, Return or accessibility press lands.

    rumps looks up a menu item's Python callback when the action is
    dispatched, and ``rebuild_menu`` purges those entries; an activation
    queued before a rebuild would find nothing and silently not switch.
    This target is created once per app and never purged. It accepts a
    request only from a panel that is on screen in the app's current menu
    while that menu is open, so a panel held by an accessibility client
    after dismissal, or left over from an earlier rebuild, cannot switch
    anything. The switch itself is queued with just the account number,
    which the handler checks against the snapshot current when it runs.
    """

    @objc.python_method
    def activate_panel(self, view) -> bool:
        log = logging.getLogger("claude-swap")
        is_open = getattr(self, "is_open", None)
        if is_open is None or not is_open():
            log.debug("usage panel press ignored: the menu is not open")
            return False
        menu = getattr(self, "menu", None)
        if view.panel is None or not _attached(view, menu):
            log.debug("usage panel press ignored: the panel is not in the open menu")
            return False
        _cancel_tracking(menu)
        # After tracking unwinds, so an alert raised by the switch is not
        # shown on top of a menu that is still closing.
        AppHelper.callAfter(self._dispatch, view.panel.num)
        return True

    @objc.python_method
    def activate_refresh(self, view) -> bool:
        """Refresh now from the open menu, which stays open.

        The same checks as a panel press. Unlike a panel, tracking is not
        cancelled and nothing waits for it to unwind: the app's handler runs
        now, during tracking, and only marks the row and starts a reading.
        """
        log = logging.getLogger("claude-swap")
        is_open = getattr(self, "is_open", None)
        if is_open is None or not is_open():
            log.debug("refresh press ignored: the menu is not open")
            return False
        if not _attached(view, getattr(self, "menu", None)):
            log.debug("refresh press ignored: the row is not in the open menu")
            return False
        handler = getattr(self, "on_refresh", None)
        if handler is None:
            return False
        try:
            handler()
        except Exception:  # never raise out of an AppKit callback
            log.debug("Menu bar: Refresh now failed", exc_info=True)
            return False
        return True

    @objc.python_method
    def _dispatch(self, number) -> None:
        handler = getattr(self, "handler", None)
        if handler is None:
            return
        try:
            handler(str(number))
        except Exception:  # never raise out of an AppKit callback
            logging.getLogger("claude-swap").warning(
                "Menu bar: switching from a usage panel failed", exc_info=True
            )


def make_account_target(handler, is_open=None, menu=None, on_refresh=None):
    """The app's long-lived activation target.

    ``handler(number)`` performs the switch; ``on_refresh()`` starts the
    Refresh now reading; ``is_open()`` reports whether ``menu`` (the app's
    root menu) is on screen. Without both, every request is refused.
    """
    target = CswapAccountTarget.alloc().init()
    target.handler = handler
    target.is_open = is_open
    target.menu = menu
    target.on_refresh = on_refresh
    return target


def _on_screen(view) -> bool:
    return view.window() is not None


def _attached(view, menu) -> bool:
    """Whether ``view`` is a panel currently shown in ``menu``."""
    if menu is None or not _on_screen(view):
        return False
    item = view.enclosingMenuItem()
    owner = item.menu() if item is not None else None  # None once removed
    return owner is not None and owner == menu


def _cancel_tracking(menu) -> None:
    if menu is not None:
        menu.cancelTracking()


# ---- the views ---------------------------------------------------------------------

class CswapMenuRowView(NSView):
    """A menu row this module draws, acting like the native item it replaces.

    Shared by the account panels and the Refresh now row: the highlight is
    the item's own highlight state, and a mouse-up inside the highlighted
    row, Return while the menu delegate has given the row focus, or an
    accessibility press activates it. What activating does is the
    subclass's ``_activate``.
    """

    HIGHLIGHT_INSET = 5.0  # matches the native selection's inset from the menu edge
    HIGHLIGHT_RADIUS = 8.0

    def initWithFrame_(self, frame):
        self = objc.super(CswapMenuRowView, self).initWithFrame_(frame)
        if self is None:
            return None
        self.account_target = None  # CswapAccountTarget
        self.focus_allowed = False  # granted by the menu delegate on highlight
        self.force_highlight = False  # offscreen rendering only
        return self

    def isFlipped(self):
        return True

    def _highlighted(self) -> bool:
        if self.force_highlight:
            return True
        item = self.enclosingMenuItem()
        return bool(item is not None and item.isHighlighted())

    # -- activation: behave like the plain item the row replaces -----------------
    def _activate(self) -> bool:
        return False

    def mouseUp_(self, event):
        # Like a native item: a release outside the row (press, drag away,
        # let go) or on a row the menu no longer highlights chooses nothing.
        if event is None:
            return
        point = self.convertPoint_fromView_(event.locationInWindow(), None)
        if not NSMouseInRect(point, self.bounds(), self.isFlipped()):
            return
        if not self._highlighted():
            return
        self._activate()

    def acceptsFirstResponder(self):
        # Only while the menu has this row highlighted. Accepting always
        # made the first panel the menu window's initial focus, so the menu
        # opened with an account selected and Return would switch to it.
        return bool(self.focus_allowed)

    def keyDown_(self, event):
        # AppKit does not send a view item's action on Return; the menu
        # delegate makes the highlighted row first responder so it can.
        # Only the highlighted row acts. Every other key is left alone: menu
        # tracking moves the selection and handles Escape itself (the same
        # with or without forwarding, checked live), and a key forwarded up
        # the responder chain would end at NSResponder's default, a beep.
        if event.keyCode() in _ACTIVATE_KEYS and self._highlighted():
            self._activate()

    def accessibilityPerformPress(self):
        return self._activate()


class CswapUsagePanelView(CswapMenuRowView):
    """One account block: header line and one bar per usage window."""

    def initWithFrame_(self, frame):
        self = objc.super(CswapUsagePanelView, self).initWithFrame_(frame)
        if self is None:
            return None
        _LIVE_VIEWS[0] += 1
        self.panel = None
        self.layout = None
        self.on_draw_failure = None  # called when drawing raises
        self.is_last = False  # the menu's last account row: a separator follows, so no hairline
        return self

    def dealloc(self):
        _LIVE_VIEWS[0] -= 1
        objc.super(CswapUsagePanelView, self).dealloc()

    def _activate(self) -> bool:
        """Ask the app's target to switch to this panel's account."""
        target = self.account_target
        return bool(target is not None and target.activate_panel(self))

    # -- drawing ----------------------------------------------------------------
    def drawRect_(self, dirty):
        # Drawing runs outside any caller's error handling, so a failure here
        # is caught here: show the plain text row and tell the app, which
        # switches the next rebuild to text rows.
        try:
            _draw_panel(self, dirty)
        except Exception:
            try:
                _draw_fallback(self)
            except Exception:
                pass
            handler = self.on_draw_failure
            if handler is not None:
                try:
                    handler()
                except Exception:
                    pass


class _Ink:
    """The text colours for one panel draw.

    ``strong`` is the percentages: the label colour, muted to secondary for
    an exhausted account. ``name`` is muted for an exhausted or a disabled
    (paused or held) account. Highlighted, every text is the selection's
    text colour.
    """

    def __init__(self, highlighted: bool, exhausted: bool, disabled: bool):
        self.highlighted = highlighted
        self.dim = exhausted and not highlighted
        if highlighted:
            self.primary = self.secondary = self.tertiary = NSColor.selectedMenuItemTextColor()
        else:
            self.primary = NSColor.labelColor()
            self.secondary = NSColor.secondaryLabelColor()
            self.tertiary = NSColor.tertiaryLabelColor()
        self.strong = self.secondary if self.dim else self.primary
        self.name = self.secondary if (exhausted or disabled) else self.primary


def _draw_panel(view, dirty) -> None:
    panel, layout = view.panel, view.layout
    if panel is None or layout is None:
        return
    highlighted = view._highlighted()
    bounds = view.bounds()
    width, height = bounds.size.width, bounds.size.height
    cols = layout.columns_for(width)
    ink = _Ink(highlighted, panel.exhausted, panel.disabled)

    # The active account sits on an accent card; the selection replaces it.
    if highlighted:
        _draw_selection(bounds)
    elif panel.is_active:
        _fill_rounded(
            mp.card_rect(width, height), mp.CARD_RADIUS,
            NSColor.controlAccentColor().colorWithAlphaComponent_(CARD_ALPHA),
        )

    _draw_header(panel, layout, cols, ink)
    y = mp.TOP_PAD + layout.header_h + mp.HEADER_GAP
    for row in panel.rows:
        _draw_row(row, y, layout, cols, ink)
        y += layout.row_h

    # A rule under each inactive account; the active one has its card, and
    # the last one is followed by the menu's own separator.
    if not highlighted and not panel.is_active and not view.is_last:
        NSColor.separatorColor().setFill()
        NSBezierPath.fillRect_(NSMakeRect(*mp.hairline_rect(cols, height)))


def _draw_header(panel, layout, cols, ink) -> None:
    """Slot number, name, email; on the right the state words and the age."""
    fonts, top, h = layout.fonts, mp.TOP_PAD, layout.header_h

    def centred(font) -> float:
        return top + (h - _line_h(font)) / 2

    _text(str(panel.num), fonts["num"], ink.tertiary).drawAtPoint_((mp.LEFT_INSET, centred(fonts["num"])))

    pieces = []  # (text, font), in reading order
    for word in panel.state_words:  # "held · 28m" stays one piece
        if word == "active":
            accent = ink.secondary if ink.highlighted else NSColor.controlAccentColor()
            pieces.append((_text("ACTIVE", fonts["tag"], accent), fonts["tag"]))
        else:
            pieces.append((_text(word, fonts["meta"], ink.secondary), fonts["meta"]))
    if panel.age_text:
        pieces.append((_text(panel.age_text, fonts["meta"], ink.tertiary), fonts["meta"]))
    xs, meta_w = mp.right_run(cols.reset_right, [t.size().width for t, _f in pieces], mp.AGE_GAP)
    for (text, font), x in zip(pieces, xs):
        text.drawAtPoint_((x, centred(font)))

    name_font = fonts["title_active"] if panel.is_active else fonts["title"]
    name = _text(panel.name_text, name_font, ink.name, NSLineBreakByTruncatingMiddle)
    natural_w = name.size().width
    name_x, name_w, email_x, email_w = mp.header_spans(cols, natural_w, meta_w)
    if name_w < natural_w:
        name.drawInRect_(NSMakeRect(name_x, top, name_w, h))  # cut to the line, in the middle
    else:
        name.drawAtPoint_((name_x, top))  # whole: no rect for rounding to truncate
    if panel.show_email and email_w > 0:
        email = _text(panel.email, fonts["email"], ink.tertiary, NSLineBreakByTruncatingMiddle)
        email.drawInRect_(NSMakeRect(email_x, centred(fonts["email"]), email_w, _line_h(fonts["email"])))


def _draw_row(row, y, layout, cols, ink) -> None:
    fonts, row_h = layout.fonts, layout.row_h
    text_y = y + (row_h - _line_h(fonts["digits"])) / 2
    if row.state == "unknown":
        # A dead login is red, so it reads at a glance; every other note is secondary.
        color = NSColor.systemRedColor() if row.urgent and not ink.highlighted else ink.secondary
        note = _text(row.note, fonts["label"], color, NSLineBreakByTruncatingTail)
        note.drawInRect_(NSMakeRect(cols.label_x, text_y, cols.reset_right - cols.label_x, row_h))
        return

    label = _text(row.label, fonts["label"], ink.secondary, NSLineBreakByTruncatingTail)
    label.drawInRect_(NSMakeRect(cols.label_x, text_y, cols.label_w, row_h))

    bx, by, bw, bh = mp.bar_rect(cols, y, row_h)
    track = (
        NSColor.selectedMenuItemTextColor().colorWithAlphaComponent_(0.3)
        if ink.highlighted else NSColor.quaternaryLabelColor()
    )
    _fill_rounded((bx, by, bw, bh), bh / 2, track)
    fw = mp.fill_width(bw, row.fraction)
    if fw > 0:
        fill = _fill_color(row.state)
        if ink.dim:
            fill = fill.colorWithAlphaComponent_(EXHAUSTED_FILL_ALPHA)
        _fill_rounded((bx, by, fw, bh), bh / 2, fill)
    if row.pace_fraction is not None:
        tick = ink.primary if ink.highlighted else NSColor.labelColor().colorWithAlphaComponent_(TICK_ALPHA)
        tick.setFill()
        NSBezierPath.fillRect_(NSMakeRect(*mp.tick_rect(cols, row.pace_fraction, by)))

    pct_color = ink.strong
    if row.state == "over" and not ink.highlighted:
        pct_color = NSColor.systemRedColor()
        if ink.dim:
            pct_color = pct_color.colorWithAlphaComponent_(EXHAUSTED_OVER_ALPHA)
    pct = _text(row.pct_text, fonts["digits"], pct_color)
    pct.drawAtPoint_((cols.pct_right - pct.size().width, text_y))
    if row.reset_text:
        reset = _text(row.reset_text, fonts["digits"], ink.tertiary)
        reset.drawAtPoint_((cols.reset_right - reset.size().width, text_y))


def _draw_selection(bounds) -> None:
    """The native menu selection: accent colour, inset from the menu edge."""
    inset, radius = CswapMenuRowView.HIGHLIGHT_INSET, CswapMenuRowView.HIGHLIGHT_RADIUS
    NSColor.controlAccentColor().setFill()
    NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
        NSMakeRect(inset, 0.0, bounds.size.width - 2 * inset, bounds.size.height), radius, radius
    ).fill()


def _draw_fallback(view) -> None:
    """The plain text row, drawn inside a panel whose own drawing failed.

    Whatever the failed attempt already drew (a header, half a bar) is
    cleared first, so the text never sits on top of partial output.
    """
    bounds = view.bounds()
    NSRectFillUsingOperation(bounds, NSCompositingOperationClear)
    highlighted = view._highlighted()
    if highlighted:
        _draw_selection(bounds)
    panel = view.panel
    if panel is None:
        return
    font = NSFont.menuFontOfSize_(0.0)  # a native text row's font
    color = NSColor.selectedMenuItemTextColor() if highlighted else NSColor.labelColor()
    text = _text(panel.text_label, font, color, NSLineBreakByTruncatingTail)
    text.drawInRect_(NSMakeRect(
        mp.LEFT_INSET, mp.TOP_PAD,
        max(0.0, bounds.size.width - mp.LEFT_INSET - mp.RIGHT_INSET), bounds.size.height,
    ))


# ---- the Refresh now row ----------------------------------------------------------------

REFRESH_TITLE = "Refresh now"
REFRESHING_TITLE = "Refreshing\u2026"
FALLBACK_ITEM_HEIGHT = 22.0  # a native item's height when AppKit cannot be asked

_MEASURED: dict = {}  # AppKit's own menu metrics, measured once per process


def refresh_row_text(busy: bool) -> str:
    return REFRESHING_TITLE if busy else REFRESH_TITLE


class CswapRefreshRowView(CswapMenuRowView):
    """The Refresh now row: drawn like a native item, but its press keeps the menu open.

    A native item's press always closes the menu. This row asks the app to
    re-measure instead and stays, so the bars above it redraw while the user
    watches; a click outside or Escape closes the menu as with any menu.
    While the reading runs it says Refreshing in the secondary colour and,
    like a disabled item, neither highlights nor acts.
    """

    def initWithFrame_(self, frame):
        self = objc.super(CswapRefreshRowView, self).initWithFrame_(frame)
        if self is None:
            return None
        self.busy = False
        return self

    @objc.python_method
    def set_busy(self, busy) -> None:
        self.busy = bool(busy)
        self.setAccessibilityLabel_(refresh_row_text(self.busy))
        self.setNeedsDisplay_(True)

    def _activate(self) -> bool:
        if self.busy:
            return False
        target = self.account_target
        return bool(target is not None and target.activate_refresh(self))

    def drawRect_(self, dirty):
        try:
            _draw_refresh_row(self)
        except Exception:  # never raise out of an AppKit callback
            logging.getLogger("claude-swap").debug("Refresh now row failed to draw", exc_info=True)


def _menu_size(states):
    """The size AppKit gives a menu of native items with these states."""
    menu = NSMenu.alloc().init()
    for state in states:
        item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(REFRESH_TITLE, None, "")
        item.setState_(state)
        menu.addItem_(item)
    return menu.size()


def native_item_height() -> float:
    """A native menu item's height, measured from AppKit: what one more item adds to a menu."""
    if "height" not in _MEASURED:
        height = float(_menu_size([0, 0]).height - _menu_size([0]).height)
        _MEASURED["height"] = height if 10.0 <= height <= 60.0 else FALLBACK_ITEM_HEIGHT
    return _MEASURED["height"]


# Native titles start this far right of LEFT_INSET. Measured live on
# Oct 22, 2026 (a 2x capture, bars on, no checkmarks shown): with the text
# at LEFT_INSET, the row's ink began at 461 px and the native "Settings" and
# "Quit" titles' at 464 px, 1.5 pt further right. AppKit offers no way to
# read a native title's origin offscreen, unlike the state column below.
NATIVE_TITLE_OFFSET = 1.5


def state_column_width() -> float:
    """How far a checkmark anywhere in a menu moves every native title right.

    Measured from AppKit: the width a menu gains when one item is checked.
    """
    if "column" not in _MEASURED:
        column = float(_menu_size([1]).width - _menu_size([0]).width)
        _MEASURED["column"] = column if 0.0 <= column <= 60.0 else 0.0
    return _MEASURED["column"]


def refresh_text_x(menu) -> float:
    """Where a native item's title starts in ``menu``, for the row's text.

    ``LEFT_INSET`` plus ``NATIVE_TITLE_OFFSET`` (measured live), moved right
    by the state column when the menu shows one: in text-row mode the
    active account's checkmark reserves it for every item.
    """
    x = mp.LEFT_INSET + NATIVE_TITLE_OFFSET
    if menu is not None and menu.showsStateColumn() and any(
        item.state() != 0 for item in menu.itemArray()
    ):
        return x + state_column_width()
    return x


def _draw_refresh_row(view) -> None:
    bounds = view.bounds()
    highlighted = view._highlighted() and not view.busy
    if highlighted:
        _draw_selection(bounds)
    font = NSFont.menuFontOfSize_(0.0)  # a native item's font
    if view.busy:
        color = NSColor.secondaryLabelColor()
    elif highlighted:
        color = NSColor.selectedMenuItemTextColor()
    else:
        color = NSColor.labelColor()
    item = view.enclosingMenuItem()
    x = refresh_text_x(item.menu() if item is not None else None)
    y = (bounds.size.height - _line_h(font)) / 2
    _text(refresh_row_text(view.busy), font, color).drawAtPoint_((x, y))


def make_refresh_row_view(target=None, busy=False):
    """The Refresh now row's view, a native item tall; ``target`` takes its press."""
    font = NSFont.menuFontOfSize_(0.0)
    text_w = max(_width(REFRESH_TITLE, font), _width(REFRESHING_TITLE, font))
    width = math.ceil(
        mp.LEFT_INSET + NATIVE_TITLE_OFFSET + state_column_width() + text_w + mp.RIGHT_INSET
    )
    view = CswapRefreshRowView.alloc().initWithFrame_(
        NSMakeRect(0.0, 0.0, float(width), native_item_height())
    )
    view.account_target = target
    view.setAutoresizingMask_(NSViewWidthSizable)  # the menu widens it to its own width
    view.setAccessibilityElement_(True)
    view.setAccessibilityRole_(NSAccessibilityButtonRole)
    view.set_busy(busy)
    return view


# ---- the menu delegate ----------------------------------------------------------------

class CswapPanelMenuDelegate(NSObject):
    """Reports the menu opening and closing, and routes Return to drawn rows.

    ``on_open``/``on_close`` let the app hold rebuilds while the menu is on
    screen. During tracking, key events go to the menu window's first
    responder: making the highlighted row (a panel or the Refresh now row)
    first responder lets Return reach it, and when a native item is
    highlighted focus is taken back so Return on that item is left entirely
    to the menu.
    """

    @objc.python_method
    def _call(self, name):
        callback = getattr(self, name, None)
        if callback is not None:
            try:
                callback()
            except Exception:
                logging.getLogger("claude-swap").debug("menu delegate %s failed", name, exc_info=True)

    def menuWillOpen_(self, menu):
        self._call("on_open")

    def menuDidClose_(self, menu):
        for other in _row_views(menu):
            other.focus_allowed = False  # the next open starts with nothing selected
        self._call("on_close")

    def menu_willHighlightItem_(self, menu, item):
        view = item.view() if item is not None else None
        target = view if isinstance(view, CswapMenuRowView) else None
        for other in _row_views(menu):
            if other is not target:
                other.focus_allowed = False
        if target is not None:
            target.focus_allowed = True
            window = target.window()
            if window is not None:
                window.makeFirstResponder_(target)
            return
        # A native item is highlighted: take focus back from any drawn row,
        # so Return on that item is left entirely to the menu. All rows share
        # the menu's window.
        for other in _row_views(menu):
            window = other.window()
            if window is not None:
                if isinstance(window.firstResponder(), CswapMenuRowView):
                    window.makeFirstResponder_(None)
                return


def _row_views(menu):
    return [
        item.view() for item in menu.itemArray()
        if isinstance(item.view(), CswapMenuRowView)
    ]


_MENU_DELEGATES: dict = {}  # NSMenu delegates are weak references; keep ours alive


def install_menu_delegate(nsmenu, on_open=None, on_close=None):
    """Give ``nsmenu`` its (single, retained) delegate; returns it.

    Idempotent: the menu keeps one delegate across rebuilds; callbacks given
    here replace the previous ones.
    """
    delegate = _MENU_DELEGATES.get(id(nsmenu))
    if delegate is None or nsmenu.delegate() is not delegate:
        delegate = CswapPanelMenuDelegate.alloc().init()
        _MENU_DELEGATES[id(nsmenu)] = delegate
        nsmenu.setDelegate_(delegate)
    if on_open is not None:
        delegate.on_open = on_open
    if on_close is not None:
        delegate.on_close = on_close
    return delegate


# ---- building and attaching views ---------------------------------------------------

def make_panel_view(panel, layout: PanelLayout, target=None, on_draw_failure=None, is_last=False):
    """The view for one panel; ``is_last`` marks the last account row in the menu."""
    view = CswapUsagePanelView.alloc().initWithFrame_(
        NSMakeRect(0.0, 0.0, layout.width, layout.height(panel))
    )
    view.panel = panel
    view.layout = layout
    view.is_last = bool(is_last)
    view.account_target = target
    view.on_draw_failure = on_draw_failure
    view.setAutoresizingMask_(NSViewWidthSizable)  # the menu widens it to its own width
    # An activatable element, read as the account and one sentence per window.
    view.setAccessibilityElement_(True)
    view.setAccessibilityRole_(NSAccessibilityButtonRole)
    view.setAccessibilityLabel_(mp.accessibility_label(panel))
    return view


def update_panel_view(view, panel, layout) -> None:
    """Show ``panel`` in an attached view, drawn with ``layout``: Refresh now in place.

    The view keeps its item, frame and place in the open menu; only its
    model, its spoken label and its drawing change. The caller checked that
    the height did not change (``menubar_panel.fits_in_place``).
    """
    view.panel = panel
    view.layout = layout
    view.setAccessibilityLabel_(mp.accessibility_label(panel))
    view.setNeedsDisplay_(True)


def _set_item_view(nsitem, view) -> None:
    nsitem.setView_(view)


def attach_usage_panels(pairs, target=None, on_draw_failure=None, after_attach=None) -> None:
    """Give each ``(rumps.MenuItem, AccountPanel)`` its panel view, or none at all.

    Every view is built and measured before any item is touched. They are
    then attached in one pass, and ``after_attach`` (the menu delegate
    install) runs last. If anything raises, every item gets back exactly
    what it had (no view, its text title, its checkmark) and the error is
    re-raised, so the menu is never a mix of panels and text rows.
    """
    pairs = list(pairs)
    layout = PanelLayout([panel for _item, panel in pairs])
    # The pairs are the menu's account rows in order; a separator follows the last.
    views = [
        make_panel_view(
            panel, layout, target=target, on_draw_failure=on_draw_failure,
            is_last=(index == len(pairs) - 1),
        )
        for index, (_item, panel) in enumerate(pairs)
    ]
    natives = [item._menuitem for item, _panel in pairs]
    originals = [(native.view(), native.title(), native.state()) for native in natives]
    try:
        for native, view in zip(natives, views):
            _set_item_view(native, view)
            # The accent card marks the active account. A checkmark state would
            # also reserve a state column that shifts every native item's text.
            native.setState_(0)
        if after_attach is not None:
            after_attach()
    except Exception:
        for native, (view, title, state) in zip(natives, originals):
            try:
                native.setView_(view)
                native.setTitle_(title)
                native.setState_(state)
            except Exception:
                pass
        raise


# ---- back to the main thread while a menu is open -----------------------------------

class CswapMainThreadCall(NSObject):
    """One Python call carried to the main thread, run there even while a menu is open.

    An open menu runs the main run loop in its event-tracking mode. rumps
    adds its timers, the app's one-second sync tick among them, in the
    default mode only, so nothing they would do happens until the menu
    closes. A call queued in the common modes, which include event
    tracking, runs as soon as the main thread is free either way.
    """

    def run_(self, _arg):
        fn, self.fn = getattr(self, "fn", None), None
        if fn is None:
            return
        try:
            fn()
        except Exception:  # never raise out of an AppKit callback
            logging.getLogger("claude-swap").debug("menu bar main-thread call failed", exc_info=True)

    @objc.python_method
    def post(self) -> None:
        self.performSelectorOnMainThread_withObject_waitUntilDone_modes_(
            "run:", None, False, [NSRunLoopCommonModes]
        )


def call_on_main(fn) -> None:
    """Run ``fn`` on the main thread soon, also while a menu is tracking. Never waits."""
    call = CswapMainThreadCall.alloc().init()
    call.fn = fn
    call.post()


# ---- offscreen rendering (visual checks) ------------------------------------------

def render_png(panels, path: str, appearance: str = "darkAqua", highlighted: bool = False):
    """Draw ``panels`` stacked as in the menu into a PNG; returns (width, height).

    ``appearance`` is "aqua" or "darkAqua". The menu's own material is not
    available offscreen, so the image is backed with the window background
    colour of that appearance.
    """
    name = NSAppearanceNameDarkAqua if appearance == "darkAqua" else NSAppearanceNameAqua
    look = NSAppearance.appearanceNamed_(name)
    layout = PanelLayout(panels)
    heights = [layout.height(p) for p in panels]
    total = sum(heights)
    container = NSView.alloc().initWithFrame_(NSMakeRect(0.0, 0.0, layout.width, total))
    container.setAppearance_(look)
    y = total
    for index, (panel, h) in enumerate(zip(panels, heights)):
        y -= h
        view = make_panel_view(panel, layout, is_last=(index == len(panels) - 1))
        view.setFrameOrigin_((0.0, y))
        view.force_highlight = highlighted
        container.addSubview_(view)

    rep = container.bitmapImageRepForCachingDisplayInRect_(container.bounds())
    previous = NSAppearance.currentDrawingAppearance()
    NSAppearance.setCurrentAppearance_(look)
    try:
        from AppKit import NSGraphicsContext

        NSGraphicsContext.saveGraphicsState()
        NSGraphicsContext.setCurrentContext_(NSGraphicsContext.graphicsContextWithBitmapImageRep_(rep))
        NSColor.windowBackgroundColor().setFill()
        NSBezierPath.fillRect_(container.bounds())
        NSGraphicsContext.restoreGraphicsState()
        container.cacheDisplayInRect_toBitmapImageRep_(container.bounds(), rep)
    finally:
        NSAppearance.setCurrentAppearance_(previous)
    data = rep.representationUsingType_properties_(NSBitmapImageFileTypePNG, {})
    data.writeToFile_atomically_(path, True)
    return (layout.width, total)
