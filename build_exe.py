"""Build PNG Squeezer into a single standalone .exe.

    py build_exe.py                 -> dist/PNG Squeezer.exe
    py build_exe.py --dest "C:\\..." -> also copies it there

The result needs no Python installed. Everything the app touches at runtime
has to be collected explicitly: tkinterdnd2 ships a compiled ``tkdnd`` Tcl
package that PyInstaller cannot see through the Tcl loader, and imagequant is
a cffi extension whose binary is easy to miss.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
APP_NAME = "PNG Squeezer"


#///////////////////////////////////////////////////////////////////////////////
def make_icon(path: str) -> str | None:
    """Draw the app icon and save it as a multi-resolution .ico."""

    try:
        from PIL import Image, ImageDraw
    except ImportError:
        return None

    base = 256
    image = Image.new("RGBA", (base, base), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle([8, 8, base - 9, base - 9], radius=56,
                           fill="#161A21", outline="#3DDC97", width=10)
    # The same "squeeze" arrow the window and the title bar use.
    scale = base / 64.0
    arrow = [(16, 22), (30, 22), (30, 14), (44, 32), (30, 50), (30, 42), (16, 42)]
    draw.polygon([(x * scale, y * scale) for x, y in arrow], fill="#3DDC97")

    image.save(path, format="ICO",
               sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    return path


#///////////////////////////////////////////////////////////////////////////////
def build(work_dir: str, one_file: bool) -> str:
    icon_path = make_icon(os.path.join(work_dir, "icon.ico"))

    command = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm",
        "--clean",
        "--windowed",                 # no console window behind the app
        "--name", APP_NAME,
        "--distpath", os.path.join(work_dir, "dist"),
        "--workpath", os.path.join(work_dir, "build"),
        "--specpath", work_dir,
        # tkdnd is a Tcl package loaded at runtime; without this the drop
        # target silently fails to register and drag-and-drop does nothing.
        "--collect-all", "tkinterdnd2",
        "--collect-all", "imagequant",
        "--collect-binaries", "oxipng",
        # soundfile ships libsndfile as a bundled native library that
        # PyInstaller cannot find by following imports.
        "--collect-all", "soundfile",
        # Nothing in the app plots anything, and imageio-ffmpeg carries an
        # 84 MB binary the audio path deliberately does not use.
        "--exclude-module", "matplotlib",
        "--exclude-module", "scipy",
        "--exclude-module", "pandas",
        "--exclude-module", "pytest",
        "--exclude-module", "imageio_ffmpeg",
    ]
    if one_file:
        command.append("--onefile")
    if icon_path:
        command += ["--icon", icon_path]
    command.append(os.path.join(HERE, "main.py"))

    print(" ".join(command))
    subprocess.run(command, check=True, cwd=HERE)

    if one_file:
        return os.path.join(work_dir, "dist", f"{APP_NAME}.exe")
    return os.path.join(work_dir, "dist", APP_NAME)


#///////////////////////////////////////////////////////////////////////////////
def main() -> int:
    parser = argparse.ArgumentParser(description="Собрать PNG Squeezer в .exe")
    parser.add_argument("--dest", help="куда скопировать готовое приложение")
    parser.add_argument("--work", default=os.path.join(HERE, ".build"),
                        help="рабочая папка сборки")
    parser.add_argument("--onedir", action="store_true",
                        help="собрать папкой вместо одного файла (быстрее стартует)")
    args = parser.parse_args()

    os.makedirs(args.work, exist_ok=True)
    produced = build(args.work, one_file=not args.onedir)
    print(f"\nсобрано: {produced}")

    if args.dest:
        target = os.path.join(args.dest, os.path.basename(produced))
        if os.path.isdir(produced):
            if os.path.exists(target):
                shutil.rmtree(target)
            shutil.copytree(produced, target)
        else:
            os.makedirs(args.dest, exist_ok=True)
            shutil.copy2(produced, target)
        size = (os.path.getsize(target) if os.path.isfile(target)
                else sum(os.path.getsize(os.path.join(d, f))
                         for d, _s, fs in os.walk(target) for f in fs))
        print(f"скопировано: {target} ({size / 1048576:.0f} MB)")

    return 0


#///////////////////////////////////////////////////////////////////////////////
if __name__ == "__main__":
    sys.exit(main())
