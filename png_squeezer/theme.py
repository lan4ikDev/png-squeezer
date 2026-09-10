"""Colours, fonts and the few drawing helpers the custom widgets share.

Everything in the app is drawn on ``tk.Canvas`` rather than assembled from ttk
widgets, because ttk on Windows refuses to give up its native chrome -- a
themed button keeps its grey border no matter what style you hand it. Drawing
by hand costs a page of geometry and buys a consistent dark UI.
"""

from __future__ import annotations

import tkinter as tk
import tkinter.font as tkfont


#///////////////////////////////////////////////////////////////////////////////
#region palette


class Color:
    """Flat namespace so call sites read as ``Color.accent``."""

    window = "#0E1014"
    surface = "#161A21"
    surface_high = "#1D222B"
    surface_hover = "#242A35"
    border = "#272D3A"
    border_soft = "#1F242E"

    text = "#E9ECF3"
    text_dim = "#98A1B4"
    text_faint = "#5F6779"

    accent = "#3DDC97"
    accent_dim = "#2AA875"
    accent_glow = "#1E4437"

    info = "#5B9DFF"
    warning = "#F5A524"
    danger = "#F2555A"

    track = "#232936"
    # Barely-there band for alternating list rows: enough to follow one line
    # across the window, not enough to read as a separate element.
    stripe = "#191D25"


#endregion
#///////////////////////////////////////////////////////////////////////////////
#region fonts


_FONT_CACHE: dict[tuple[str, int, str], tkfont.Font] = {}
_FAMILY: str | None = None


#///////////////////////////////////////////////////////////////////////////////
def preferred_family(root: tk.Misc) -> str:
    """Pick the nicest UI font actually installed on this machine."""

    global _FAMILY
    if _FAMILY is not None:
        return _FAMILY
    available = {name.lower(): name for name in tkfont.families(root)}
    for wanted in ("Segoe UI Variable Text", "Segoe UI", "Inter", "Roboto", "DejaVu Sans"):
        if wanted.lower() in available:
            _FAMILY = available[wanted.lower()]
            return _FAMILY
    _FAMILY = "TkDefaultFont"
    return _FAMILY


#///////////////////////////////////////////////////////////////////////////////
def font(root: tk.Misc, size: int = 10, weight: str = "normal") -> tkfont.Font:
    """Return a cached font, so repeated redraws do not allocate."""

    family = preferred_family(root)
    key = (family, size, weight)
    cached = _FONT_CACHE.get(key)
    if cached is None:
        cached = tkfont.Font(root=root, family=family, size=size, weight=weight)
        _FONT_CACHE[key] = cached
    return cached


#endregion
#///////////////////////////////////////////////////////////////////////////////
#region drawing helpers


#///////////////////////////////////////////////////////////////////////////////
def round_rect(
    canvas: tk.Canvas,
    x1: float,
    y1: float,
    x2: float,
    y2: float,
    radius: float = 10.0,
    **kwargs,
) -> int:
    """Draw a rounded rectangle and return its canvas id.

    Tk has no such primitive; the standard trick is a spline through the
    corner points, which is indistinguishable from an arc at UI radii.
    """

    radius = max(0.0, min(radius, (x2 - x1) / 2.0, (y2 - y1) / 2.0))
    points = [
        x1 + radius, y1,
        x2 - radius, y1,
        x2, y1,
        x2, y1 + radius,
        x2, y2 - radius,
        x2, y2,
        x2 - radius, y2,
        x1 + radius, y2,
        x1, y2,
        x1, y2 - radius,
        x1, y1 + radius,
        x1, y1,
    ]
    return canvas.create_polygon(points, smooth=True, splinesteps=24, **kwargs)


#///////////////////////////////////////////////////////////////////////////////
def dashed_round_rect(
    canvas: tk.Canvas,
    x1: float,
    y1: float,
    x2: float,
    y2: float,
    radius: float = 16.0,
    dash: tuple[int, int] = (7, 6),
    **kwargs,
) -> list[int]:
    """Draw a dashed rounded outline as four lines and four arcs.

    A dashed spline polygon renders its dashes unevenly around the corners, so
    the drop zone border is assembled from straight and curved pieces instead.
    """

    ids: list[int] = []
    diameter = radius * 2
    ids.append(canvas.create_line(x1 + radius, y1, x2 - radius, y1, dash=dash, **kwargs))
    ids.append(canvas.create_line(x1 + radius, y2, x2 - radius, y2, dash=dash, **kwargs))
    ids.append(canvas.create_line(x1, y1 + radius, x1, y2 - radius, dash=dash, **kwargs))
    ids.append(canvas.create_line(x2, y1 + radius, x2, y2 - radius, dash=dash, **kwargs))

    arc_kwargs = dict(kwargs)
    arc_kwargs.pop("fill", None)
    outline = kwargs.get("fill", Color.border)
    corners = (
        (x1, y1, x1 + diameter, y1 + diameter, 90),
        (x2 - diameter, y1, x2, y1 + diameter, 0),
        (x2 - diameter, y2 - diameter, x2, y2, 270),
        (x1, y2 - diameter, x1 + diameter, y2, 180),
    )
    for ax1, ay1, ax2, ay2, start in corners:
        ids.append(
            canvas.create_arc(
                ax1, ay1, ax2, ay2,
                start=start, extent=90, style=tk.ARC,
                outline=outline, dash=dash, **arc_kwargs,
            )
        )
    return ids


#///////////////////////////////////////////////////////////////////////////////
def mix(color_a: str, color_b: str, amount: float) -> str:
    """Blend two ``#rrggbb`` colours; ``amount`` 0 gives A, 1 gives B."""

    amount = max(0.0, min(1.0, amount))
    a = (int(color_a[1:3], 16), int(color_a[3:5], 16), int(color_a[5:7], 16))
    b = (int(color_b[1:3], 16), int(color_b[3:5], 16), int(color_b[5:7], 16))
    blended = tuple(round(a[i] + (b[i] - a[i]) * amount) for i in range(3))
    return "#%02x%02x%02x" % blended


#///////////////////////////////////////////////////////////////////////////////
def ratio_color(ratio: float) -> str:
    """Colour a saving ratio: red for nothing, amber midway, green for a lot."""

    if ratio <= 0.0:
        return Color.text_faint
    if ratio < 0.25:
        return mix(Color.warning, Color.accent, ratio / 0.25 * 0.35)
    if ratio < 0.5:
        return mix(Color.warning, Color.accent, 0.35 + (ratio - 0.25) / 0.25 * 0.45)
    return Color.accent


#///////////////////////////////////////////////////////////////////////////////
def elide(text: str, target_font: tkfont.Font, max_width: int) -> str:
    """Trim a string with a leading ellipsis so the filename stays readable."""

    if max_width <= 0 or target_font.measure(text) <= max_width:
        return text
    ellipsis = "…"
    low, high = 0, len(text)
    while low < high:
        middle = (low + high) // 2
        candidate = ellipsis + text[len(text) - middle:]
        if target_font.measure(candidate) <= max_width:
            low = middle + 1
        else:
            high = middle
    keep = max(0, low - 1)
    return ellipsis + text[len(text) - keep:] if keep else ellipsis


#endregion
