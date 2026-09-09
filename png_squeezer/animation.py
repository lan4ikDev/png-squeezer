"""A tiny animation system for the Tk widgets.

One timer drives everything. Each widget registers a value it wants eased
towards a target; on every frame the driver advances all of them and calls the
owner back once. Giving each widget its own ``after`` loop would mean dozens of
timers competing, and Tk would start dropping frames well before that becomes
a lot of widgets.
"""

from __future__ import annotations

import tkinter as tk
from typing import Callable

# 60 fps is smoother than Tk can reliably deliver on Windows; 50 is honest and
# leaves the main loop room to breathe during a compression run.
FRAME_MS = 20


#///////////////////////////////////////////////////////////////////////////////
#region easing


#///////////////////////////////////////////////////////////////////////////////
def ease_out_cubic(t: float) -> float:
    """Fast at first, settling gently. The default for UI motion."""

    t = max(0.0, min(1.0, t))
    return 1.0 - (1.0 - t) ** 3


#///////////////////////////////////////////////////////////////////////////////
def ease_in_out(t: float) -> float:
    """Symmetric ease, for things that move and stop in place."""

    t = max(0.0, min(1.0, t))
    if t < 0.5:
        return 4.0 * t * t * t
    return 1.0 - ((-2.0 * t + 2.0) ** 3) / 2.0


#endregion
#///////////////////////////////////////////////////////////////////////////////
#region lookup

_ATTRIBUTE = "_png_squeezer_animator"


#///////////////////////////////////////////////////////////////////////////////
def attach(root: tk.Misc) -> "Animator":
    """Create the window's animator and hang it off the root widget."""

    animator = Animator(root)
    setattr(root, _ATTRIBUTE, animator)
    return animator


#///////////////////////////////////////////////////////////////////////////////
def get(widget: tk.Misc) -> "Animator | None":
    """Find the animator for whatever window this widget belongs to."""

    try:
        return getattr(widget.winfo_toplevel(), _ATTRIBUTE, None)
    except Exception:
        return None


#endregion
#///////////////////////////////////////////////////////////////////////////////
#region the driver


class Animator:
    """Drives every eased value in the window from a single timer."""

    #///////////////////////////////////////////////////////////////////////////
    def __init__(self, widget: tk.Misc) -> None:
        self._widget = widget
        self._tweens: list[Tween] = []
        self._running = False
        self._after_id: str | None = None
        # Set by the app while a batch runs: animation is cosmetic and must
        # never compete with the work for main-loop time.
        self.reduced = False

    #///////////////////////////////////////////////////////////////////////////
    def add(self, tween: "Tween") -> None:
        if tween not in self._tweens:
            self._tweens.append(tween)
        self._ensure_running()

    #///////////////////////////////////////////////////////////////////////////
    def _ensure_running(self) -> None:
        if self._running:
            return
        self._running = True
        self._after_id = self._widget.after(FRAME_MS, self._tick)

    #///////////////////////////////////////////////////////////////////////////
    def stop(self) -> None:
        """Cancel the timer; used when the window is going away."""

        self._running = False
        if self._after_id is not None:
            try:
                self._widget.after_cancel(self._after_id)
            except Exception:
                pass
            self._after_id = None

    #///////////////////////////////////////////////////////////////////////////
    def _tick(self) -> None:
        if not self._running:
            return

        step = FRAME_MS / 1000.0
        finished: list[Tween] = []
        owners: dict[int, Tween] = {}

        for tween in self._tweens:
            if tween.advance(step):
                finished.append(tween)
            owners[id(tween)] = tween

        for tween in finished:
            self._tweens.remove(tween)

        # Redraw each owner once, no matter how many of its values moved.
        redrawn: set[int] = set()
        for tween in owners.values():
            key = id(tween.owner)
            if key in redrawn:
                continue
            redrawn.add(key)
            try:
                tween.owner.redraw()
            except Exception:
                # A widget destroyed mid-animation must not kill the timer.
                pass

        if self._tweens:
            self._after_id = self._widget.after(FRAME_MS, self._tick)
        else:
            self._running = False
            self._after_id = None


class Tween:
    """One value easing towards a target.

    The owner only needs a ``redraw()`` method; the tween holds the number and
    the driver calls back when it changes.
    """

    __slots__ = ("owner", "value", "target", "duration", "_elapsed", "_from",
                 "_easing", "_animator")

    #///////////////////////////////////////////////////////////////////////////
    def __init__(
        self,
        owner,
        animator: Animator | None,
        value: float = 0.0,
        duration: float = 0.18,
        easing: Callable[[float], float] = ease_out_cubic,
    ) -> None:
        self.owner = owner
        self._animator = animator
        self.value = value
        self.target = value
        self._from = value
        self.duration = duration
        self._elapsed = duration
        self._easing = easing

    #///////////////////////////////////////////////////////////////////////////
    def set(self, target: float, immediate: bool = False) -> None:
        """Ease towards ``target``, or jump straight there."""

        if immediate or self._animator is None or self._animator.reduced:
            self.value = self.target = self._from = float(target)
            self._elapsed = self.duration
            return
        if abs(target - self.target) < 1e-6:
            return
        self._from = self.value
        self.target = float(target)
        self._elapsed = 0.0
        self._animator.add(self)

    #///////////////////////////////////////////////////////////////////////////
    def advance(self, step: float) -> bool:
        """Move one frame on. Returns True when the tween is done."""

        if self._elapsed >= self.duration:
            self.value = self.target
            return True
        self._elapsed += step
        fraction = min(1.0, self._elapsed / self.duration) if self.duration > 0 else 1.0
        self.value = self._from + (self.target - self._from) * self._easing(fraction)
        if fraction >= 1.0:
            self.value = self.target
            return True
        return False

    #///////////////////////////////////////////////////////////////////////////
    @property
    def done(self) -> bool:
        return self._elapsed >= self.duration


class Ticker:
    """Runs a callback every frame until it reports itself finished.

    For animation that does not fit a single eased number -- the file list
    grows a bar per visible row, and there can be thousands of rows.
    """

    __slots__ = ("owner", "_step", "_animator", "_active")

    #///////////////////////////////////////////////////////////////////////////
    def __init__(self, owner, animator: Animator | None,
                 step: Callable[[float], bool]) -> None:
        self.owner = owner
        self._step = step
        self._animator = animator
        self._active = False

    #///////////////////////////////////////////////////////////////////////////
    def start(self) -> None:
        if self._active or self._animator is None or self._animator.reduced:
            return
        self._active = True
        self._animator.add(self)

    #///////////////////////////////////////////////////////////////////////////
    def stop(self) -> None:
        self._active = False

    #///////////////////////////////////////////////////////////////////////////
    def advance(self, step: float) -> bool:
        if not self._active:
            return True
        done = self._step(step)
        if done:
            self._active = False
        return done


class Spinner:
    """A continuously rotating value for busy indicators."""

    __slots__ = ("owner", "angle", "speed", "_animator", "_active")

    #///////////////////////////////////////////////////////////////////////////
    def __init__(self, owner, animator: Animator | None, speed: float = 220.0) -> None:
        self.owner = owner
        self._animator = animator
        self.angle = 0.0
        self.speed = speed          # degrees per second
        self._active = False

    #///////////////////////////////////////////////////////////////////////////
    def start(self) -> None:
        if self._active or self._animator is None:
            return
        self._active = True
        self._animator.add(self)

    #///////////////////////////////////////////////////////////////////////////
    def stop(self) -> None:
        self._active = False

    #///////////////////////////////////////////////////////////////////////////
    def advance(self, step: float) -> bool:
        if not self._active:
            return True
        self.angle = (self.angle + self.speed * step) % 360.0
        return False


#endregion
