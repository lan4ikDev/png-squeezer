"""The PNG Squeezer window.

Two tools share the window, chosen by the switch in the header:

* **Сжатие** -- the TinyPNG behaviour. No knobs on screen; the quality
  controls live behind the gear icon because the defaults are the answer
  almost every time.
* **Изменить размер** -- percent or pixel resizing, with its panel open by
  default because there is nothing sensible to default to.

Either tool writes its output one of two ways, picked independently:

* **Сохранить в ZIP** -- results are held in memory and handed back as a
  single archive through the normal Windows save dialog.
* **Заменить на месте** -- the bypass: every file is written back over
  itself, and dropping a folder starts the run immediately.

Compression runs in a worker thread driving a process pool; results come back
over a ``queue.Queue`` that the Tk main loop drains on a timer. Tk is not
thread safe, so no worker ever touches a widget.
"""

from __future__ import annotations

import os
import queue
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import filedialog, messagebox

from . import animation, core
from .core import BatchTotals, Options, Result, human_size
from .theme import Color, font, ratio_color, round_rect
from .widgets import (
    DropZone,
    FileList,
    FileRow,
    IconButton,
    NumberField,
    RoundButton,
    Segmented,
    Slider,
    Toggle,
)

try:
    from tkinterdnd2 import DND_FILES, TkinterDnD

    HAVE_DND = True
except Exception:  # pragma: no cover - optional dependency
    DND_FILES = None
    TkinterDnD = None
    HAVE_DND = False


TOOL_COMPRESS = 0
TOOL_RESIZE = 1

OUTPUT_ZIP = 0
OUTPUT_REPLACE = 1

WINDOW_TITLE = "PNG Squeezer"


#///////////////////////////////////////////////////////////////////////////////
#region small helpers


#///////////////////////////////////////////////////////////////////////////////
def reveal_in_explorer(path: str) -> None:
    """Open the containing folder with the file selected."""

    path = os.path.abspath(path)
    if sys.platform == "win32":
        # explorer.exe returns 1 even when it succeeds, so the code is ignored.
        subprocess.run(["explorer", "/select,", path], check=False)
    elif sys.platform == "darwin":
        subprocess.run(["open", "-R", path], check=False)
    else:
        subprocess.run(["xdg-open", os.path.dirname(path)], check=False)


#///////////////////////////////////////////////////////////////////////////////
def open_path(path: str) -> None:
    """Hand a file to whatever the system opens it with."""

    if sys.platform == "win32":
        os.startfile(path)  # noqa: S606 - intended, this is a desktop app
    elif sys.platform == "darwin":
        subprocess.run(["open", path], check=False)
    else:
        subprocess.run(["xdg-open", path], check=False)


#///////////////////////////////////////////////////////////////////////////////
def make_window_icon(root: tk.Misc) -> tk.PhotoImage | None:
    """Draw the app icon at runtime so the tool stays a pure-code folder."""

    try:
        import io

        from PIL import Image, ImageDraw

        size = 64
        image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        draw = ImageDraw.Draw(image)
        draw.rounded_rectangle([2, 2, size - 3, size - 3], radius=14, fill="#161A21",
                               outline="#3DDC97", width=3)
        draw.polygon([(16, 22), (30, 22), (30, 14), (44, 32), (30, 50), (30, 42), (16, 42)],
                     fill="#3DDC97")
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        return tk.PhotoImage(master=root, data=buffer.getvalue())
    except Exception:
        return None


#///////////////////////////////////////////////////////////////////////////////
def read_int(text: str, fallback: int = 0) -> int:
    """Parse a number out of a text field without ever raising."""

    digits = "".join(character for character in text if character.isdigit())
    if not digits:
        return fallback
    try:
        return int(digits)
    except ValueError:
        return fallback


#///////////////////////////////////////////////////////////////////////////////
def describe_note(note: str) -> str:
    """Turn an engine note code into a phrase for the file list.

    The engine reports why it declined to quantise as a bare code so it stays
    language-neutral; the wording belongs here.
    """

    code, _, detail = note.partition(":")
    wording = {
        core.NOTE_NO_ENGINE: "libimagequant не установлен",
        core.NOTE_ANIMATED: "анимированный PNG",
        core.NOTE_PALETTE: "уже 8-битная палитра",
        core.NOTE_ALPHA: "изменилась бы прозрачность",
        core.NOTE_NOT_SMALLER: "сжатие не уменьшило файл",
        core.NOTE_QUALITY: "качество упало бы ниже порога",
    }
    if code == core.NOTE_DEVIATION:
        return f"отличие {detail}% выше порога"
    if code == core.NOTE_FLAT:
        return f"обесцветило бы кадр (потеря {detail}% контраста)"
    if code == core.NOTE_COLLAPSE:
        return f"палитра схлопнулась бы до {detail} цветов"
    if code == core.NOTE_ERROR:
        return f"ошибка сжатия: {detail}"
    return wording.get(code, note)


#///////////////////////////////////////////////////////////////////////////////
def plural(count: int, one: str, few: str, many: str) -> str:
    """Russian number agreement: 1 файл, 2 файла, 5 файлов."""

    if count % 100 in (11, 12, 13, 14):
        return many
    last = count % 10
    if last == 1:
        return one
    if last in (2, 3, 4):
        return few
    return many


#///////////////////////////////////////////////////////////////////////////////
def card(parent: tk.Misc) -> tuple[tk.Frame, tk.Frame]:
    """Return ``(outer, inner)`` frames forming a one-pixel bordered panel."""

    outer = tk.Frame(parent, background=Color.border_soft)
    inner = tk.Frame(outer, background=Color.surface)
    inner.pack(fill="both", expand=True, padx=1, pady=1)
    return outer, inner


#endregion
#///////////////////////////////////////////////////////////////////////////////
#region summary panel


class SummaryPanel(tk.Canvas):
    """The footer readout: before, after, saving, and a progress bar."""

    #///////////////////////////////////////////////////////////////////////////
    def __init__(self, master: tk.Misc) -> None:
        super().__init__(master, highlightthickness=0, borderwidth=0,
                         background=Color.window, height=64)
        self._totals = BatchTotals()
        self._message = "Готово к работе"
        self._message_color = Color.text_dim
        self._show_progress = False
        # The bar and the headline figures ease rather than step, so a batch
        # reads as continuous motion instead of a stutter per file.
        animator = animation.get(self)
        self._progress = animation.Tween(self, animator, 0.0, duration=0.25)
        self._before = animation.Tween(self, animator, 0.0, duration=0.35)
        self._after = animation.Tween(self, animator, 0.0, duration=0.35)
        self._ratio = animation.Tween(self, animator, 0.0, duration=0.35)
        self.bind("<Configure>", lambda _event: self.redraw())

    #///////////////////////////////////////////////////////////////////////////
    def update_totals(self, totals: BatchTotals, progress: float, show_progress: bool,
                      immediate: bool = False) -> None:
        self._totals = totals
        self._show_progress = show_progress
        self._progress.set(progress, immediate=immediate)
        self._before.set(float(totals.original_size), immediate=immediate)
        self._after.set(float(totals.new_size), immediate=immediate)
        self._ratio.set(totals.saved_ratio, immediate=immediate)
        self.redraw()

    #///////////////////////////////////////////////////////////////////////////
    def set_message(self, message: str, color: str = Color.text_dim) -> None:
        self._message = message
        self._message_color = color
        self.redraw()

    #///////////////////////////////////////////////////////////////////////////
    def redraw(self) -> None:
        self.delete("all")
        width = self.winfo_width()
        height = self.winfo_height()
        if width <= 1 or height <= 1:
            return

        top = 4

        if self._totals.original_size > 0:
            ratio = self._ratio.value
            saved = max(0.0, self._before.value - self._after.value)
            self.create_text(0, top, anchor="nw", text=human_size(self._before.value),
                             fill=Color.text_dim, font=font(self, 12))
            offset = self.bbox("all")[2] + 8
            self.create_text(offset, top, anchor="nw", text="→",
                             fill=Color.text_faint, font=font(self, 12))
            offset += 22
            self.create_text(offset, top, anchor="nw", text=human_size(self._after.value),
                             fill=Color.text, font=font(self, 12, "bold"))
            offset = self.bbox("all")[2] + 14
            self.create_text(offset, top - 2, anchor="nw", text=f"−{ratio * 100:.1f}%",
                             fill=ratio_color(ratio), font=font(self, 14, "bold"))
            offset = self.bbox("all")[2] + 14
            self.create_text(offset, top + 4, anchor="nw",
                             text=f"экономия {human_size(saved)}",
                             fill=Color.text_faint, font=font(self, 9))
        else:
            self.create_text(0, top, anchor="nw", text="Ничего ещё не обработано",
                             fill=Color.text_faint, font=font(self, 12))

        self.create_text(0, height - 22, anchor="nw", text=self._message,
                         fill=self._message_color, font=font(self, 9))

        if self._show_progress:
            bar_top = height - 6
            progress = self._progress.value
            round_rect(self, 0, bar_top, width, bar_top + 4, radius=2,
                       fill=Color.track, outline="")
            if progress > 0.001:
                round_rect(self, 0, bar_top, width * min(1.0, progress), bar_top + 4,
                           radius=2, fill=Color.accent, outline="")


#endregion
#///////////////////////////////////////////////////////////////////////////////
#region the app


class SqueezerApp:
    """Owns the window, the widget tree and the batch lifecycle."""

    #///////////////////////////////////////////////////////////////////////////
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.tool = TOOL_COMPRESS
        self.output = OUTPUT_ZIP
        self.settings_open = False

        self.rows: list[FileRow] = []
        # What the user handed over (folders and files), kept so the list can
        # be rebuilt when the "include sub-folders" setting changes.
        self.source_paths: list[str] = []
        # The PNGs those paths expanded to.
        self.paths: list[str] = []
        self.results: dict[str, Result] = {}
        self.totals = BatchTotals()
        self.busy = False
        self.stop_event = threading.Event()
        self.result_queue: "queue.Queue[tuple[str, object]]" = queue.Queue()
        self.last_archive: str | None = None
        self.replace_root: str | None = None
        self.confirm_replace = True
        # Width/height are seeded from the first image until the user edits
        # them; _suppress_field_events tells a programmatic set from a typed one.
        self.pixel_fields_touched = False
        self._suppress_field_events = False

        # Must exist before any widget is built: they look it up on creation.
        self.animator = animation.attach(root)

        self._build_window()
        self._build_header()
        self._build_toolbar()
        self._build_panels()
        self._build_body()
        self._build_footer()
        self._on_smoothing_changed(self.smoothing_slider.value)
        self._install_dnd()
        self._sync_layout()
        self._sync_ui()

    #///////////////////////////////////////////////////////////////////////////
    #region construction

    #///////////////////////////////////////////////////////////////////////////
    def _build_window(self) -> None:
        self.root.title(WINDOW_TITLE)
        self.root.configure(background=Color.window)
        self.root.minsize(980, 720)

        width, height = 1080, 820
        x = max(0, (self.root.winfo_screenwidth() - width) // 2)
        y = max(0, (self.root.winfo_screenheight() - height) // 3)
        self.root.geometry(f"{width}x{height}+{x}+{y}")

        icon = make_window_icon(self.root)
        if icon is not None:
            self._icon = icon  # keep a reference or Tk drops it
            self.root.iconphoto(True, icon)

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    #///////////////////////////////////////////////////////////////////////////
    def _build_header(self) -> None:
        header = tk.Frame(self.root, background=Color.window)
        header.pack(fill="x", padx=26, pady=(18, 4))

        logo = tk.Canvas(header, width=34, height=34, highlightthickness=0,
                         borderwidth=0, background=Color.window)
        logo.pack(side="left")
        round_rect(logo, 1, 1, 33, 33, radius=10, fill=Color.surface_high,
                   outline=Color.accent, width=1.5)
        logo.create_polygon([10, 13, 18, 13, 18, 9, 25, 17, 18, 25, 18, 21, 10, 21],
                            fill=Color.accent)

        titles = tk.Frame(header, background=Color.window)
        titles.pack(side="left", padx=(12, 0))
        tk.Label(titles, text=WINDOW_TITLE, background=Color.window, foreground=Color.text,
                 font=font(self.root, 14, "bold")).pack(anchor="w")
        self.subtitle_label = tk.Label(
            titles, text="Сжатие PNG без видимой потери качества",
            background=Color.window, foreground=Color.text_faint, font=font(self.root, 9))
        self.subtitle_label.pack(anchor="w")

        self.tool_switch = Segmented(header, ["Сжатие", "Изменить размер"],
                                     self._on_tool_changed, height=40)
        self.tool_switch.configure(width=310)
        self.tool_switch.pack(side="right")

    #///////////////////////////////////////////////////////////////////////////
    def _build_toolbar(self) -> None:
        toolbar = tk.Frame(self.root, background=Color.window)
        toolbar.pack(fill="x", padx=26, pady=(14, 0))

        tk.Label(toolbar, text="Результат", background=Color.window,
                 foreground=Color.text_faint, font=font(self.root, 9)).pack(side="left",
                                                                           padx=(0, 10))

        self.output_switch = Segmented(toolbar, ["Сохранить в ZIP", "Заменить на месте"],
                                       self._on_output_changed, height=38)
        self.output_switch.configure(width=330)
        self.output_switch.pack(side="left")

        self.gear = IconButton(toolbar, self._toggle_settings, size=38)
        self.gear.pack(side="right")

        self.hint_label = tk.Label(toolbar, text="", background=Color.window,
                                   foreground=Color.text_faint, font=font(self.root, 9))
        self.hint_label.pack(side="right", padx=(0, 14))

    #///////////////////////////////////////////////////////////////////////////
    def _build_panels(self) -> None:
        self.panels = tk.Frame(self.root, background=Color.window)
        self.panels.pack(fill="x", padx=26, pady=(12, 0))

        self._build_resize_panel()
        self._build_settings_panel()

    #///////////////////////////////////////////////////////////////////////////
    def _build_resize_panel(self) -> None:
        self.resize_card, inner = card(self.panels)

        top = tk.Frame(inner, background=Color.surface)
        top.pack(fill="x", padx=16, pady=(14, 8))

        self.resize_switch = Segmented(top, ["В процентах", "В пикселях"],
                                       self._on_resize_mode_changed, height=34,
                                       background=Color.surface)
        self.resize_switch.configure(width=250)
        self.resize_switch.pack(side="left")

        # --- percent controls -------------------------------------------------
        self.percent_row = tk.Frame(top, background=Color.surface)
        self.percent_row.pack(side="left", padx=(20, 0))

        for preset in (25, 50, 75):
            RoundButton(self.percent_row, f"{preset}%",
                        lambda value=preset: self._set_percent(value),
                        variant="ghost", width=60, height=32,
                        background=Color.surface, font_size=9).pack(side="left", padx=(0, 8))

        self.percent_field = NumberField(self.percent_row, "50", self._on_number_changed,
                                         width=92, height=34, suffix="%",
                                         background=Color.surface)
        self.percent_field.pack(side="left", padx=(6, 0))

        # --- pixel controls ---------------------------------------------------
        self.pixels_row = tk.Frame(top, background=Color.surface)

        tk.Label(self.pixels_row, text="Ширина", background=Color.surface,
                 foreground=Color.text_dim, font=font(self.root, 9)).pack(side="left")
        self.width_field = NumberField(self.pixels_row, "", self._on_width_changed,
                                       width=94, height=34, suffix="px",
                                       background=Color.surface)
        self.width_field.pack(side="left", padx=(8, 16))

        tk.Label(self.pixels_row, text="Высота", background=Color.surface,
                 foreground=Color.text_dim, font=font(self.root, 9)).pack(side="left")
        self.height_field = NumberField(self.pixels_row, "", self._on_height_changed,
                                        width=94, height=34, suffix="px",
                                        background=Color.surface)
        self.height_field.pack(side="left", padx=(8, 0))

        # --- shared switches --------------------------------------------------
        bottom = tk.Frame(inner, background=Color.surface)
        bottom.pack(fill="x", padx=16, pady=(0, 14))

        self.aspect_toggle = Toggle(bottom, "Сохранять пропорции", True,
                                    self._on_aspect_changed, background=Color.surface)
        self.aspect_toggle.configure(width=200)
        self.aspect_toggle.pack(side="left")

        self.no_enlarge_toggle = Toggle(bottom, "Не увеличивать маленькие", True,
                                     lambda _value: self._update_hint(),
                                     background=Color.surface)
        self.no_enlarge_toggle.configure(width=230)
        self.no_enlarge_toggle.pack(side="left", padx=(24, 0))

        self.resize_note = tk.Label(bottom, text="", background=Color.surface,
                                    foreground=Color.text_faint, font=font(self.root, 9))
        self.resize_note.pack(side="right")

        smooth_row = tk.Frame(inner, background=Color.surface)
        smooth_row.pack(fill="x", padx=16, pady=(0, 14))

        tk.Label(smooth_row, text="Сглаживание", background=Color.surface,
                 foreground=Color.text_dim, font=font(self.root, 10)).pack(side="left")

        self.smoothing_slider = Slider(smooth_row, 0, 100, 50, self._on_smoothing_changed,
                                       width=180, height=26, background=Color.surface)
        self.smoothing_slider.pack(side="left", padx=(10, 10))

        self.smoothing_label = tk.Label(smooth_row, text="", background=Color.surface,
                                        foreground=Color.text_dim, anchor="w",
                                        font=font(self.root, 9))
        self.smoothing_label.pack(side="left")

    #///////////////////////////////////////////////////////////////////////////
    def _build_settings_panel(self) -> None:
        self.settings_card, inner = card(self.panels)

        # Two rows: five controls do not fit across the window on one line.
        top = tk.Frame(inner, background=Color.surface)
        top.pack(fill="x", padx=16, pady=(14, 6))

        tk.Label(top, text="Качество", background=Color.surface,
                 foreground=Color.text_dim, font=font(self.root, 10)).pack(side="left")

        self.quality_slider = Slider(top, 40, 100, 82, self._on_quality_changed,
                                     width=180, height=26, background=Color.surface)
        self.quality_slider.pack(side="left", padx=(10, 8))

        self.quality_label = tk.Label(top, text="82", background=Color.surface,
                                      foreground=Color.accent, width=3, anchor="w",
                                      font=font(self.root, 11, "bold"))
        self.quality_label.pack(side="left")

        # Off by default: it costs about 20% of the file and does not measure
        # any better. Useful on wide smooth gradients, where banding shows.
        self.dither_toggle = Toggle(top, "Дизеринг", False, lambda _value: None,
                                    background=Color.surface)
        self.dither_toggle.configure(width=105)
        self.dither_toggle.pack(side="left", padx=(22, 0))

        bottom = tk.Frame(inner, background=Color.surface)
        bottom.pack(fill="x", padx=16, pady=(0, 14))

        self.recursive_toggle = Toggle(bottom, "Включая подпапки", True,
                                       self._on_recursive_changed,
                                       background=Color.surface)
        self.recursive_toggle.configure(width=170)
        self.recursive_toggle.pack(side="left")

        self.backup_toggle = Toggle(bottom, "Копии .bak", False, lambda _value: None,
                                    background=Color.surface)
        self.backup_toggle.configure(width=125)
        self.backup_toggle.pack(side="left", padx=(14, 0))

        self.confirm_toggle = Toggle(bottom, "Спрашивать перед заменой", True,
                                     self._on_confirm_changed, background=Color.surface)
        self.confirm_toggle.configure(width=225)
        self.confirm_toggle.pack(side="left", padx=(14, 0))

    #///////////////////////////////////////////////////////////////////////////
    def _build_body(self) -> None:
        body = tk.Frame(self.root, background=Color.window)
        body.pack(fill="both", expand=True, padx=26, pady=(12, 0))

        self.drop_zone = DropZone(
            body,
            title="Перетащите PNG или папку сюда",
            subtitle="папки обходятся рекурсивно",
            on_pick_files=self._pick_files,
            on_pick_folder=self._pick_folder,
            height=196,
        )
        self.drop_zone.pack(fill="both", expand=True)

        self.list_frame = tk.Frame(body, background=Color.border)
        self.file_list = FileList(self.list_frame)
        self.file_list.pack(fill="both", expand=True, padx=1, pady=1)

    #///////////////////////////////////////////////////////////////////////////
    def _build_footer(self) -> None:
        footer = tk.Frame(self.root, background=Color.window)
        footer.pack(fill="x", padx=26, pady=(12, 18))

        buttons = tk.Frame(footer, background=Color.window)
        buttons.pack(side="right")

        self.clear_button = RoundButton(buttons, "Очистить", self._clear, variant="ghost",
                                        width=110, height=42)
        self.clear_button.pack(side="left", padx=(0, 10))

        self.secondary_button = RoundButton(buttons, "Открыть папку", self._reveal_archive,
                                            variant="ghost", width=150, height=42)
        self.secondary_button.pack(side="left", padx=(0, 10))
        self.secondary_button.pack_forget()

        self.action_button = RoundButton(buttons, "Сжать", self._start, variant="primary",
                                         width=190, height=42, font_size=11)
        self.action_button.pack(side="left")
        self.action_button.set_enabled(False)

        self.summary = SummaryPanel(footer)
        self.summary.pack(side="left", fill="x", expand=True, padx=(0, 20))

    #///////////////////////////////////////////////////////////////////////////
    def _install_dnd(self) -> None:
        # The wording for a missing tkinterdnd2 lives in _sync_drop_labels, so
        # that a later _sync_ui cannot quietly overwrite it.
        if not HAVE_DND:
            return
        for widget in (self.root, self.drop_zone, self.file_list):
            widget.drop_target_register(DND_FILES)
            widget.dnd_bind("<<Drop>>", self._on_drop)
            widget.dnd_bind("<<DropEnter>>", lambda _e: self.drop_zone.set_hot(True))
            widget.dnd_bind("<<DropLeave>>", lambda _e: self.drop_zone.set_hot(False))

    #endregion
    #///////////////////////////////////////////////////////////////////////////
    #region ui state

    #///////////////////////////////////////////////////////////////////////////
    def _on_tool_changed(self, index: int) -> None:
        if self.busy:
            self.tool_switch.select(self.tool)
            return
        self.tool = index
        self._sync_ui()

    #///////////////////////////////////////////////////////////////////////////
    def _on_output_changed(self, index: int) -> None:
        if self.busy:
            self.output_switch.select(self.output)
            return
        self.output = index
        self._sync_ui()

    #///////////////////////////////////////////////////////////////////////////
    def _toggle_settings(self) -> None:
        self.settings_open = not self.settings_open
        self._sync_ui()

    #///////////////////////////////////////////////////////////////////////////
    def _on_resize_mode_changed(self, _index: int) -> None:
        self._sync_resize_rows()
        self._update_hint()

    #///////////////////////////////////////////////////////////////////////////
    def _sync_resize_rows(self) -> None:
        if self.resize_switch.selected == 0:
            self.pixels_row.pack_forget()
            self.percent_row.pack(side="left", padx=(20, 0))
        else:
            self.percent_row.pack_forget()
            self.pixels_row.pack(side="left", padx=(20, 0))

    #///////////////////////////////////////////////////////////////////////////
    def _sync_ui(self) -> None:
        """Show the panels this tool needs and relabel the action button.

        Safe to call at any time: while a batch runs it only moves panels
        around. Touching the action button then would rename "Стоп" back to
        "Сжать" mid-run, and re-enabling the toggles would let the settings
        drift away from the options the workers were handed.
        """

        # The resize panel is always open on the resize tool and never on the
        # compression tool; the settings panel is only ever opened by the gear.
        self.resize_card.pack_forget()
        self.settings_card.pack_forget()
        showing_panel = False
        if self.tool == TOOL_RESIZE:
            self.resize_card.pack(fill="x", pady=(0, 8))
            self._sync_resize_rows()
            showing_panel = True
        if self.settings_open:
            self.settings_card.pack(fill="x")
            showing_panel = True
        # An empty panel strip should not leave a band of padding behind.
        self.panels.pack_configure(pady=(12, 0) if showing_panel else (0, 0))
        self.gear.set_active(self.settings_open)

        replacing = self.output == OUTPUT_REPLACE
        if self.tool == TOOL_COMPRESS:
            self.subtitle_label.configure(text="Сжатие PNG без видимой потери качества")
            action = "Сжать и заменить" if replacing else "Сжать"
        else:
            self.subtitle_label.configure(text="Изменение размера с последующим сжатием")
            action = "Изменить и заменить" if replacing else "Изменить размер"

        if self.busy:
            return

        self.action_button.configure_text(action)
        self._sync_drop_labels()
        self.backup_toggle.set_enabled(replacing)
        self.confirm_toggle.set_enabled(replacing)
        self._update_hint()

    #///////////////////////////////////////////////////////////////////////////
    def _sync_layout(self) -> None:
        """Give the space to whichever of the drop zone and the list needs it.

        With nothing loaded the drop zone fills the window; an empty list
        underneath it would just be a second, larger hole.
        """

        if self.rows:
            self.drop_zone.configure(height=96)
            self.drop_zone.pack_configure(fill="x", expand=False, pady=(0, 12))
            self.list_frame.pack(fill="both", expand=True)
        else:
            self.list_frame.pack_forget()
            self.drop_zone.configure(height=196)
            self.drop_zone.pack_configure(fill="both", expand=True, pady=(0, 0))

    #///////////////////////////////////////////////////////////////////////////
    def _sync_drop_labels(self) -> None:
        """Word the drop zone for the current output mode."""

        deep = "включая все подпапки" if self.recursive_toggle.value else "без подпапок"
        if not HAVE_DND:
            self.drop_zone.set_labels(
                "Выберите файлы или папку",
                "перетаскивание выключено: pip install tkinterdnd2",
            )
        elif self.output == OUTPUT_REPLACE:
            self.drop_zone.set_labels(
                "Перетащите папку или файлы сюда",
                f"всё внутри будет заменено на месте, {deep}",
            )
        else:
            self.drop_zone.set_labels(
                "Перетащите PNG или папку сюда",
                f"результат вернётся одним ZIP-архивом, {deep}",
            )

    #///////////////////////////////////////////////////////////////////////////
    def _update_hint(self) -> None:
        """Explain, in one line, what the current settings will actually do."""

        if self.busy:
            return
        if self.output == OUTPUT_REPLACE:
            self.hint_label.configure(text="файлы будут перезаписаны", fg=Color.warning)
        else:
            self.hint_label.configure(text="исходные файлы не изменятся", fg=Color.text_faint)

        if self.tool != TOOL_RESIZE:
            return

        if self.resize_switch.selected == 0:
            percent = read_int(self.percent_field.get(), 100)
            self.resize_note.configure(
                text="размер не изменится" if percent in (0, 100) else f"→ {percent}% от исходного")
        else:
            width = read_int(self.width_field.get())
            height = read_int(self.height_field.get())
            if not width and not height:
                text = "укажите ширину или высоту"
            elif self.aspect_toggle.value:
                if width and height:
                    text = f"вписать в {width}×{height}"
                elif width:
                    text = f"ширина {width}px, высота пропорционально"
                else:
                    text = f"высота {height}px, ширина пропорционально"
            else:
                text = f"ровно {width or '?'}×{height or '?'}"
            self.resize_note.configure(text=text)

    #///////////////////////////////////////////////////////////////////////////
    def _set_percent(self, value: int) -> None:
        self.percent_field.set(str(value))

    #///////////////////////////////////////////////////////////////////////////
    def _on_number_changed(self, _text: str) -> None:
        self._update_hint()

    #///////////////////////////////////////////////////////////////////////////
    def _on_width_changed(self, _text: str) -> None:
        if not self._suppress_field_events:
            self.pixel_fields_touched = True
        self._update_hint()

    #///////////////////////////////////////////////////////////////////////////
    def _on_height_changed(self, _text: str) -> None:
        if not self._suppress_field_events:
            self.pixel_fields_touched = True
        self._update_hint()

    #///////////////////////////////////////////////////////////////////////////
    def _on_aspect_changed(self, _value: bool) -> None:
        self._update_hint()

    #///////////////////////////////////////////////////////////////////////////
    def _on_quality_changed(self, value: int) -> None:
        self.quality_label.configure(text=str(value))

    #///////////////////////////////////////////////////////////////////////////
    def _on_smoothing_changed(self, value: int) -> None:
        """Describe what the smoothing slider is actually doing."""

        filter_name = "резко (Lanczos)" if value < 40 else "мягко (Bicubic)"
        radius = core.blur_radius(value)
        if radius > 0.0:
            filter_name += f" + размытие {radius:.1f}px"
        self.smoothing_label.configure(text=f"{value} — {filter_name}")

    #///////////////////////////////////////////////////////////////////////////
    def _on_confirm_changed(self, value: bool) -> None:
        self.confirm_replace = value

    #///////////////////////////////////////////////////////////////////////////
    def _on_recursive_changed(self, _value: bool) -> None:
        # Re-scan what is already loaded so the count reflects the new setting
        # immediately, rather than only on the next drop.
        self._sync_drop_labels()
        if self.source_paths and not self.busy:
            self._accept(list(self.source_paths), auto_start=False)

    #///////////////////////////////////////////////////////////////////////////
    def _current_options(self) -> Options:
        quality = self.quality_slider.value
        resize_mode = core.RESIZE_NONE
        if self.tool == TOOL_RESIZE:
            resize_mode = (core.RESIZE_PERCENT if self.resize_switch.selected == 0
                           else core.RESIZE_PIXELS)
        return Options(
            quality=quality,
            quality_floor=max(0, quality - 25),
            dithering=1.0 if self.dither_toggle.value else 0.0,
            make_backup=self.backup_toggle.value and self.output == OUTPUT_REPLACE,
            resize_mode=resize_mode,
            resize_percent=float(read_int(self.percent_field.get(), 100)),
            resize_width=read_int(self.width_field.get()),
            resize_height=read_int(self.height_field.get()),
            keep_aspect=self.aspect_toggle.value,
            no_enlarge=self.no_enlarge_toggle.value,
            smoothing=self.smoothing_slider.value,
        )

    #endregion
    #///////////////////////////////////////////////////////////////////////////
    #region intake

    #///////////////////////////////////////////////////////////////////////////
    def _pick_files(self) -> None:
        if self.busy:
            return
        chosen = filedialog.askopenfilenames(
            parent=self.root,
            title="Выберите PNG",
            filetypes=[("PNG изображения", "*.png"), ("Все файлы", "*.*")],
        )
        if chosen:
            self._accept(list(chosen))

    #///////////////////////////////////////////////////////////////////////////
    def _pick_folder(self) -> None:
        if self.busy:
            return
        folder = filedialog.askdirectory(parent=self.root, title="Выберите папку")
        if folder:
            self._accept([folder])

    #///////////////////////////////////////////////////////////////////////////
    def _on_drop(self, event) -> None:
        self.drop_zone.set_hot(False)
        if self.busy:
            return
        try:
            paths = list(self.root.tk.splitlist(event.data))
        except Exception:
            paths = [event.data]
        paths = [path for path in paths if path]
        if paths:
            self._accept(paths)

    #///////////////////////////////////////////////////////////////////////////
    def _accept(self, paths: list[str], auto_start: bool = True) -> None:
        """Turn dropped or picked paths into a queue of rows.

        Folders are walked recursively unless the setting says otherwise, so
        dropping one folder picks up every PNG underneath it at any depth.
        """

        recursive = self.recursive_toggle.value
        found = core.collect_pngs(paths, recursive=recursive)
        if not found:
            where = "в папке" if any(os.path.isdir(p) for p in paths) else "в выбранном"
            extra = "" if recursive else " (обход подпапок выключен)"
            self.summary.set_message(f"PNG не найдены {where}{extra}.", Color.warning)
            return

        folders = [path for path in paths if os.path.isdir(path)]
        self.replace_root = os.path.abspath(folders[0]) if folders else None

        self.source_paths = list(paths)
        self.paths = found
        self.results.clear()
        self.totals = BatchTotals(files=len(found))

        base = self._common_base(found)
        self.rows = []
        for path in found:
            try:
                size = os.path.getsize(path)
            except OSError:
                size = 0
            display = os.path.relpath(path, base).replace(os.sep, "/") if base else path
            self.rows.append(FileRow(path, display, size))

        self.file_list.set_rows(self.rows)
        self._sync_layout()
        self._autofill_pixel_fields(found[0])
        self.action_button.set_enabled(True)

        total_size = sum(row.original_size for row in self.rows)
        self.summary.update_totals(BatchTotals(), 0.0, False, immediate=True)
        self.summary.set_message(
            f"Готово к обработке: {self._file_count(len(found))} "
            f"{self._folder_summary(found)}, {human_size(total_size)}",
            Color.text_dim)

        # The bypass: dropping onto the replace mode just goes.
        if auto_start and self.output == OUTPUT_REPLACE:
            self._start()

    #///////////////////////////////////////////////////////////////////////////
    @staticmethod
    def _folder_summary(paths: list[str]) -> str:
        """Say how many folders the files came from, so recursion is visible."""

        count = len({os.path.dirname(path) for path in paths})
        if count <= 1:
            return "в одной папке"
        return f"в {count} {plural(count, 'папке', 'папках', 'папках')}"

    #///////////////////////////////////////////////////////////////////////////
    @staticmethod
    def _file_count(count: int) -> str:
        return f"{count} {plural(count, 'файл', 'файла', 'файлов')}"

    #///////////////////////////////////////////////////////////////////////////
    def _autofill_pixel_fields(self, sample: str) -> None:
        """Seed the width and height boxes from the first image loaded.

        Only until the user types in them: after that the numbers are theirs
        and a new drop must not overwrite them.
        """

        if self.pixel_fields_touched:
            return
        try:
            from PIL import Image

            with Image.open(sample) as image:
                width, height = image.size
        except Exception:
            return

        self._suppress_field_events = True
        try:
            self.width_field.set(str(width))
            self.height_field.set(str(height))
        finally:
            self._suppress_field_events = False
        self._update_hint()

    #///////////////////////////////////////////////////////////////////////////
    @staticmethod
    def _common_base(paths: list[str]) -> str:
        if not paths:
            return ""
        if len(paths) == 1:
            return os.path.dirname(paths[0])
        try:
            return os.path.commonpath(paths)
        except ValueError:
            return ""

    #endregion
    #///////////////////////////////////////////////////////////////////////////
    #region running

    #///////////////////////////////////////////////////////////////////////////
    def _start(self) -> None:
        if self.busy or not self.rows:
            return

        options = self._current_options()

        if self.tool == TOOL_RESIZE and options.resize_mode == core.RESIZE_PIXELS \
                and not options.resize_width and not options.resize_height:
            self.summary.set_message("Укажите ширину или высоту.", Color.warning)
            return

        if self.output == OUTPUT_REPLACE and self.confirm_replace and not self._confirm():
            return

        self.busy = True
        self.stop_event.clear()
        self.results.clear()
        self.totals = BatchTotals(files=len(self.rows))
        self.last_archive = None

        for row in self.rows:
            row.state = "queued"
            row.detail = ""
            row.new_size = 0
            row.ratio = 0.0
            row.shown_ratio = 0.0
            row.appeared = 0.0
        self.rows[0].state = "working"
        self.file_list.note_activity()
        self.file_list.redraw()

        self.action_button.configure_text("Стоп")
        self.action_button.set_command(self._stop)
        self._lock_controls(True)
        self.secondary_button.pack_forget()
        self.summary.set_message("Обработка…", Color.info)

        worker = threading.Thread(
            target=self._run_batch,
            args=(list(self.paths), options, self.output == OUTPUT_REPLACE),
            daemon=True,
        )
        worker.start()
        self.root.after(60, self._pump)

    #///////////////////////////////////////////////////////////////////////////
    def _lock_controls(self, locked: bool) -> None:
        """Freeze every setting while a batch is in flight.

        The workers were handed a snapshot of the options; letting the user
        move a slider afterwards would show settings that no longer describe
        what is actually running.
        """

        enabled = not locked
        self.clear_button.set_enabled(enabled)
        self.tool_switch.set_enabled(enabled)
        self.output_switch.set_enabled(enabled)
        self.quality_slider.set_enabled(enabled)
        self.dither_toggle.set_enabled(enabled)
        self.recursive_toggle.set_enabled(enabled)
        self.smoothing_slider.set_enabled(enabled)
        self.aspect_toggle.set_enabled(enabled)
        self.no_enlarge_toggle.set_enabled(enabled)
        self.resize_switch.set_enabled(enabled)
        self.percent_field.set_enabled(enabled)
        self.width_field.set_enabled(enabled)
        self.height_field.set_enabled(enabled)

        replacing = self.output == OUTPUT_REPLACE
        self.backup_toggle.set_enabled(enabled and replacing)
        self.confirm_toggle.set_enabled(enabled and replacing)

    #///////////////////////////////////////////////////////////////////////////
    def _confirm(self) -> bool:
        total_size = sum(row.original_size for row in self.rows)
        where = self.replace_root or self._common_base(self.paths) or "выбранных путях"
        what = "сжаты" if self.tool == TOOL_COMPRESS else "изменены в размере и сжаты"
        extra = "\n\nРядом останутся копии .bak." if self.backup_toggle.value else ""
        scope = ("включая все подпапки" if self.recursive_toggle.value
                 else "без обхода подпапок")
        confirmed = messagebox.askyesno(
            "Заменить файлы на месте?",
            f"{len(self.rows)} PNG ({human_size(total_size)}) "
            f"{self._folder_summary(self.paths)} будут {what} "
            f"и записаны поверх исходных.\n\nв: {where}\n({scope})\n\n"
            f"Отменить нельзя." + extra,
            icon="warning",
            parent=self.root,
        )
        if not confirmed:
            self.summary.set_message("Отменено.", Color.text_dim)
        return confirmed

    #///////////////////////////////////////////////////////////////////////////
    def _run_batch(self, paths: list[str], options: Options, in_place: bool) -> None:
        """Worker thread: drive the pool, post every result to the queue."""

        try:
            for result in core.squeeze_many(
                paths, options, in_place=in_place, should_stop=self.stop_event.is_set
            ):
                self.result_queue.put(("result", result))
        except Exception as exc:  # keep the UI alive whatever happens
            self.result_queue.put(("fatal", f"{type(exc).__name__}: {exc}"))
        finally:
            self.result_queue.put(("done", None))

    #///////////////////////////////////////////////////////////////////////////
    def _stop(self) -> None:
        if self.busy:
            self.stop_event.set()
            self.summary.set_message("Останавливаюсь…", Color.warning)
            self.action_button.set_enabled(False)

    #///////////////////////////////////////////////////////////////////////////
    def _pump(self) -> None:
        """Drain the result queue on the Tk thread."""

        finished = False
        fatal: str | None = None
        index = self.totals.done

        while True:
            try:
                kind, payload = self.result_queue.get_nowait()
            except queue.Empty:
                break
            if kind == "done":
                finished = True
            elif kind == "fatal":
                fatal = str(payload)
            elif kind == "result":
                self._absorb(payload, index)
                index += 1

        progress = self.totals.done / max(1, self.totals.files)
        self.summary.update_totals(self.totals, progress, True)
        if not finished and 0 <= self.totals.done < len(self.rows):
            self.rows[self.totals.done].state = "working"
            self.file_list.scroll_to(self.totals.done)
        self.file_list.note_activity()
        self.file_list.redraw()

        if finished:
            self._finish(fatal)
        else:
            self.root.after(60, self._pump)

    #///////////////////////////////////////////////////////////////////////////
    def _absorb(self, result: Result, index: int) -> None:
        """Fold one worker result into the row list and the totals."""

        self.totals.add(result)
        if result.ok:
            self.results[result.source] = result

        if not (0 <= index < len(self.rows)):
            return
        row = self.rows[index]
        row.new_size = result.new_size
        row.ratio = result.saved_ratio

        if not result.ok:
            row.state = "error"
            row.detail = result.error or "не удалось обработать"
            return

        if result.was_resized:
            geometry = (f"{result.source_width}×{result.source_height} → "
                        f"{result.width}×{result.height}")
        else:
            geometry = f"{result.width}×{result.height}"

        if result.method == core.METHOD_QUANTIZED:
            row.state = "done"
            row.detail = (f"{geometry} · {result.colors} цветов · "
                          f"отличие {result.deviation * 100:.2f}%")
        elif result.method == core.METHOD_LOSSLESS:
            row.state = "done"
            row.detail = f"{geometry} · без потерь"
            if result.notes:
                row.detail += f" · {describe_note(result.notes[0])}"
        else:
            row.state = "skipped"
            row.detail = f"{geometry} · уже оптимален"
            if result.notes:
                row.detail += f" · {describe_note(result.notes[0])}"

    #///////////////////////////////////////////////////////////////////////////
    def _finish(self, fatal: str | None) -> None:
        self.busy = False
        self.action_button.set_command(self._start)
        self.action_button.set_enabled(bool(self.rows))
        self._lock_controls(False)
        self._sync_ui()

        # A stopped run leaves the row it was on marked as working, and any
        # number of rows still queued behind it. Put them back to plain queued
        # so nothing spins forever.
        for row in self.rows:
            if row.state == "working":
                row.state = "queued"
        self.file_list.scroll_to(0)
        self.file_list.redraw()

        progress = self.totals.done / max(1, self.totals.files)
        self.summary.update_totals(self.totals, progress, progress < 1.0)

        if fatal:
            self.summary.set_message(f"Сбой: {fatal}", Color.danger)
            return

        prefix = "Остановлено. " if self.stop_event.is_set() else ""
        failed = f", ошибок: {self.totals.failed}" if self.totals.failed else ""
        succeeded = self.totals.done - self.totals.failed

        if self.output == OUTPUT_REPLACE:
            self.summary.set_message(
                f"{prefix}Заменено файлов: {succeeded}{failed}. "
                f"Освобождено {human_size(self.totals.saved_bytes)}.",
                Color.warning if self.totals.failed else Color.accent)
            if self.replace_root:
                self.secondary_button.configure_text("Открыть папку")
                self.secondary_button.set_command(
                    lambda: self._reveal_folder(self.replace_root))
                self.secondary_button.pack(side="left", padx=(0, 10))
            return

        self.summary.set_message(
            f"{prefix}Обработано файлов: {succeeded}{failed}. Собираю архив…", Color.accent)
        if self.results:
            self.root.after(120, self._prompt_save_zip)

    #endregion
    #///////////////////////////////////////////////////////////////////////////
    #region archive

    #///////////////////////////////////////////////////////////////////////////
    def _default_archive_name(self) -> str:
        base = self._common_base(list(self.results.keys()))
        stem = os.path.basename(base.rstrip("\\/")) if base else ""
        suffix = "resized" if self.tool == TOOL_RESIZE else "compressed"
        return f"{stem}-{suffix}.zip" if stem else f"png-squeezer-{suffix}.zip"

    #///////////////////////////////////////////////////////////////////////////
    def _prompt_save_zip(self) -> None:
        """Offer the finished batch as a single archive via the save dialog."""

        payload_paths = [path for path in self.paths if path in self.results]
        if not payload_paths:
            return

        destination = filedialog.asksaveasfilename(
            parent=self.root,
            title="Сохранить архив",
            defaultextension=".zip",
            initialfile=self._default_archive_name(),
            filetypes=[("ZIP архив", "*.zip")],
        )
        if not destination:
            self.summary.set_message(
                "Архив не сохранён. Нажмите «Сохранить ZIP», когда будете готовы.",
                Color.warning)
            self.secondary_button.configure_text("Сохранить ZIP")
            self.secondary_button.set_command(self._prompt_save_zip)
            self.secondary_button.pack(side="left", padx=(0, 10))
            return

        names = core.archive_names(payload_paths)
        payload = []
        for path in payload_paths:
            data = self.results[path].data
            if data is not None:
                payload.append((names[path], data))

        try:
            size = core.write_zip(destination, payload)
        except OSError as exc:
            messagebox.showerror("Не удалось сохранить",
                                 f"{type(exc).__name__}: {exc}", parent=self.root)
            self.summary.set_message("Не удалось записать архив.", Color.danger)
            return

        self.last_archive = destination
        self.summary.set_message(
            f"Архив сохранён: {os.path.basename(destination)} ({human_size(size)}) · "
            f"{len(payload)} файлов", Color.accent)
        self.secondary_button.configure_text("Открыть папку")
        self.secondary_button.set_command(self._reveal_archive)
        self.secondary_button.pack(side="left", padx=(0, 10))

        if messagebox.askyesno(
            "Архив готов",
            f"{os.path.basename(destination)} — {human_size(size)}\n"
            f"Файлов внутри: {len(payload)}\n"
            f"Экономия: {human_size(self.totals.saved_bytes)} "
            f"(−{self.totals.saved_ratio * 100:.1f}%)\n\nОткрыть архив?",
            parent=self.root,
        ):
            try:
                open_path(destination)
            except OSError:
                reveal_in_explorer(destination)

    #///////////////////////////////////////////////////////////////////////////
    def _reveal_archive(self) -> None:
        if self.last_archive and os.path.exists(self.last_archive):
            reveal_in_explorer(self.last_archive)

    #///////////////////////////////////////////////////////////////////////////
    def _reveal_folder(self, folder: str | None) -> None:
        if folder and os.path.isdir(folder):
            if sys.platform == "win32":
                subprocess.run(["explorer", os.path.abspath(folder)], check=False)
            else:
                open_path(folder)

    #endregion
    #///////////////////////////////////////////////////////////////////////////
    #region teardown

    #///////////////////////////////////////////////////////////////////////////
    def _clear(self) -> None:
        if self.busy:
            return
        self.rows = []
        self.paths = []
        self.source_paths = []
        self.results.clear()
        self.totals = BatchTotals()
        self.last_archive = None
        self.replace_root = None
        self.file_list.clear()
        self._sync_layout()
        self.action_button.set_enabled(False)
        self.secondary_button.pack_forget()
        self.summary.update_totals(BatchTotals(), 0.0, False, immediate=True)
        self.summary.set_message("Готово к работе", Color.text_dim)
        self._sync_ui()

    #///////////////////////////////////////////////////////////////////////////
    def _on_close(self) -> None:
        if self.busy:
            if not messagebox.askyesno(
                "Прервать?", "Обработка ещё идёт. Закрыть окно?", parent=self.root
            ):
                return
            self.stop_event.set()
        self.animator.stop()
        self.root.destroy()

    #endregion


#endregion
#///////////////////////////////////////////////////////////////////////////////
#region entry point


#///////////////////////////////////////////////////////////////////////////////
def build_root() -> tk.Tk:
    """Create the Tk root, with drag-and-drop support when it is available."""

    if HAVE_DND:
        return TkinterDnD.Tk()
    return tk.Tk()


def run(initial_paths: list[str] | None = None) -> int:
    """Open the window, optionally pre-loaded with files.

    ``initial_paths`` is what arrives when files or folders are dropped onto
    the application icon or a shortcut, which Windows passes as argv.
    """

    missing = []
    if not core.HAVE_IMAGEQUANT:
        missing.append("imagequant")
    if not core.HAVE_OXIPNG:
        missing.append("pyoxipng")

    root = build_root()
    app = SqueezerApp(root)

    if missing:
        app.summary.set_message(
            "Не установлено: " + ", ".join(missing)
            + " — доступно только сжатие без потерь.",
            Color.warning,
        )

    existing = [path for path in (initial_paths or []) if os.path.exists(path)]
    if existing:
        # Deferred so the window is mapped and sized first; _accept lays rows
        # out against the real widget geometry.
        root.after(250, lambda: app._accept(existing))

    root.mainloop()
    return 0


#endregion
