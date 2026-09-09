"""Hand-drawn dark-theme widgets.

Each widget is a ``tk.Canvas`` that repaints itself on ``<Configure>`` and on
state changes. The file list is virtualised -- it paints only the rows that
are on screen -- because a folder run can easily hold a few thousand entries
and Tk slows to a crawl long before that if every row is a real canvas item.
"""

from __future__ import annotations

import tkinter as tk
from typing import Callable, Sequence

from . import animation
from .core import human_size
from .theme import Color, dashed_round_rect, elide, font, mix, ratio_color, round_rect


#///////////////////////////////////////////////////////////////////////////////
#region base


class CanvasWidget(tk.Canvas):
    """A canvas that knows how to repaint itself when it is resized."""

    #///////////////////////////////////////////////////////////////////////////
    def __init__(self, master: tk.Misc, background: str = Color.window, **kwargs) -> None:
        super().__init__(
            master,
            highlightthickness=0,
            borderwidth=0,
            background=background,
            **kwargs,
        )
        self._background = background
        self.bind("<Configure>", lambda _event: self.redraw())

    #///////////////////////////////////////////////////////////////////////////
    def tween(self, value: float = 0.0, duration: float = 0.18) -> animation.Tween:
        """Make a tween bound to this window's animator."""

        return animation.Tween(self, animation.get(self), value=value, duration=duration)

    #///////////////////////////////////////////////////////////////////////////
    def spinner(self, speed: float = 220.0) -> animation.Spinner:
        return animation.Spinner(self, animation.get(self), speed=speed)

    #///////////////////////////////////////////////////////////////////////////
    def redraw(self) -> None:
        """Repaint. Subclasses override; the base clears the canvas."""

        self.delete("all")


#endregion
#///////////////////////////////////////////////////////////////////////////////
#region button


class RoundButton(CanvasWidget):
    """A filled or outlined pill button with hover, press and disabled states."""

    #///////////////////////////////////////////////////////////////////////////
    def __init__(
        self,
        master: tk.Misc,
        text: str,
        command: Callable[[], None],
        variant: str = "primary",
        width: int = 160,
        height: int = 40,
        background: str = Color.window,
        font_size: int = 10,
    ) -> None:
        super().__init__(master, background=background, width=width, height=height)
        self._text = text
        self._command = command
        self._variant = variant
        self._font_size = font_size
        self._hovered = False
        self._pressed = False
        self._enabled = True
        # Hover and press are eased rather than switched, which is most of
        # what makes a hand-drawn button feel like a real one.
        self._hover = self.tween(0.0, duration=0.14)
        self._press = self.tween(0.0, duration=0.09)

        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)
        self.bind("<ButtonPress-1>", self._on_press)
        self.bind("<ButtonRelease-1>", self._on_release)

    #///////////////////////////////////////////////////////////////////////////
    def configure_text(self, text: str) -> None:
        if text != self._text:
            self._text = text
            self.redraw()

    #///////////////////////////////////////////////////////////////////////////
    def set_command(self, command: Callable[[], None]) -> None:
        """Repoint the button; the action button doubles as a stop button."""

        self._command = command

    #///////////////////////////////////////////////////////////////////////////
    def set_enabled(self, enabled: bool) -> None:
        if enabled != self._enabled:
            self._enabled = enabled
            self._hovered = False
            self._pressed = False
            self._hover.set(0.0, immediate=True)
            self._press.set(0.0, immediate=True)
            self.configure(cursor="" if not enabled else "hand2")
            self.redraw()

    #///////////////////////////////////////////////////////////////////////////
    def _on_enter(self, _event) -> None:
        if self._enabled:
            self._hovered = True
            self.configure(cursor="hand2")
            self._hover.set(1.0)

    #///////////////////////////////////////////////////////////////////////////
    def _on_leave(self, _event) -> None:
        self._hovered = False
        self._pressed = False
        self._hover.set(0.0)
        self._press.set(0.0)

    #///////////////////////////////////////////////////////////////////////////
    def _on_press(self, _event) -> None:
        if self._enabled:
            self._pressed = True
            self._press.set(1.0)

    #///////////////////////////////////////////////////////////////////////////
    def _on_release(self, event) -> None:
        was_pressed = self._pressed
        self._pressed = False
        self._press.set(0.0)
        if not (self._enabled and was_pressed):
            return
        inside = 0 <= event.x <= self.winfo_width() and 0 <= event.y <= self.winfo_height()
        if inside:
            self._command()

    #///////////////////////////////////////////////////////////////////////////
    def _colors(self) -> tuple[str, str, str]:
        """Return ``(fill, outline, text)`` blended for the current state."""

        hover = self._hover.value
        press = self._press.value

        if not self._enabled:
            if self._variant == "primary":
                return Color.surface_high, Color.surface_high, Color.text_faint
            return self._background, Color.border_soft, Color.text_faint

        if self._variant == "primary":
            base = mix(Color.accent, "#FFFFFF", 0.12 * hover)
            base = mix(base, Color.accent_dim, press)
            return base, base, "#08130D"

        if self._variant == "danger":
            fill = mix(self._background, Color.danger, hover)
            fill = mix(fill, mix(Color.danger, "#000000", 0.25), press)
            text = mix(Color.danger, "#FFFFFF", hover)
            return fill, Color.danger, text

        # ghost
        fill = mix(self._background, Color.surface_hover, hover)
        fill = mix(fill, Color.surface_high, press)
        outline = mix(Color.border, Color.text_faint, hover * 0.6)
        text = mix(Color.text_dim, Color.text, hover)
        return fill, outline, text

    #///////////////////////////////////////////////////////////////////////////
    def redraw(self) -> None:
        self.delete("all")
        width = self.winfo_width()
        height = self.winfo_height()
        if width <= 1 or height <= 1:
            return
        fill, outline, text_color = self._colors()

        # A press nudges the pill inwards by a pixel; cheaper to read than a
        # colour change alone and it reads as a physical push.
        inset = 1.0 + self._press.value
        round_rect(
            self, inset, inset, width - inset, height - inset,
            radius=height / 2.0, fill=fill, outline=outline, width=1.4,
        )
        self.create_text(
            width / 2, height / 2 + 1 + self._press.value,
            text=self._text,
            fill=text_color,
            font=font(self, self._font_size, "bold"),
        )


#endregion
#///////////////////////////////////////////////////////////////////////////////
#region segmented control


class Segmented(CanvasWidget):
    """A two-or-more way mode switch with a sliding highlight."""

    #///////////////////////////////////////////////////////////////////////////
    def __init__(
        self,
        master: tk.Misc,
        options: Sequence[str],
        command: Callable[[int], None],
        height: int = 40,
        background: str = Color.window,
    ) -> None:
        super().__init__(master, background=background, height=height)
        self._labels = list(options)
        self._command = command
        self._selected = 0
        self._hovered = -1
        self._enabled = True
        # The highlight slides between segments instead of jumping.
        self._position = self.tween(0.0, duration=0.22)
        self.bind("<Motion>", self._on_motion)
        self.bind("<Leave>", self._on_leave)
        self.bind("<Button-1>", self._on_click)

    #///////////////////////////////////////////////////////////////////////////
    @property
    def selected(self) -> int:
        return self._selected

    #///////////////////////////////////////////////////////////////////////////
    def select(self, index: int, notify: bool = False) -> None:
        index = max(0, min(len(self._labels) - 1, index))
        if index != self._selected:
            self._selected = index
            self._position.set(float(index))
            self.redraw()
            if notify:
                self._command(index)

    #///////////////////////////////////////////////////////////////////////////
    def set_enabled(self, enabled: bool) -> None:
        self._enabled = enabled
        self.redraw()

    #///////////////////////////////////////////////////////////////////////////
    def _index_at(self, x: int) -> int:
        width = max(1, self.winfo_width())
        slot = width / len(self._labels)
        return max(0, min(len(self._labels) - 1, int(x // slot)))

    #///////////////////////////////////////////////////////////////////////////
    def _on_motion(self, event) -> None:
        if not self._enabled:
            return
        index = self._index_at(event.x)
        if index != self._hovered:
            self._hovered = index
            self.configure(cursor="hand2")
            self.redraw()

    #///////////////////////////////////////////////////////////////////////////
    def _on_leave(self, _event) -> None:
        if self._hovered != -1:
            self._hovered = -1
            self.redraw()

    #///////////////////////////////////////////////////////////////////////////
    def _on_click(self, event) -> None:
        if not self._enabled:
            return
        self.select(self._index_at(event.x), notify=True)

    #///////////////////////////////////////////////////////////////////////////
    def redraw(self) -> None:
        self.delete("all")
        width = self.winfo_width()
        height = self.winfo_height()
        if width <= 1 or height <= 1:
            return

        round_rect(
            self, 0, 0, width, height,
            radius=height / 2.0, fill=Color.surface, outline=Color.border_soft, width=1,
        )

        slot = width / len(self._labels)
        pad = 3
        left = self._position.value * slot
        round_rect(
            self, left + pad, pad, left + slot - pad, height - pad,
            radius=(height - pad * 2) / 2.0,
            fill=Color.surface_hover if self._enabled else Color.surface_high,
            outline=Color.accent_glow if self._enabled else Color.surface_high,
            width=1,
        )

        for index, label in enumerate(self._labels):
            # Fade each label by how close the highlight is to it, so text and
            # highlight move together instead of the text snapping.
            nearness = max(0.0, 1.0 - abs(self._position.value - index))
            if not self._enabled:
                color = Color.text_faint
            else:
                resting = Color.text if index == self._hovered else Color.text_dim
                color = mix(resting, Color.accent, nearness)
            self.create_text(
                slot * (index + 0.5), height / 2 + 1,
                text=label, fill=color,
                font=font(self, 10, "bold" if nearness > 0.5 else "normal"),
            )


#endregion
#///////////////////////////////////////////////////////////////////////////////
#region slider and toggle


class Slider(CanvasWidget):
    """A horizontal integer slider with a draggable knob."""

    #///////////////////////////////////////////////////////////////////////////
    def __init__(
        self,
        master: tk.Misc,
        minimum: int,
        maximum: int,
        value: int,
        command: Callable[[int], None],
        width: int = 200,
        height: int = 26,
        background: str = Color.window,
    ) -> None:
        super().__init__(master, background=background, width=width, height=height)
        self._min = minimum
        self._max = maximum
        self._value = value
        self._command = command
        self._dragging = False
        self._hovered = False
        self._enabled = True
        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)
        self.bind("<ButtonPress-1>", self._on_press)
        self.bind("<B1-Motion>", self._on_drag)
        self.bind("<ButtonRelease-1>", self._on_release)

    #///////////////////////////////////////////////////////////////////////////
    @property
    def value(self) -> int:
        return self._value

    #///////////////////////////////////////////////////////////////////////////
    def set_enabled(self, enabled: bool) -> None:
        self._enabled = enabled
        self.configure(cursor="hand2" if enabled else "")
        self.redraw()

    #///////////////////////////////////////////////////////////////////////////
    def _on_enter(self, _event) -> None:
        self._hovered = True
        if self._enabled:
            self.configure(cursor="hand2")
        self.redraw()

    #///////////////////////////////////////////////////////////////////////////
    def _on_leave(self, _event) -> None:
        self._hovered = False
        self.redraw()

    #///////////////////////////////////////////////////////////////////////////
    def _value_at(self, x: int) -> int:
        margin = 11
        span = max(1, self.winfo_width() - margin * 2)
        fraction = max(0.0, min(1.0, (x - margin) / span))
        return int(round(self._min + fraction * (self._max - self._min)))

    #///////////////////////////////////////////////////////////////////////////
    def _apply(self, x: int) -> None:
        new_value = self._value_at(x)
        if new_value != self._value:
            self._value = new_value
            self.redraw()
            self._command(new_value)

    #///////////////////////////////////////////////////////////////////////////
    def _on_press(self, event) -> None:
        if not self._enabled:
            return
        self._dragging = True
        self._apply(event.x)

    #///////////////////////////////////////////////////////////////////////////
    def _on_drag(self, event) -> None:
        if self._dragging:
            self._apply(event.x)

    #///////////////////////////////////////////////////////////////////////////
    def _on_release(self, _event) -> None:
        self._dragging = False

    #///////////////////////////////////////////////////////////////////////////
    def redraw(self) -> None:
        self.delete("all")
        width = self.winfo_width()
        height = self.winfo_height()
        if width <= 1 or height <= 1:
            return

        margin = 11
        middle = height / 2
        fraction = (self._value - self._min) / max(1, self._max - self._min)
        knob_x = margin + fraction * (width - margin * 2)

        active = Color.accent if self._enabled else Color.text_faint
        self.create_line(margin, middle, width - margin, middle,
                         fill=Color.track, width=5, capstyle=tk.ROUND)
        if knob_x > margin:
            self.create_line(margin, middle, knob_x, middle,
                             fill=active, width=5, capstyle=tk.ROUND)

        radius = 9 if (self._hovered or self._dragging) and self._enabled else 7.5
        self.create_oval(knob_x - radius, middle - radius, knob_x + radius, middle + radius,
                         fill=Color.surface_hover if not self._enabled else "#F2F5FA",
                         outline=active, width=2)


class Toggle(CanvasWidget):
    """A labelled checkbox."""

    #///////////////////////////////////////////////////////////////////////////
    def __init__(
        self,
        master: tk.Misc,
        text: str,
        value: bool,
        command: Callable[[bool], None],
        background: str = Color.window,
        height: int = 26,
    ) -> None:
        super().__init__(master, background=background, height=height)
        self._text = text
        self._value = value
        self._command = command
        self._hovered = False
        self._enabled = True
        self._on = self.tween(1.0 if value else 0.0, duration=0.16)
        self._hover = self.tween(0.0, duration=0.12)
        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)
        self.bind("<Button-1>", self._on_click)

    #///////////////////////////////////////////////////////////////////////////
    @property
    def value(self) -> bool:
        return self._value

    #///////////////////////////////////////////////////////////////////////////
    def set_enabled(self, enabled: bool) -> None:
        self._enabled = enabled
        self.redraw()

    #///////////////////////////////////////////////////////////////////////////
    def set_value(self, value: bool) -> None:
        if value != self._value:
            self._value = value
            self._on.set(1.0 if value else 0.0)
            self.redraw()

    #///////////////////////////////////////////////////////////////////////////
    def _on_enter(self, _event) -> None:
        self._hovered = True
        if self._enabled:
            self.configure(cursor="hand2")
        self._hover.set(1.0)

    #///////////////////////////////////////////////////////////////////////////
    def _on_leave(self, _event) -> None:
        self._hovered = False
        self._hover.set(0.0)

    #///////////////////////////////////////////////////////////////////////////
    def _on_click(self, _event) -> None:
        if not self._enabled:
            return
        self._value = not self._value
        self._on.set(1.0 if self._value else 0.0)
        self.redraw()
        self._command(self._value)

    #///////////////////////////////////////////////////////////////////////////
    def redraw(self) -> None:
        self.delete("all")
        width = self.winfo_width()
        height = self.winfo_height()
        if width <= 1 or height <= 1:
            return

        box = 17
        top = (height - box) / 2
        on = self._on.value
        hover = self._hover.value

        if not self._enabled:
            fill = Color.surface_high
            outline = Color.surface_high
            text_color = Color.text_faint
        else:
            resting_outline = mix(Color.border, Color.text_dim, hover)
            fill = mix(Color.surface, Color.accent, on)
            outline = mix(resting_outline, Color.accent, on)
            text_color = mix(mix(Color.text_dim, Color.text, hover), Color.text, on)

        round_rect(self, 1, top, 1 + box, top + box, radius=5,
                   fill=fill, outline=outline, width=1.4)

        if on > 0.01 and self._enabled:
            # The tick draws itself on: the short leg first, then the long one.
            check = "#08130D"
            first = min(1.0, on / 0.45)
            x1, y1 = 5.5, top + box / 2
            x2, y2 = 8.0, top + box - 5
            x3, y3 = 13.5, top + 5.0
            self.create_line(x1, y1, x1 + (x2 - x1) * first, y1 + (y2 - y1) * first,
                             fill=check, width=2, capstyle=tk.ROUND)
            if on > 0.45:
                second = (on - 0.45) / 0.55
                self.create_line(x2, y2, x2 + (x3 - x2) * second, y2 + (y3 - y2) * second,
                                 fill=check, width=2, capstyle=tk.ROUND)
        elif self._value and not self._enabled:
            self.create_line(5.5, top + box / 2, 8, top + box - 5,
                             fill=Color.text_faint, width=2, capstyle=tk.ROUND)
            self.create_line(8, top + box - 5, 13.5, top + 5,
                             fill=Color.text_faint, width=2, capstyle=tk.ROUND)

        self.create_text(box + 11, height / 2 + 1, text=self._text, anchor="w",
                         fill=text_color, font=font(self, 10))


#endregion
#///////////////////////////////////////////////////////////////////////////////
#region number field and icon button


class NumberField(CanvasWidget):
    """A small numeric entry inside a rounded frame.

    The entry itself is a real ``tk.Entry`` parked on the canvas, because
    re-implementing a text caret is not worth it. Only the frame is drawn.
    """

    #///////////////////////////////////////////////////////////////////////////
    def __init__(
        self,
        master: tk.Misc,
        value: str,
        on_change: Callable[[str], None],
        width: int = 92,
        height: int = 34,
        suffix: str = "",
        background: str = Color.window,
    ) -> None:
        super().__init__(master, background=background, width=width, height=height)
        self._suffix = suffix
        self._on_change = on_change
        self._focused = False

        self.variable = tk.StringVar(master=self, value=value)
        self.variable.trace_add("write", lambda *_a: self._on_change(self.variable.get()))

        self.entry = tk.Entry(
            self,
            textvariable=self.variable,
            background=Color.surface,
            foreground=Color.text,
            insertbackground=Color.accent,
            disabledbackground=Color.surface,
            disabledforeground=Color.text_faint,
            relief="flat",
            borderwidth=0,
            highlightthickness=0,
            justify="center",
            font=font(self, 10),
        )
        self.entry.bind("<FocusIn>", self._on_focus_in)
        self.entry.bind("<FocusOut>", self._on_focus_out)
        self._entry_window = self.create_window(0, 0, window=self.entry, anchor="nw")

    #///////////////////////////////////////////////////////////////////////////
    def get(self) -> str:
        return self.variable.get()

    #///////////////////////////////////////////////////////////////////////////
    def set(self, value: str) -> None:
        if value != self.variable.get():
            self.variable.set(value)

    #///////////////////////////////////////////////////////////////////////////
    def set_enabled(self, enabled: bool) -> None:
        self.entry.configure(state="normal" if enabled else "disabled")
        self.redraw()

    #///////////////////////////////////////////////////////////////////////////
    def _on_focus_in(self, _event) -> None:
        self._focused = True
        self.redraw()

    #///////////////////////////////////////////////////////////////////////////
    def _on_focus_out(self, _event) -> None:
        self._focused = False
        self.redraw()

    #///////////////////////////////////////////////////////////////////////////
    def redraw(self) -> None:
        self.delete("frame")
        width = self.winfo_width()
        height = self.winfo_height()
        if width <= 1 or height <= 1:
            return

        enabled = str(self.entry.cget("state")) == "normal"
        if not enabled:
            outline = Color.border_soft
        elif self._focused:
            outline = Color.accent
        else:
            outline = Color.border
        round_rect(self, 1, 1, width - 1, height - 1, radius=8,
                   fill=Color.surface, outline=outline, width=1.4, tags="frame")
        self.tag_lower("frame")

        suffix_width = 0
        if self._suffix:
            suffix_font = font(self, 9)
            suffix_width = suffix_font.measure(self._suffix) + 8
            self.create_text(width - 9, height / 2 + 1, anchor="e", text=self._suffix,
                             fill=Color.text_faint if enabled else Color.border,
                             font=suffix_font, tags="frame")

        self.coords(self._entry_window, 9, 6)
        self.itemconfigure(self._entry_window,
                           width=max(10, width - 18 - suffix_width),
                           height=max(10, height - 12))


class IconButton(CanvasWidget):
    """A square button carrying a drawn glyph -- currently only a gear."""

    #///////////////////////////////////////////////////////////////////////////
    def __init__(
        self,
        master: tk.Misc,
        command: Callable[[], None],
        size: int = 40,
        background: str = Color.window,
    ) -> None:
        super().__init__(master, background=background, width=size, height=size)
        self._command = command
        self._hovered = False
        self._active = False
        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)
        self.bind("<Button-1>", lambda _event: self._command())

    #///////////////////////////////////////////////////////////////////////////
    def set_active(self, active: bool) -> None:
        if active != self._active:
            self._active = active
            self.redraw()

    #///////////////////////////////////////////////////////////////////////////
    def _on_enter(self, _event) -> None:
        self._hovered = True
        self.configure(cursor="hand2")
        self.redraw()

    #///////////////////////////////////////////////////////////////////////////
    def _on_leave(self, _event) -> None:
        self._hovered = False
        self.redraw()

    #///////////////////////////////////////////////////////////////////////////
    def redraw(self) -> None:
        self.delete("all")
        width = self.winfo_width()
        height = self.winfo_height()
        if width <= 1 or height <= 1:
            return

        if self._active:
            fill, outline, glyph = Color.surface_hover, Color.accent, Color.accent
        elif self._hovered:
            fill, outline, glyph = Color.surface_high, Color.border, Color.text
        else:
            fill, outline, glyph = Color.surface, Color.border_soft, Color.text_dim

        round_rect(self, 1, 1, width - 1, height - 1, radius=11,
                   fill=fill, outline=outline, width=1.4)
        self._draw_gear(width / 2, height / 2, glyph)

    #///////////////////////////////////////////////////////////////////////////
    def _draw_gear(self, x: float, y: float, color: str) -> None:
        import math

        outer, inner = 9.0, 6.2
        points: list[float] = []
        teeth = 8
        for step in range(teeth * 4):
            angle = math.pi * 2 * step / (teeth * 4)
            radius = outer if (step % 4) in (0, 1) else inner
            points.extend((x + math.cos(angle) * radius, y + math.sin(angle) * radius))
        self.create_polygon(points, fill=color, outline="")
        self.create_oval(x - 3, y - 3, x + 3, y + 3, fill=self._background, outline="")


#endregion
#///////////////////////////////////////////////////////////////////////////////
#region drop zone


class DropZone(CanvasWidget):
    """The dashed target that accepts dropped files and folders."""

    #///////////////////////////////////////////////////////////////////////////
    def __init__(
        self,
        master: tk.Misc,
        title: str,
        subtitle: str,
        on_pick_files: Callable[[], None],
        on_pick_folder: Callable[[], None],
        height: int = 190,
    ) -> None:
        super().__init__(master, background=Color.window, height=height)
        self._title = title
        self._subtitle = subtitle
        self._hot = False
        self._hovered = False
        self._glow = self.tween(0.0, duration=0.16)
        self._lift = self.tween(0.0, duration=0.20)

        self._files_button = RoundButton(
            self, "Выбрать файлы", on_pick_files, variant="primary",
            width=150, height=36, background=Color.surface, font_size=9)
        self._folder_button = RoundButton(
            self, "Выбрать папку", on_pick_folder, variant="ghost",
            width=150, height=36, background=Color.surface, font_size=9)
        self._files_window = self.create_window(0, 0, window=self._files_button,
                                                anchor="center", state="hidden")
        self._folder_window = self.create_window(0, 0, window=self._folder_button,
                                                 anchor="center", state="hidden")

        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)
        self.bind("<Button-1>", lambda _event: on_pick_files())

    #///////////////////////////////////////////////////////////////////////////
    def set_labels(self, title: str, subtitle: str) -> None:
        self._title = title
        self._subtitle = subtitle
        self.redraw()

    #///////////////////////////////////////////////////////////////////////////
    def set_hot(self, hot: bool) -> None:
        """Highlight while a drag hovers over the window."""

        if hot != self._hot:
            self._hot = hot
            self._glow.set(1.0 if hot else 0.0)
            self._lift.set(1.0 if hot else (0.35 if self._hovered else 0.0))
            self.redraw()

    #///////////////////////////////////////////////////////////////////////////
    def _on_enter(self, _event) -> None:
        self._hovered = True
        self.configure(cursor="hand2")
        self._lift.set(1.0 if self._hot else 0.35)

    #///////////////////////////////////////////////////////////////////////////
    def _on_leave(self, _event) -> None:
        self._hovered = False
        self._lift.set(1.0 if self._hot else 0.0)

    #///////////////////////////////////////////////////////////////////////////
    def redraw(self) -> None:
        # Only the painted parts are cleared; deleting "all" would destroy the
        # window items that hold the two buttons.
        self.delete("art")
        width = self.winfo_width()
        height = self.winfo_height()
        if width <= 1 or height <= 1:
            return

        glow = self._glow.value
        lift = self._lift.value

        # Hover warms the panel; an active drag pushes it the rest of the way
        # to the accent colour and pulls the border in slightly, so the zone
        # visibly reaches for the file.
        fill = mix(Color.surface, Color.surface_high, min(1.0, lift * 1.4))
        fill = mix(fill, Color.accent_glow, glow)
        border = mix(Color.border, Color.text_faint, min(1.0, lift * 1.2))
        border = mix(border, Color.accent, glow)
        title_color = mix(Color.text_dim, Color.text, max(glow, lift))

        margin = 2.0 + glow * 2.0
        round_rect(self, margin, margin, width - margin, height - margin,
                   radius=18, fill=fill, outline="", tags="art")
        dashed_round_rect(self, margin, margin, width - margin, height - margin,
                          radius=18, fill=border, width=1.6 + glow * 0.6, tags="art")
        self.tag_lower("art")

        center_x = width / 2
        roomy = height >= 150

        if roomy:
            # The arrow drifts down a little while a drag is over the window.
            self._draw_icon(center_x, height / 2 - 48 + glow * 5.0,
                            mix(Color.text_faint, Color.accent, glow))
            title_y = height / 2 - 2
        else:
            title_y = height / 2 - 12

        self.create_text(center_x, title_y, text=self._title,
                         fill=title_color, font=font(self, 13, "bold"), tags="art")
        self.create_text(center_x, title_y + 22, text=self._subtitle,
                         fill=Color.text_faint, font=font(self, 9), tags="art")

        # The buttons only fit, and only help, in the tall empty state.
        show_buttons = roomy and not self._hot
        for window, offset in ((self._files_window, -82), (self._folder_window, 82)):
            self.itemconfigure(window, state="normal" if show_buttons else "hidden")
            if show_buttons:
                self.coords(window, center_x + offset, title_y + 58)

    #///////////////////////////////////////////////////////////////////////////
    def _draw_icon(self, x: float, y: float, color: str) -> None:
        """A downward arrow into a tray -- the universal 'drop here' glyph."""

        for coords in (
            (x, y - 18, x, y + 6),
            (x - 9, y - 3, x, y + 6),
            (x + 9, y - 3, x, y + 6),
            (x - 18, y + 13, x - 18, y + 19),
            (x + 18, y + 13, x + 18, y + 19),
            (x - 18, y + 19, x + 18, y + 19),
        ):
            self.create_line(*coords, fill=color, width=2.4,
                             capstyle=tk.ROUND, tags="art")


#endregion
#///////////////////////////////////////////////////////////////////////////////
#region file list


class FileRow:
    """One line of the result list. Plain data; the list paints it."""

    __slots__ = ("path", "display", "original_size", "new_size", "state",
                 "detail", "ratio", "note", "shown_ratio", "appeared")

    #///////////////////////////////////////////////////////////////////////////
    def __init__(self, path: str, display: str, original_size: int) -> None:
        self.path = path
        self.display = display
        self.original_size = original_size
        self.new_size = 0
        self.state = "queued"  # queued | working | done | skipped | error
        self.detail = ""
        self.ratio = 0.0
        self.note = ""
        # How much of the saving bar has grown in, 0..1 of `ratio`. Animated
        # by the list rather than by a tween each, because there can be
        # thousands of rows and only a handful are ever on screen.
        self.shown_ratio = 0.0
        self.appeared = 0.0


class FileList(CanvasWidget):
    """A virtualised, scrollable list of :class:`FileRow`."""

    ROW_HEIGHT = 54

    #///////////////////////////////////////////////////////////////////////////
    def __init__(self, master: tk.Misc) -> None:
        super().__init__(master, background=Color.surface)
        self._rows: list[FileRow] = []
        self._offset = 0.0
        self._hover_index = -1
        # Scrolling glides instead of teleporting, and the busy row spins.
        self._scroll = self.tween(0.0, duration=0.20)
        self._spin = self.spinner(speed=300.0)
        self._bars = animation.Ticker(self, animation.get(self), self._grow_bars)
        self.bind("<MouseWheel>", self._on_wheel)
        self.bind("<Motion>", self._on_motion)
        self.bind("<Leave>", self._on_leave)

    #///////////////////////////////////////////////////////////////////////////
    #region list contents

    #///////////////////////////////////////////////////////////////////////////
    def set_rows(self, rows: list[FileRow]) -> None:
        self._rows = rows
        self._offset = 0.0
        self._scroll.set(0.0, immediate=True)
        self.redraw()

    #///////////////////////////////////////////////////////////////////////////
    def rows(self) -> list[FileRow]:
        return self._rows

    #///////////////////////////////////////////////////////////////////////////
    def clear(self) -> None:
        self._rows = []
        self._offset = 0.0
        self._scroll.set(0.0, immediate=True)
        self._spin.stop()
        self._bars.stop()
        self.redraw()

    #///////////////////////////////////////////////////////////////////////////
    def scroll_to(self, index: int) -> None:
        """Keep the row at ``index`` in view while a batch runs."""

        height = self.winfo_height()
        top = index * self.ROW_HEIGHT
        bottom = top + self.ROW_HEIGHT
        if top < self._offset:
            self._offset = float(top)
        elif bottom > self._offset + height:
            self._offset = float(bottom - height)
        self._clamp()
        self._scroll.set(self._offset)

    #///////////////////////////////////////////////////////////////////////////
    def _grow_bars(self, step: float) -> bool:
        """Ease every visible saving bar towards its real value.

        Returns True once nothing on screen is still growing, which is what
        stops the ticker.
        """

        settled = True
        for row in self._visible_rows():
            if row.state in ("done", "skipped"):
                if row.shown_ratio < row.ratio - 0.001:
                    row.shown_ratio += (row.ratio - row.shown_ratio) * min(1.0, step * 9.0)
                    settled = False
                else:
                    row.shown_ratio = row.ratio
                if row.appeared < 0.999:
                    row.appeared = min(1.0, row.appeared + step * 4.0)
                    settled = False
        return settled

    #///////////////////////////////////////////////////////////////////////////
    def _visible_rows(self):
        height = self.winfo_height()
        if height <= 1 or not self._rows:
            return []
        first = max(0, int(self._scroll.value // self.ROW_HEIGHT))
        last = min(len(self._rows),
                   int((self._scroll.value + height) // self.ROW_HEIGHT) + 1)
        return self._rows[first:last]

    #///////////////////////////////////////////////////////////////////////////
    def note_activity(self) -> None:
        """Called by the app after results land, to restart the animations."""

        self._bars.start()
        if any(row.state == "working" for row in self._visible_rows()):
            self._spin.start()
        else:
            self._spin.stop()

    #endregion
    #///////////////////////////////////////////////////////////////////////////
    #region scrolling

    #///////////////////////////////////////////////////////////////////////////
    def _max_offset(self) -> float:
        return max(0.0, len(self._rows) * self.ROW_HEIGHT - self.winfo_height())

    #///////////////////////////////////////////////////////////////////////////
    def _clamp(self) -> None:
        self._offset = max(0.0, min(self._offset, self._max_offset()))

    #///////////////////////////////////////////////////////////////////////////
    def _on_wheel(self, event) -> None:
        # Windows reports 120 per notch; two rows per notch feels right.
        self._offset -= (event.delta / 120.0) * self.ROW_HEIGHT * 2
        self._clamp()
        self._scroll.set(self._offset)
        self.redraw()

    #///////////////////////////////////////////////////////////////////////////
    def _on_motion(self, event) -> None:
        index = int((event.y + self._scroll.value) // self.ROW_HEIGHT)
        if not (0 <= index < len(self._rows)):
            index = -1
        if index != self._hover_index:
            self._hover_index = index
            self.redraw()

    #///////////////////////////////////////////////////////////////////////////
    def _on_leave(self, _event) -> None:
        if self._hover_index != -1:
            self._hover_index = -1
            self.redraw()

    #endregion
    #///////////////////////////////////////////////////////////////////////////
    #region painting

    #///////////////////////////////////////////////////////////////////////////
    def redraw(self) -> None:
        self.delete("all")
        width = self.winfo_width()
        height = self.winfo_height()
        if width <= 1 or height <= 1:
            return

        self._clamp()
        if not self._rows:
            self.create_text(width / 2, height / 2, text="Список пуст",
                             fill=Color.text_faint, font=font(self, 10))
            return

        scroll = self._scroll.value
        first = max(0, int(scroll // self.ROW_HEIGHT))
        last = min(len(self._rows), int((scroll + height) // self.ROW_HEIGHT) + 1)
        for index in range(first, last):
            top = index * self.ROW_HEIGHT - scroll
            self._draw_row(self._rows[index], top, width, index == self._hover_index)

        self._draw_scrollbar(width, height)

    #///////////////////////////////////////////////////////////////////////////
    def _draw_row(self, row: FileRow, top: float, width: int, hovered: bool) -> None:
        height = self.ROW_HEIGHT
        if hovered:
            self.create_rectangle(0, top, width, top + height,
                                  fill=Color.surface_high, outline="")
        self.create_line(14, top + height - 0.5, width - 14, top + height - 0.5,
                         fill=Color.border_soft)

        dot_x = 20
        dot_y = top + height / 2
        state_color = {
            "queued": Color.text_faint,
            "working": Color.info,
            "done": ratio_color(row.ratio),
            "skipped": Color.warning,
            "error": Color.danger,
        }.get(row.state, Color.text_faint)

        if row.state == "working":
            # A rotating arc, the usual "working on it" cue.
            self.create_arc(dot_x - 6, dot_y - 6, dot_x + 6, dot_y + 6,
                            start=-self._spin.angle, extent=250, style=tk.ARC,
                            outline=state_color, width=2)
        else:
            grown = row.appeared if row.state in ("done", "skipped") else 1.0
            radius = 4.0 * (0.4 + 0.6 * grown)
            self.create_oval(dot_x - radius, dot_y - radius, dot_x + radius, dot_y + radius,
                             fill=state_color, outline="")

        right_edge = width - 20
        bar_width = 118
        percent_width = 58
        name_font = font(self, 10)
        detail_font = font(self, 8)
        name_limit = int(right_edge - bar_width - percent_width - 60)

        self.create_text(38, top + 17, anchor="w",
                         text=elide(row.display, name_font, name_limit),
                         fill=Color.text if row.state != "queued" else Color.text_dim,
                         font=name_font)
        if row.detail:
            self.create_text(38, top + 36, anchor="w", text=row.detail,
                             fill=Color.text_faint, font=detail_font)

        if row.state in ("done", "skipped"):
            # The number counts up with the bar rather than landing first.
            shown = row.shown_ratio
            percent = f"−{shown * 100:.0f}%" if row.ratio > 0.0005 else "0%"
            self.create_text(right_edge, top + 17, anchor="e", text=percent,
                             fill=ratio_color(row.ratio), font=font(self, 11, "bold"))
            sizes = f"{human_size(row.original_size)} → {human_size(row.new_size)}"
            self.create_text(right_edge, top + 36, anchor="e", text=sizes,
                             fill=Color.text_faint, font=detail_font)

            bar_left = right_edge - percent_width - bar_width
            bar_right = right_edge - percent_width
            bar_y = top + 17
            round_rect(self, bar_left, bar_y - 4, bar_right, bar_y + 4,
                       radius=4, fill=Color.track, outline="")
            filled = bar_left + (bar_right - bar_left) * max(0.0, min(1.0, shown))
            if filled > bar_left + 2:
                round_rect(self, bar_left, bar_y - 4, filled, bar_y + 4,
                           radius=4, fill=ratio_color(row.ratio), outline="")
        elif row.state == "error":
            self.create_text(right_edge, top + height / 2, anchor="e", text="ошибка",
                             fill=Color.danger, font=font(self, 10, "bold"))
        elif row.state == "working":
            self.create_text(right_edge, top + height / 2, anchor="e", text="сжимается…",
                             fill=Color.info, font=font(self, 9))
        else:
            self.create_text(right_edge, top + height / 2, anchor="e",
                             text=human_size(row.original_size),
                             fill=Color.text_faint, font=font(self, 9))

    #///////////////////////////////////////////////////////////////////////////
    def _draw_scrollbar(self, width: int, height: int) -> None:
        total = len(self._rows) * self.ROW_HEIGHT
        if total <= height:
            return
        track_top = 6
        track_height = height - 12
        thumb_height = max(28.0, track_height * height / total)
        maximum = self._max_offset()
        travel = (self._scroll.value / maximum) if maximum > 0 else 0.0
        thumb_top = track_top + (track_height - thumb_height) * travel
        x = width - 7
        round_rect(self, x - 2, thumb_top, x + 2, thumb_top + thumb_height,
                   radius=2, fill=Color.border, outline="")

    #endregion


#endregion
