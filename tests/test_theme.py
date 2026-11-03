"""Tests for the Palette value object and the light/dark themes."""
from __future__ import annotations

import pytest

from claude_swap.tui import theme
from claude_swap.tui.theme import CSWAP_DARK, CSWAP_LIGHT, Palette
from claude_swap.tui.widgets import usage_bar


def test_dark_palette_matches_constants():
    p = Palette.DARK
    assert (p.accent, p.foreground, p.muted, p.sev_ok, p.sev_warn, p.sev_crit, p.track) == (
        theme.ACCENT,
        theme.FOREGROUND,
        theme.MUTED,
        theme.SEV_OK,
        theme.SEV_WARN,
        theme.SEV_CRIT,
        theme.TRACK,
    )


def test_from_theme_reads_theme_object_including_track():
    p = Palette.from_theme(CSWAP_LIGHT)
    assert p.accent == theme.ACCENT_LIGHT
    assert p.sev_crit == theme.SEV_CRIT_LIGHT
    assert p.track == theme.TRACK_LIGHT  # from Theme.variables["track"], not app cache


_BANDS = [
    (0.0, "sev_ok"),
    (74.9, "sev_ok"),
    (74.99, "sev_ok"),
    (75.0, "sev_warn"),
    (92.0, "sev_warn"),
    (99.9, "sev_warn"),
    (99.99, "sev_warn"),
    (100.0, "sev_crit"),
    (130.0, "sev_crit"),
]
_PALETTES = pytest.mark.parametrize(
    "palette", [Palette.DARK, Palette.from_theme(CSWAP_LIGHT)], ids=["dark", "light"]
)


def test_band_edges():
    # Green under 75%, yellow from 75% up to the limit, red at 100% and over.
    assert (theme.WARN_PCT, theme.CRIT_PCT) == (75.0, 100.0)


@_PALETTES
def test_severity_of_none_is_muted(palette):
    assert palette.severity(None) == palette.muted


@_PALETTES
@pytest.mark.parametrize("pct, band", _BANDS)
def test_severity_bands(palette, pct, band):
    assert palette.severity(pct) == getattr(palette, band)


@pytest.mark.parametrize("pct, band", _BANDS)
def test_dashboard_bar_and_percent_use_the_band(pct, band):
    # The dashboard's usage line: the bar fill and the percent share the band.
    p = Palette.DARK
    line = usage_bar("5h", pct, None, 20, palette=p)
    styles = {str(span.style) for span in line.spans if line.plain[span.start:span.end].strip() != "5h"}
    assert getattr(p, band) in styles  # bar fill and percent
    pct_spans = [s for s in line.spans if line.plain[s.start:s.end].strip() == f"{pct:.0f}%"]
    assert [str(s.style) for s in pct_spans] == [getattr(p, band)]
    other_bands = {p.sev_ok, p.sev_warn, p.sev_crit} - {getattr(p, band)}
    assert not styles & other_bands


def test_both_themes_expose_track_variable():
    assert CSWAP_DARK.variables["track"] == theme.TRACK
    assert CSWAP_LIGHT.variables["track"] == theme.TRACK_LIGHT
    assert CSWAP_LIGHT.dark is False


def _contrast(hex_a: str, hex_b: str) -> float:
    def lum(h: str) -> float:
        r, g, b = (int(h[i:i+2], 16) / 255 for i in (1, 3, 5))
        f = lambda c: c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
        return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b)
    la, lb = sorted((lum(hex_a), lum(hex_b)))
    return (lb + 0.05) / (la + 0.05)


def test_light_text_meets_AA_on_all_backgrounds():
    # Accent and severity colors render as PERCENTAGE TEXT on highlighted
    # ($surface) and flash ($panel) rows, not just the base background — so
    # every text color must clear the 4.5:1 text bar against all three.
    from claude_swap.tui import theme
    text_colors = (
        theme.FOREGROUND_LIGHT,
        theme.MUTED_LIGHT,
        theme.ACCENT_LIGHT,
        theme.SEV_OK_LIGHT,
        theme.SEV_WARN_LIGHT,
        theme.SEV_CRIT_LIGHT,
    )
    backgrounds = (theme.BACKGROUND_LIGHT, theme.SURFACE_LIGHT, theme.PANEL_LIGHT)
    for color in text_colors:
        for bg in backgrounds:
            assert _contrast(color, bg) >= 4.5, f"{color} on {bg}"
