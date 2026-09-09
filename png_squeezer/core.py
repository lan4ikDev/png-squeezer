"""Compression engine for PNG Squeezer.

This module is deliberately free of any UI import so it can be sent to a
``ProcessPoolExecutor`` worker on Windows, where every worker re-imports the
module it was handed.

The pipeline is the one pngquant/TinyPNG use:

    RGBA  ->  libimagequant (median cut + Floyd-Steinberg)  ->  8-bit palette
          ->  oxipng (filter search + deflate, palette bit-depth reduction)

Every result is checked before it is accepted: a candidate that came out
bigger than the original, or that moved a visible pixel further than the
caller allows, is thrown away and the lossless re-pack is used instead.
"""

from __future__ import annotations

import io
import os
import re
import shutil
import zipfile
from dataclasses import dataclass, field
from typing import Callable, Iterable, Iterator, Sequence

import numpy as np
from PIL import Image, ImageFilter

try:  # the real pngquant engine; the tool still works without it
    import imagequant

    HAVE_IMAGEQUANT = True
except Exception:  # pragma: no cover - depends on the machine
    imagequant = None
    HAVE_IMAGEQUANT = False

try:  # the lossless re-packer
    import oxipng

    HAVE_OXIPNG = True
except Exception:  # pragma: no cover - depends on the machine
    oxipng = None
    HAVE_OXIPNG = False


# Pillow refuses giant images by default as a decompression-bomb guard. Game
# sprite sheets legitimately get large, so raise the ceiling rather than hit it.
Image.MAX_IMAGE_PIXELS = 512 * 1024 * 1024

PNG_SUFFIXES = (".png",)

METHOD_QUANTIZED = "quantized"
METHOD_LOSSLESS = "lossless"
METHOD_UNCHANGED = "unchanged"
METHOD_FAILED = "failed"

RESIZE_NONE = "none"
RESIZE_PERCENT = "percent"
RESIZE_PIXELS = "pixels"

# Why a file was not quantised. These are codes, not sentences: the engine
# stays language-neutral and the UI decides how to word them.
NOTE_NO_ENGINE = "no_engine"
NOTE_ANIMATED = "animated"
NOTE_PALETTE = "palette"
NOTE_ALPHA = "alpha"
NOTE_DEVIATION = "deviation"
NOTE_FLAT = "flat"
NOTE_COLLAPSE = "collapse"
NOTE_NOT_SMALLER = "not_smaller"
NOTE_QUALITY = "quality"
NOTE_ERROR = "error"


#///////////////////////////////////////////////////////////////////////////////
#region options and results


@dataclass(frozen=True)
class Options:
    """Everything the engine needs to know, in one picklable object."""

    # How rich a palette the image is allowed. Lower means fewer colours and
    # a smaller file. This is NOT handed to libimagequant as a quality
    # ceiling -- see quantizer_bounds() for why that was a disaster.
    quality: int = 82
    # Kept for compatibility with older call sites; no longer used to squeeze
    # the quantiser, only as documentation of intent.
    quality_floor: int = 55
    max_colors: int = 256
    # Off by default, and measured rather than assumed: Floyd-Steinberg noise
    # costs about 20% of the file on this kind of art (328 KB against 273 KB
    # on a jumpscare frame) and does not even improve the error metrics,
    # because the dither pattern itself reads as deviation. Worth turning on
    # for wide smooth gradients, where banding is the bigger evil.
    dithering: float = 0.0
    # oxipng effort, 0..6. Higher is slower and a little smaller.
    effort: int = 3
    # False turns the whole thing into a lossless optimiser.
    lossy: bool = True
    # Reject a quantised candidate whose mean visible deviation exceeds this
    # fraction of full scale. 0.02 == 2% == about 5 levels out of 255.
    max_deviation: float = 0.02
    # And reject one whose error is large *relative to the image's own
    # contrast*. An absolute limit alone is blind on dark frames: a night
    # scene spanning values 0..36 can be flattened to two shades while the
    # mean error stays near 1%, which is what wrecked the door animations.
    max_relative_deviation: float = 0.10
    strip_metadata: bool = True
    # Let oxipng rewrite fully transparent pixels for better packing.
    optimize_alpha: bool = True
    # Keep a <name>.png.bak next to each file replaced in place.
    make_backup: bool = False

    # --- resizing, applied before the image is ever compressed ---------------
    resize_mode: str = RESIZE_NONE
    resize_percent: float = 100.0
    resize_width: int = 0
    resize_height: int = 0
    keep_aspect: bool = True
    no_enlarge: bool = True
    # 0 = sharpest (Lanczos, every pixel crisp), 100 = softest. Lanczos alone
    # leaves resized game art looking crunchier than what iLoveIMG and friends
    # produce, and the palette step afterwards exaggerates it further, so the
    # default sits at a gentler bicubic.
    smoothing: int = 50

    #///////////////////////////////////////////////////////////////////////////
    def quantizer_bounds(self) -> tuple[int, int]:
        """Return the ``(min_quality, max_quality)`` pair for libimagequant.

        Always ``(0, 100)``, deliberately.

        ``max_quality`` is a *ceiling*: when libimagequant finds it can do
        better than the ceiling, it throws colours away until the result sinks
        to it. Its quality metric is near-blind on dark images -- a night
        frame whose values span 0..36 still scores highly with two colours --
        so a ceiling of 82 collapsed those frames to two shades and reported a
        1% error while doing it. The palette size is controlled by
        :meth:`palette_size` instead, and whether the result is acceptable is
        decided by measuring it, not by trusting the library's own score.
        """

        return 0, 100

    #///////////////////////////////////////////////////////////////////////////
    def palette_size(self) -> int:
        """The largest palette the search may consider."""

        return max(2, min(256, int(self.max_colors)))

    #///////////////////////////////////////////////////////////////////////////
    def quality_targets(self) -> tuple[float, float]:
        """Return the ``(absolute, relative)`` error the slider is willing to accept.

        These drive the palette search: the smallest palette whose error stays
        inside both budgets wins. Quality 100 is nearly lossless, 40 is
        visibly cheaper but still nothing like the two-colour disaster the
        hard limits guard against.
        """

        # These decide how far down PALETTE_LADDER the search may walk; the
        # ladder's floor is what actually protects the dark frames. The
        # budgets are deliberately loose enough that a detailed bright texture
        # still qualifies -- tightening them to 2% pushed those into lossless
        # and cost half the compression, because busy images measure worse
        # than flat ones at the same perceived quality.
        fraction = (100 - max(40, min(100, int(self.quality)))) / 60.0
        absolute = 0.004 + fraction * (0.016 - 0.004)
        relative = 0.020 + fraction * (0.070 - 0.020)
        return absolute, relative


@dataclass
class Result:
    """The outcome for a single file."""

    source: str
    original_size: int = 0
    new_size: int = 0
    method: str = METHOD_FAILED
    colors: int = 0
    deviation: float = 0.0
    # The size that was written out.
    width: int = 0
    height: int = 0
    # The size the file had on disk, before any resizing.
    source_width: int = 0
    source_height: int = 0
    data: bytes | None = None
    error: str | None = None
    notes: list[str] = field(default_factory=list)

    #///////////////////////////////////////////////////////////////////////////
    @property
    def ok(self) -> bool:
        return self.error is None

    #///////////////////////////////////////////////////////////////////////////
    @property
    def saved_bytes(self) -> int:
        return max(0, self.original_size - self.new_size)

    #///////////////////////////////////////////////////////////////////////////
    @property
    def saved_ratio(self) -> float:
        if self.original_size <= 0:
            return 0.0
        return self.saved_bytes / self.original_size

    #///////////////////////////////////////////////////////////////////////////
    @property
    def was_resized(self) -> bool:
        return bool(self.source_width) and (
            self.width != self.source_width or self.height != self.source_height
        )


#endregion
#///////////////////////////////////////////////////////////////////////////////
#region low level helpers


#///////////////////////////////////////////////////////////////////////////////
def human_size(num_bytes: float) -> str:
    """Format a byte count the way a file manager would."""

    step = 1024.0
    value = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if abs(value) < step or unit == "GB":
            if unit == "B":
                return f"{int(value)} {unit}"
            return f"{value:.1f} {unit}"
        value /= step
    return f"{value:.1f} GB"


#///////////////////////////////////////////////////////////////////////////////
def _encode_png(image: Image.Image) -> bytes:
    """Serialise a Pillow image to PNG bytes without Pillow's own optimiser.

    oxipng runs afterwards and does a far better job, so paying for
    ``optimize=True`` here would be wasted time.
    """

    buffer = io.BytesIO()
    image.save(buffer, format="PNG", optimize=False, compress_level=6)
    return buffer.getvalue()


#///////////////////////////////////////////////////////////////////////////////
def _repack(data: bytes, options: Options, allow_alpha_rewrite: bool) -> bytes:
    """Run oxipng over PNG bytes, returning the input unchanged on failure."""

    if not HAVE_OXIPNG:
        return data
    kwargs: dict = {
        "level": max(0, min(6, int(options.effort))),
        "interlace": oxipng.Interlacing.Off,
        "optimize_alpha": bool(allow_alpha_rewrite and options.optimize_alpha),
    }
    if options.strip_metadata:
        kwargs["strip"] = oxipng.StripChunks.safe()
    try:
        out = oxipng.optimize_from_memory(data, **kwargs)
    except Exception:
        return data
    return out if len(out) < len(data) else data


#///////////////////////////////////////////////////////////////////////////////
def _as_rgba_array(image: Image.Image) -> np.ndarray:
    return np.asarray(image.convert("RGBA"), dtype=np.int16)


#///////////////////////////////////////////////////////////////////////////////
def _deviation(before: np.ndarray, after: np.ndarray) -> tuple[float, float, bool]:
    """Measure how far a candidate drifted from the original.

    Returns ``(absolute, relative, alpha_is_identical)``.

    ``absolute`` is the alpha-weighted mean error as a fraction of full scale,
    so pixels the player cannot see -- which ``optimize_alpha`` is free to
    rewrite -- do not count against the result.

    ``relative`` is that same error divided by the image's own contrast. It
    exists because the absolute number is meaningless on dark art: a frame
    whose visible values only span 0..36 can be flattened to two shades for an
    absolute error near 1%, which looks catastrophic but passes any fixed
    threshold. Dividing by the spread of the original makes the two cases
    comparable.
    """

    alpha_before = before[..., 3]
    alpha_after = after[..., 3]
    alpha_identical = bool(np.array_equal(alpha_before, alpha_after))

    weight = alpha_before.astype(np.float32) / 255.0
    total_weight = float(weight.sum())
    if total_weight <= 0.0:
        # Fully transparent image: nothing is visible, nothing can be wrong.
        return 0.0, 0.0, alpha_identical

    rgb_delta = np.abs(before[..., :3] - after[..., :3]).astype(np.float32).mean(axis=2)
    alpha_delta = np.abs(alpha_before - alpha_after).astype(np.float32)
    # Alpha errors are visible everywhere, not only where the pixel was opaque.
    visible = float((rgb_delta * weight).sum()) / total_weight
    visible = max(visible, float(alpha_delta.mean()))

    # Contrast of the original, measured only where it is actually visible.
    mask = alpha_before > 8
    if mask.sum() > 1:
        contrast = float(before[..., :3][mask].astype(np.float32).std())
    else:
        contrast = 0.0
    # The +1 keeps a flat image from dividing by zero and stops the ratio from
    # exploding on art that genuinely has almost no contrast to lose.
    relative = visible / (contrast + 1.0)

    return visible / 255.0, relative, alpha_identical


#///////////////////////////////////////////////////////////////////////////////
def _blur_rgba(image: Image.Image, radius: float) -> Image.Image:
    """Gaussian blur that does not drag transparent colour into the edges.

    Blurring RGBA straight would mix whatever colour sits under alpha 0 into
    neighbouring visible pixels -- usually black, so sprites gain a dark rim.
    Fully opaque images (most game frames) skip that entirely and take the
    fast path.
    """

    if radius <= 0.0:
        return image

    alpha = image.getchannel("A")
    if alpha.getextrema() == (255, 255):
        return image.filter(ImageFilter.GaussianBlur(radius))

    # Premultiply, blur, divide back out. The whole thing stays in RGBA uint8
    # because GaussianBlur rejects Pillow's float "F" mode outright.
    source = np.asarray(image, dtype=np.float32)
    weight = source[..., 3:4] / 255.0

    premultiplied = np.empty_like(source)
    premultiplied[..., :3] = source[..., :3] * weight
    premultiplied[..., 3] = source[..., 3]

    blurred = np.asarray(
        Image.fromarray(np.rint(premultiplied).astype(np.uint8))
        .filter(ImageFilter.GaussianBlur(radius)),
        dtype=np.float32,
    )

    alpha = blurred[..., 3:4] / 255.0
    divisor = np.where(alpha > 1e-6, alpha, 1.0)

    output = np.empty_like(blurred)
    output[..., :3] = np.clip(blurred[..., :3] / divisor, 0.0, 255.0)
    output[..., 3] = blurred[..., 3]

    return Image.fromarray(np.rint(output).astype(np.uint8))


#///////////////////////////////////////////////////////////////////////////////
def resample_filter(smoothing: int) -> int:
    """Pick the resampling filter for a smoothing setting of 0..100."""

    return Image.LANCZOS if int(smoothing) < 40 else Image.BICUBIC


#///////////////////////////////////////////////////////////////////////////////
def blur_radius(smoothing: int) -> float:
    """Extra softening past what the filter alone provides."""

    return max(0.0, (int(smoothing) - 60) / 40.0) * 0.8


#///////////////////////////////////////////////////////////////////////////////
def resize_rgba(image: Image.Image, size: tuple[int, int],
                smoothing: int = 50) -> Image.Image:
    """Resample an RGBA image down (or up) to ``size``.

    Pillow already resamples RGBA through premultiplied alpha, so the colour
    of fully transparent pixels cannot bleed into the visible edge and there
    is no halo to correct for. This was verified rather than assumed: a white
    disc on a transparent *red* background comes back with the transparent
    side turned black and the rim still pure white, which is only possible if
    the resampler premultiplied. Doing the premultiply by hand in numpy was
    measured against this and agreed to within rounding (0.13/255 mean, 4/255
    worst) while running 3.3x slower, so the hand-rolled version was dropped.

    ``smoothing`` runs 0..100. Lanczos is the sharpest filter available and is
    what the tool used to use unconditionally, but on resized game art it
    leaves visibly crunchier pixels than other resizers produce -- and the
    palette step downstream makes that worse, not better, because flat blocks
    of colour read as blockiness. Past 40 the gentler bicubic takes over, and
    past 60 a small gaussian is added on top.
    """

    resized = image.convert("RGBA").resize(size, resample_filter(smoothing))
    return _blur_rgba(resized, blur_radius(smoothing))


#///////////////////////////////////////////////////////////////////////////////
def target_size(width: int, height: int, options: Options) -> tuple[int, int]:
    """Work out what an image should be resized to.

    Percent mode scales both sides. Pixel mode honours whichever of width and
    height was filled in: with ``keep_aspect`` and both filled in, the image is
    fitted inside that box rather than stretched to it.
    """

    if options.resize_mode == RESIZE_PERCENT:
        factor = max(0.01, float(options.resize_percent) / 100.0)
        new_width = int(round(width * factor))
        new_height = int(round(height * factor))
    elif options.resize_mode == RESIZE_PIXELS:
        want_width = max(0, int(options.resize_width))
        want_height = max(0, int(options.resize_height))
        if not want_width and not want_height:
            return width, height
        if options.keep_aspect:
            if want_width and want_height:
                factor = min(want_width / width, want_height / height)
            elif want_width:
                factor = want_width / width
            else:
                factor = want_height / height
            new_width = int(round(width * factor))
            new_height = int(round(height * factor))
        else:
            new_width = want_width or width
            new_height = want_height or height
    else:
        return width, height

    if options.no_enlarge and (new_width > width or new_height > height):
        return width, height

    return max(1, new_width), max(1, new_height)


#///////////////////////////////////////////////////////////////////////////////
# Palette sizes the search walks, largest first. Coarse on purpose: the file
# size difference between 96 and 104 colours is not worth another quantisation
# pass, and each step here costs real time.
#
# It stops at 48 rather than going lower, and that floor is the single most
# important number here. Error statistics cannot tell "fine" from "ruined" on
# this kind of art -- a bright detailed frame at 128 colours measures *worse*
# than a dark frame flattened to 20, because detail masks error and flat
# shadow does not. libimagequant's own score is no better: it rates the ruined
# 20-colour version 99 out of 100. What did separate every case checked by eye
# was the raw palette size: everything at 28 colours and up looked right,
# everything at 20 and below had lost highlights. 48 leaves margin.
PALETTE_LADDER = (256, 192, 144, 112, 80, 64, 48)


#///////////////////////////////////////////////////////////////////////////////
def _choose_palette(
    rgba: Image.Image,
    options: Options,
    source: np.ndarray,
) -> tuple[Image.Image | None, int, float, str | None]:
    """Find the smallest palette that still meets the quality budget.

    Returns ``(quantized_image, colours, absolute_deviation, failure_note)``.

    This is where most of the compression comes from. A fixed palette wastes
    space on images that do not need it -- a frame that looks identical with
    112 colours does not benefit from 198 -- so the size is searched instead
    of assumed, which is what TinyPNG does and what this tool did not.

    Quality is judged on the quantised pixels directly, before the PNG is ever
    encoded. Encoding is the expensive half, and running oxipng once per probe
    would make the search cost more than it saves.
    """

    ceiling = options.palette_size()
    ladder = [size for size in PALETTE_LADDER if size <= ceiling] or [ceiling]
    absolute_budget, relative_budget = options.quality_targets()

    def attempt(colors: int):
        """Quantise at this size and report whether it is good enough."""
        try:
            image = imagequant.quantize_pil_image(
                rgba,
                dithering_level=float(options.dithering),
                max_colors=int(colors),
                min_quality=0,
                max_quality=100,
            )
        except Exception:
            return None
        absolute, relative, alpha_ok = _deviation(source, _as_rgba_array(image))
        ok = alpha_ok and absolute <= absolute_budget and relative <= relative_budget
        return image, absolute, relative, alpha_ok, ok

    # The ladder runs large to small, and quality falls as it goes, so the
    # acceptable entries form a prefix. Binary search for its last index.
    best = None
    low, high = 0, len(ladder) - 1
    probe = attempt(ladder[0])
    if probe is None:
        return None, 0, 0.0, NOTE_QUALITY
    if not probe[4]:
        # Even the richest palette misses the budget: report why.
        _image, absolute, relative, alpha_ok, _ok = probe
        if not alpha_ok:
            return None, 0, 0.0, NOTE_ALPHA
        if relative > relative_budget:
            return None, 0, 0.0, f"{NOTE_FLAT}:{relative * 100:.0f}"
        return None, 0, 0.0, f"{NOTE_DEVIATION}:{absolute * 100:.1f}"

    best = (probe[0], ladder[0], probe[1])
    low = 1
    while low <= high:
        middle = (low + high) // 2
        probe = attempt(ladder[middle])
        if probe is not None and probe[4]:
            best = (probe[0], ladder[middle], probe[1])
            low = middle + 1          # try to go smaller still
        else:
            high = middle - 1

    return best[0], best[1], best[2], None


#///////////////////////////////////////////////////////////////////////////////
def _count_colors(image: Image.Image) -> int:
    colors = image.getcolors(1 << 20)
    return len(colors) if colors else 0


#endregion
#///////////////////////////////////////////////////////////////////////////////
#region the engine


#///////////////////////////////////////////////////////////////////////////////
def squeeze_bytes(data: bytes, options: Options, label: str = "") -> Result:
    """Compress one PNG held in memory.

    Never raises for a bad image: the failure lands in ``Result.error``.
    """

    result = Result(source=label, original_size=len(data))

    try:
        with Image.open(io.BytesIO(data)) as probe:
            probe.load()
            source_mode = probe.mode
            result.source_width, result.source_height = probe.size
            frames = int(getattr(probe, "n_frames", 1) or 1)
            rgba = probe.convert("RGBA")
    except Exception as exc:
        result.error = f"{type(exc).__name__}: {exc}"
        return result

    # --- resize first; everything downstream works on the new pixels ---------
    # An animated PNG is deliberately left alone: Pillow hands us only the
    # first frame, so resizing one would silently throw the animation away.
    animated = frames > 1
    wanted = target_size(result.source_width, result.source_height, options)
    resized = wanted != (result.source_width, result.source_height) and not animated
    if resized:
        try:
            rgba = resize_rgba(rgba, wanted, options.smoothing)
        except Exception as exc:
            result.error = f"resize failed - {type(exc).__name__}: {exc}"
            return result
    result.width, result.height = rgba.size

    # A lossless re-pack is both the fallback and the floor every lossy
    # candidate has to beat. Once the image has been resized the bytes on disk
    # are no longer a candidate at all -- they are the wrong size.
    if resized:
        lossless = _repack(_encode_png(rgba), options, allow_alpha_rewrite=False)
        method = METHOD_LOSSLESS
    else:
        lossless = _repack(data, options, allow_alpha_rewrite=False)
        method = METHOD_LOSSLESS if len(lossless) < len(data) else METHOD_UNCHANGED
    best = lossless
    colors = 0
    deviation = 0.0

    already_small_palette = (
        not resized
        and source_mode == "P"
        and _count_colors(rgba) <= options.max_colors
    )

    reason: str | None = None
    if animated:
        # Worth saying even in lossless mode, because it also explains why a
        # requested resize did not happen.
        reason = NOTE_ANIMATED
    elif not options.lossy:
        reason = None  # lossless was asked for; nothing to explain
    elif not HAVE_IMAGEQUANT:
        reason = NOTE_NO_ENGINE
    elif already_small_palette:
        reason = NOTE_PALETTE

    if options.lossy and reason is None:
        # More than 64 distinct colours going in? getcolors returns None once
        # it passes the limit, which is all we need and costs nothing.
        source_is_rich = rgba.getcolors(64) is None
        try:
            source_array = _as_rgba_array(rgba)
            quantized, _picked, _err, search_note = _choose_palette(
                rgba, options, source_array)

            if quantized is None:
                reason = search_note
            else:
                candidate = _repack(_encode_png(quantized), options,
                                    allow_alpha_rewrite=True)

                # Re-measure on what will actually be written: oxipng may have
                # rewritten transparent pixels since the search ran.
                with Image.open(io.BytesIO(candidate)) as check:
                    check.load()
                    absolute, relative, alpha_ok = _deviation(
                        source_array, _as_rgba_array(check))

                colors_used = _count_colors(quantized)

                if not alpha_ok:
                    reason = NOTE_ALPHA
                elif absolute > options.max_deviation:
                    reason = f"{NOTE_DEVIATION}:{absolute * 100:.1f}"
                elif relative > options.max_relative_deviation:
                    reason = f"{NOTE_FLAT}:{relative * 100:.0f}"
                elif source_is_rich and colors_used < 8:
                    # Last-ditch guard: a detailed image reduced to a handful
                    # of shades is destroyed whatever the error metrics say.
                    reason = f"{NOTE_COLLAPSE}:{colors_used}"
                elif len(candidate) >= len(best):
                    reason = NOTE_NOT_SMALLER
                else:
                    best = candidate
                    method = METHOD_QUANTIZED
                    colors = colors_used
                    deviation = absolute
        except RuntimeError:
            reason = NOTE_QUALITY
        except Exception as exc:
            reason = f"{NOTE_ERROR}:{type(exc).__name__}: {exc}"

    # Falling back to the original bytes is only allowed when the pixels were
    # left alone; after a resize they are simply the wrong image.
    if not resized and len(best) >= len(data):
        best = data
        method = METHOD_UNCHANGED
        colors = 0
        deviation = 0.0

    result.data = best
    result.new_size = len(best)
    result.method = method
    result.colors = colors
    result.deviation = deviation
    if reason:
        result.notes.append(reason)
    return result


#///////////////////////////////////////////////////////////////////////////////
def squeeze_file(path: str, options: Options) -> Result:
    """Compress a file from disk and hand the bytes back in memory."""

    try:
        with open(path, "rb") as handle:
            data = handle.read()
    except OSError as exc:
        return Result(source=path, error=f"{type(exc).__name__}: {exc}")
    return squeeze_bytes(data, options, label=path)


#///////////////////////////////////////////////////////////////////////////////
def squeeze_file_in_place(path: str, options: Options) -> Result:
    """Compress a file and replace it on disk.

    The new bytes go to a sibling temporary file first and are moved over the
    original with ``os.replace``, so an interrupted run can never leave a
    half-written texture behind. ``Result.data`` is dropped afterwards to keep
    batch memory flat.
    """

    result = squeeze_file(path, options)
    if not result.ok or result.data is None:
        return result

    if result.method == METHOD_UNCHANGED:
        result.data = None
        return result

    directory = os.path.dirname(os.path.abspath(path))
    temp_path = os.path.join(directory, f".{os.path.basename(path)}.squeeze-tmp")
    try:
        if options.make_backup:
            backup = path + ".bak"
            # Never clobber an existing backup: it holds the pristine original
            # from an earlier run, which is exactly what must be kept.
            if not os.path.exists(backup):
                shutil.copy2(path, backup)
        with open(temp_path, "wb") as handle:
            handle.write(result.data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    except OSError as exc:
        result.error = f"{type(exc).__name__}: {exc}"
        try:
            if os.path.exists(temp_path):
                os.remove(temp_path)
        except OSError:
            pass
    finally:
        result.data = None
    return result


#endregion
#///////////////////////////////////////////////////////////////////////////////
#region gathering files


#///////////////////////////////////////////////////////////////////////////////
def is_png(path: str) -> bool:
    return path.lower().endswith(PNG_SUFFIXES)


#///////////////////////////////////////////////////////////////////////////////
def natural_key(path: str) -> list:
    """Sort key that orders ``2.png`` before ``10.png``.

    Animation frames are numbered, and plain lexicographic order interleaves
    them into nonsense (1, 10, 11, 2). Splitting the digits out fixes it.
    """

    parts = re.split(r"(\d+)", path.lower())
    return [int(part) if part.isdigit() else part for part in parts]


#///////////////////////////////////////////////////////////////////////////////
def collect_pngs(paths: Iterable[str], recursive: bool = True) -> list[str]:
    """Expand a mixed list of files and folders into a sorted list of PNGs."""

    found: list[str] = []
    seen: set[str] = set()

    def add(candidate: str) -> None:
        key = os.path.normcase(os.path.abspath(candidate))
        if key not in seen and is_png(candidate):
            seen.add(key)
            found.append(os.path.abspath(candidate))

    for raw in paths:
        path = os.path.abspath(raw)
        if os.path.isdir(path):
            if recursive:
                for directory, _subdirs, files in os.walk(path):
                    for name in files:
                        add(os.path.join(directory, name))
            else:
                for name in os.listdir(path):
                    full = os.path.join(path, name)
                    if os.path.isfile(full):
                        add(full)
        elif os.path.isfile(path):
            add(path)

    found.sort(key=natural_key)
    return found


#///////////////////////////////////////////////////////////////////////////////
def archive_names(paths: Sequence[str]) -> dict[str, str]:
    """Pick a name inside the zip for every path, keeping folders readable.

    Files that share a common parent keep their relative structure; anything
    that would still collide gets a ``name (2).png`` style suffix.
    """

    if not paths:
        return {}

    absolute = [os.path.abspath(p) for p in paths]
    try:
        base = os.path.commonpath(absolute) if len(absolute) > 1 else os.path.dirname(absolute[0])
    except ValueError:  # different drives on Windows
        base = ""

    names: dict[str, str] = {}
    used: set[str] = set()
    for path in absolute:
        if base:
            try:
                relative = os.path.relpath(path, base)
            except ValueError:
                relative = os.path.basename(path)
        else:
            relative = os.path.basename(path)
        # Only separators are normalised. Stripping leading dots would rename
        # a legitimate ".hidden.png" to "hidden.png".
        relative = relative.replace(os.sep, "/")

        candidate = relative
        stem, extension = os.path.splitext(relative)
        counter = 2
        while candidate.lower() in used:
            candidate = f"{stem} ({counter}){extension}"
            counter += 1
        used.add(candidate.lower())
        names[path] = candidate
    return names


#///////////////////////////////////////////////////////////////////////////////
def write_zip(destination: str, payload: Sequence[tuple[str, bytes]]) -> int:
    """Write ``(name_in_zip, data)`` pairs to a zip and return its size.

    PNG bytes are already deflated, so the archive is stored rather than
    compressed again -- it saves a few seconds and costs a fraction of a
    percent.
    """

    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_STORED) as archive:
        for name, data in payload:
            archive.writestr(name, data)
    return os.path.getsize(destination)


#endregion
#///////////////////////////////////////////////////////////////////////////////
#region batch driver


@dataclass
class BatchTotals:
    """Running totals for a batch, so the UI does not have to add them up."""

    files: int = 0
    done: int = 0
    failed: int = 0
    original_size: int = 0
    new_size: int = 0

    #///////////////////////////////////////////////////////////////////////////
    @property
    def saved_bytes(self) -> int:
        return max(0, self.original_size - self.new_size)

    #///////////////////////////////////////////////////////////////////////////
    @property
    def saved_ratio(self) -> float:
        if self.original_size <= 0:
            return 0.0
        return self.saved_bytes / self.original_size

    #///////////////////////////////////////////////////////////////////////////
    def add(self, result: Result) -> None:
        self.done += 1
        if not result.ok:
            self.failed += 1
            return
        self.original_size += result.original_size
        self.new_size += result.new_size


#///////////////////////////////////////////////////////////////////////////////
def _worker(args: tuple[str, Options, bool]) -> Result:
    """Entry point executed inside a pool worker."""

    path, options, in_place = args
    if in_place:
        return squeeze_file_in_place(path, options)
    return squeeze_file(path, options)


#///////////////////////////////////////////////////////////////////////////////
def squeeze_many(
    paths: Sequence[str],
    options: Options,
    in_place: bool,
    workers: int | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> Iterator[Result]:
    """Compress a list of files, yielding each ``Result`` as it lands.

    Work is spread over processes because libimagequant and oxipng are both
    CPU bound. A single file is done inline -- spinning up a pool for it costs
    more than the compression.
    """

    if not paths:
        return

    if workers is None:
        workers = max(1, min(len(paths), (os.cpu_count() or 4)))

    if workers == 1 or len(paths) == 1:
        for path in paths:
            if should_stop is not None and should_stop():
                return
            yield _worker((path, options, in_place))
        return

    # Imported here so a machine without a working pool can still run inline.
    from concurrent.futures import ProcessPoolExecutor

    tasks = [(path, options, in_place) for path in paths]
    with ProcessPoolExecutor(max_workers=workers) as pool:
        try:
            for result in pool.map(_worker, tasks, chunksize=1):
                yield result
                if should_stop is not None and should_stop():
                    break
        finally:
            # Stopping early should not wait for the whole queue to drain.
            if should_stop is not None and should_stop():
                pool.shutdown(wait=False, cancel_futures=True)


#endregion
