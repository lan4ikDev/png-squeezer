"""Audio re-encoding engine.

Mirrors the image engine: no UI imports, so it can be handed to a
``ProcessPoolExecutor`` worker, and nothing is accepted without being checked
first.

Everything goes through libsndfile (via ``soundfile``), which can both read and
write MP3, Ogg Vorbis, Opus, FLAC and WAV. No external encoder is needed, and
the 84 MB ffmpeg binary that would otherwise be required stays out of the
build.

Two things about that library are load-bearing here:

* **Writing must be done in blocks.** Handing libsndfile a whole 52-second
  track in one ``write`` call kills the process outright -- a hard crash with
  no Python traceback, exit code 0xC00000FD. Feeding it in chunks produces
  byte-identical output and does not crash.
* **``compression_level`` runs backwards.** 0 is best quality and biggest
  file, 1 is worst and smallest. The quality setting exposed here is the human
  way round and gets inverted on the way in.
"""

from __future__ import annotations

import io
import os
import shutil
from dataclasses import dataclass, field

import numpy as np

try:
    import soundfile as sf

    HAVE_SOUNDFILE = True
except Exception:  # pragma: no cover - depends on the machine
    sf = None
    HAVE_SOUNDFILE = False


AUDIO_SUFFIXES = (".mp3", ".ogg", ".wav", ".flac", ".oga", ".opus", ".aiff", ".aif")

METHOD_REENCODED = "reencoded"
METHOD_UNCHANGED = "unchanged"
METHOD_FAILED = "failed"

# Keep the source container unless asked otherwise: changing a file's
# extension breaks every reference to it in a game project.
FORMAT_KEEP = "keep"
FORMAT_OGG = "ogg"
FORMAT_MP3 = "mp3"

NOTE_NO_ENGINE = "no_engine"
NOTE_NOT_SMALLER = "not_smaller"
NOTE_UNSUPPORTED = "unsupported"
NOTE_DAMAGED = "damaged"
NOTE_ERROR = "error"

# Samples per write call. Small enough to keep libsndfile happy, large enough
# that the per-call overhead does not show up.
BLOCK = 8192


#///////////////////////////////////////////////////////////////////////////////
#region options and results


@dataclass(frozen=True)
class AudioOptions:
    """Settings for one audio re-encode."""

    # 0..100, the human way round: 100 keeps the most detail and the most
    # bytes. Inverted into libsndfile's compression_level.
    quality: int = 55
    # Fold stereo to mono. Roughly halves the file and is usually
    # indistinguishable for UI sounds and many game tracks.
    mono: bool = False
    # Which container to write. Keeping the source container is the only safe
    # choice for in-place replacement.
    target_format: str = FORMAT_KEEP
    # Reject a result whose decoded waveform drifts from the original by more
    # than this (1 - correlation). 0.02 is far looser than it sounds; a good
    # re-encode scores 0.999+.
    max_drift: float = 0.02
    make_backup: bool = False

    #///////////////////////////////////////////////////////////////////////////
    def compression_level(self) -> float:
        """Translate quality into libsndfile's backwards scale."""

        return 1.0 - max(0, min(100, int(self.quality))) / 100.0


@dataclass
class AudioResult:
    """The outcome for a single audio file."""

    source: str
    original_size: int = 0
    new_size: int = 0
    method: str = METHOD_FAILED
    duration: float = 0.0
    samplerate: int = 0
    channels_in: int = 0
    channels_out: int = 0
    drift: float = 0.0
    data: bytes | None = None
    suffix: str = ""
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


#endregion
#///////////////////////////////////////////////////////////////////////////////
#region helpers


#///////////////////////////////////////////////////////////////////////////////
def is_audio(path: str) -> bool:
    return path.lower().endswith(AUDIO_SUFFIXES)


#///////////////////////////////////////////////////////////////////////////////
def _target_container(suffix: str, options: AudioOptions) -> tuple[str, str, str]:
    """Return ``(sndfile_format, subtype, output_suffix)``."""

    lowered = suffix.lower()
    if options.target_format == FORMAT_OGG:
        return "OGG", "VORBIS", ".ogg"
    if options.target_format == FORMAT_MP3:
        return "MP3", "MPEG_LAYER_III", ".mp3"

    if lowered in (".ogg", ".oga"):
        return "OGG", "VORBIS", lowered
    if lowered == ".opus":
        return "OGG", "OPUS", ".opus"
    if lowered == ".mp3":
        return "MP3", "MPEG_LAYER_III", ".mp3"
    if lowered == ".flac":
        return "FLAC", "PCM_16", ".flac"
    if lowered in (".wav", ".aiff", ".aif"):
        # Re-encoding WAV as WAV saves nothing; Vorbis is the point.
        return "OGG", "VORBIS", ".ogg"
    return "OGG", "VORBIS", ".ogg"


#///////////////////////////////////////////////////////////////////////////////
def _encode(samples: np.ndarray, rate: int, fmt: str, subtype: str,
            level: float) -> bytes:
    """Write samples to an in-memory file, in blocks.

    The blocking is not an optimisation. A single large write crashes the
    process inside libsndfile with no recoverable error.
    """

    buffer = io.BytesIO()
    with sf.SoundFile(buffer, mode="w", samplerate=rate,
                      channels=samples.shape[1], format=fmt, subtype=subtype,
                      compression_level=level) as handle:
        for start in range(0, len(samples), BLOCK):
            handle.write(samples[start:start + BLOCK])
    return buffer.getvalue()


#///////////////////////////////////////////////////////////////////////////////
def _drift(original: np.ndarray, decoded: np.ndarray) -> float:
    """How far the re-encoded waveform moved, as ``1 - correlation``.

    Lossy codecs shift samples slightly, so subtracting the two signals is
    meaningless; correlation of the mono mixdown is not.
    """

    length = min(len(original), len(decoded))
    if length < 1024:
        return 0.0
    left = original[:length].mean(axis=1).astype(np.float64)
    right = decoded[:length].mean(axis=1).astype(np.float64)
    if left.std() < 1e-9 or right.std() < 1e-9:
        # Silence, or a constant tone: correlation is undefined but nothing
        # can have gone visibly wrong either.
        return 0.0
    correlation = float(np.corrcoef(left, right)[0, 1])
    if not np.isfinite(correlation):
        return 0.0
    return max(0.0, 1.0 - correlation)


#endregion
#///////////////////////////////////////////////////////////////////////////////
#region the engine


#///////////////////////////////////////////////////////////////////////////////
def squeeze_audio_bytes(data: bytes, suffix: str, options: AudioOptions,
                        label: str = "") -> AudioResult:
    """Re-encode one audio file held in memory.

    Never raises for a bad file: the failure lands in ``AudioResult.error``.
    """

    result = AudioResult(source=label, original_size=len(data), suffix=suffix)

    if not HAVE_SOUNDFILE:
        result.method = METHOD_UNCHANGED
        result.new_size = len(data)
        result.data = data
        result.notes.append(NOTE_NO_ENGINE)
        return result

    try:
        samples, rate = sf.read(io.BytesIO(data), always_2d=True, dtype="float32")
    except Exception as exc:
        result.error = f"{type(exc).__name__}: {exc}"
        return result

    if samples.size == 0 or rate <= 0:
        result.error = "пустой или нечитаемый аудиофайл"
        return result

    result.samplerate = int(rate)
    result.channels_in = int(samples.shape[1])
    result.duration = len(samples) / float(rate)

    source = samples
    if options.mono and samples.shape[1] > 1:
        samples = samples.mean(axis=1, keepdims=True)
    result.channels_out = int(samples.shape[1])

    fmt, subtype, out_suffix = _target_container(suffix, options)

    try:
        candidate = _encode(samples, int(rate), fmt, subtype,
                            options.compression_level())
    except Exception as exc:
        result.notes.append(f"{NOTE_ERROR}:{type(exc).__name__}: {exc}")
        result.method = METHOD_UNCHANGED
        result.new_size = len(data)
        result.data = data
        return result

    # Prove it decodes, and that it still sounds like the original.
    try:
        decoded, decoded_rate = sf.read(io.BytesIO(candidate), always_2d=True,
                                        dtype="float32")
    except Exception as exc:
        result.notes.append(f"{NOTE_DAMAGED}:{type(exc).__name__}")
        result.method = METHOD_UNCHANGED
        result.new_size = len(data)
        result.data = data
        return result

    drift = _drift(source if not options.mono else samples, decoded)
    duration_out = len(decoded) / float(decoded_rate or 1)
    duration_ok = abs(duration_out - result.duration) <= max(0.1, result.duration * 0.02)

    if drift > options.max_drift or not duration_ok:
        result.notes.append(f"{NOTE_DAMAGED}:{drift * 100:.1f}")
        result.method = METHOD_UNCHANGED
        result.new_size = len(data)
        result.data = data
        result.drift = drift
        return result

    result.drift = drift

    # Same rule as the image side: a result that is not smaller is not a
    # result. Re-encoding an already-compressed track usually inflates it.
    keeping_container = out_suffix.lower() == suffix.lower()
    if len(candidate) >= len(data) and keeping_container:
        result.method = METHOD_UNCHANGED
        result.new_size = len(data)
        result.data = data
        result.notes.append(NOTE_NOT_SMALLER)
        return result

    result.data = candidate
    result.new_size = len(candidate)
    result.suffix = out_suffix
    result.method = METHOD_REENCODED
    return result


#///////////////////////////////////////////////////////////////////////////////
def squeeze_audio_file(path: str, options: AudioOptions) -> AudioResult:
    """Re-encode a file from disk, handing the bytes back in memory."""

    try:
        with open(path, "rb") as handle:
            data = handle.read()
    except OSError as exc:
        return AudioResult(source=path, error=f"{type(exc).__name__}: {exc}")
    return squeeze_audio_bytes(data, os.path.splitext(path)[1], options, label=path)


#///////////////////////////////////////////////////////////////////////////////
def squeeze_audio_file_in_place(path: str, options: AudioOptions) -> AudioResult:
    """Re-encode and replace on disk, atomically.

    If the container changed the new file is written beside the old one under
    the new extension and the original is left alone -- silently deleting a
    file that a project still references by name would be worse than leaving
    a duplicate.
    """

    result = squeeze_audio_file(path, options)
    if not result.ok or result.data is None:
        return result
    if result.method == METHOD_UNCHANGED:
        result.data = None
        return result

    directory = os.path.dirname(os.path.abspath(path))
    stem = os.path.splitext(os.path.basename(path))[0]
    changed_container = result.suffix.lower() != os.path.splitext(path)[1].lower()
    destination = (os.path.join(directory, stem + result.suffix)
                   if changed_container else path)

    temp_path = os.path.join(directory, f".{os.path.basename(destination)}.squeeze-tmp")
    try:
        if options.make_backup and not changed_container:
            backup = path + ".bak"
            if not os.path.exists(backup):
                shutil.copy2(path, backup)
        with open(temp_path, "wb") as handle:
            handle.write(result.data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, destination)
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
#region gathering and batching


#///////////////////////////////////////////////////////////////////////////////
def collect_audio(paths, recursive: bool = True) -> list[str]:
    """Expand files and folders into a sorted list of audio files."""

    from .core import natural_key

    found: list[str] = []
    seen: set[str] = set()

    def add(candidate: str) -> None:
        key = os.path.normcase(os.path.abspath(candidate))
        if key not in seen and is_audio(candidate):
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
def _audio_worker(args):
    path, options, in_place = args
    if in_place:
        return squeeze_audio_file_in_place(path, options)
    return squeeze_audio_file(path, options)


#///////////////////////////////////////////////////////////////////////////////
def squeeze_audio_many(paths, options: AudioOptions, in_place: bool,
                       workers: int | None = None, should_stop=None):
    """Re-encode a list of files, yielding each result as it lands.

    Runs in processes for the same reason the image side does -- encoding is
    CPU bound -- and because a codec that crashes takes only its worker with
    it rather than the whole window.
    """

    if not paths:
        return

    if workers is None:
        workers = max(1, min(len(paths), (os.cpu_count() or 4)))

    if workers == 1 or len(paths) == 1:
        for path in paths:
            if should_stop is not None and should_stop():
                return
            yield _audio_worker((path, options, in_place))
        return

    from concurrent.futures import ProcessPoolExecutor

    tasks = [(path, options, in_place) for path in paths]
    with ProcessPoolExecutor(max_workers=workers) as pool:
        try:
            for result in pool.map(_audio_worker, tasks, chunksize=1):
                yield result
                if should_stop is not None and should_stop():
                    break
        finally:
            if should_stop is not None and should_stop():
                pool.shutdown(wait=False, cancel_futures=True)


#endregion
