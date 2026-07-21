"""Shared UI glyphs used across board and tree widgets."""

from __future__ import annotations

DROPDOWN_EXPANDED_GLYPH = "▾"
DROPDOWN_COLLAPSED_GLYPH = "▸"
DROPDOWN_EXPANDED_PREFIX = f"{DROPDOWN_EXPANDED_GLYPH} "
DROPDOWN_COLLAPSED_PREFIX = f"{DROPDOWN_COLLAPSED_GLYPH} "


def dropdown_glyph(expanded: bool) -> str:
    return DROPDOWN_EXPANDED_GLYPH if bool(expanded) else DROPDOWN_COLLAPSED_GLYPH


def dropdown_prefix(expanded: bool) -> str:
    return DROPDOWN_EXPANDED_PREFIX if bool(expanded) else DROPDOWN_COLLAPSED_PREFIX