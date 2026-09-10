"""Entry point.

``py -m png_squeezer`` opens the window. Given paths on the command line it
runs headless instead, which is the practical way to sweep a few hundred
textures without watching a progress bar.

Nothing heavy is imported at module scope: on Windows the process pool uses
``spawn``, and every worker re-imports this module as ``__mp_main__``. Keeping
Tk out of the import path saves that cost in every worker.
"""

from __future__ import annotations

import argparse
import multiprocessing
import os
import sys


#///////////////////////////////////////////////////////////////////////////////
def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="png_squeezer",
        description="Сжатие PNG в 8-битную палитру (libimagequant + oxipng). "
                    "Без аргументов открывает графическое окно.",
    )
    parser.add_argument("paths", nargs="*",
                        help="файлы или папки; папки обходятся рекурсивно")
    parser.add_argument("--no-recursive", action="store_true",
                        help="не заходить в подпапки, брать только верхний уровень")
    parser.add_argument("-q", "--quality", type=int, default=82, metavar="N",
                        help="целевое качество 40..100 (по умолчанию 82)")
    parser.add_argument("-c", "--colors", type=int, default=256, metavar="N",
                        help="максимум цветов в палитре (по умолчанию 256)")
    parser.add_argument("-e", "--effort", type=int, default=3, metavar="N",
                        help="усилие oxipng 0..6 (по умолчанию 3)")
    parser.add_argument("--dither", action="store_true",
                        help="включить дизеринг Флойда-Стайнберга (крупнее файл, "
                             "но меньше полос на плавных градиентах)")
    parser.add_argument("--lossless", action="store_true",
                        help="только перепаковка без потерь, без квантования")
    parser.add_argument("-o", "--output", metavar="ZIP",
                        help="записать результат в ZIP вместо замены файлов")
    parser.add_argument("--in-place", action="store_true",
                        help="заменить исходные файлы сжатыми")
    parser.add_argument("--backup", action="store_true",
                        help="с --in-place оставить копии .bak")
    parser.add_argument("-n", "--dry-run", action="store_true",
                        help="только посчитать, ничего не записывать")
    parser.add_argument("-j", "--jobs", type=int, default=None, metavar="N",
                        help="число процессов (по умолчанию по числу ядер)")

    resize = parser.add_argument_group("изменение размера")
    resize.add_argument("--percent", type=float, default=None, metavar="N",
                        help="масштабировать до N%% от исходного размера")
    resize.add_argument("--width", type=int, default=0, metavar="PX",
                        help="целевая ширина в пикселях")
    resize.add_argument("--height", type=int, default=0, metavar="PX",
                        help="целевая высота в пикселях")
    resize.add_argument("--no-aspect", action="store_true",
                        help="не сохранять пропорции (растянуть точно в размер)")
    resize.add_argument("--enlarge", action="store_true",
                        help="разрешить увеличение маленьких изображений")
    resize.add_argument("--smoothing", type=int, default=50, metavar="N",
                        help="сглаживание 0..100: 0 резко (Lanczos), "
                             "50 мягче (Bicubic), 100 с размытием")
    return parser


#///////////////////////////////////////////////////////////////////////////////
def _run_cli(args: argparse.Namespace) -> int:
    from . import core

    files = core.collect_pngs(args.paths, recursive=not args.no_recursive)
    if not files:
        print("PNG не найдены.", file=sys.stderr)
        return 1

    if not args.dry_run and not args.in_place and not args.output:
        print("Укажите --in-place, -o ARCHIVE.zip или --dry-run.", file=sys.stderr)
        return 2

    if args.in_place and args.output:
        print("--in-place и --output взаимоисключающи.", file=sys.stderr)
        return 2

    if args.percent is not None:
        resize_mode = core.RESIZE_PERCENT
    elif args.width or args.height:
        resize_mode = core.RESIZE_PIXELS
    else:
        resize_mode = core.RESIZE_NONE

    options = core.Options(
        quality=max(1, min(100, args.quality)),
        quality_floor=max(0, min(99, args.quality - 25)),
        max_colors=max(2, min(256, args.colors)),
        dithering=1.0 if args.dither else 0.0,
        effort=max(0, min(6, args.effort)),
        lossy=not args.lossless,
        make_backup=args.backup,
        resize_mode=resize_mode,
        resize_percent=args.percent if args.percent is not None else 100.0,
        resize_width=max(0, args.width),
        resize_height=max(0, args.height),
        keep_aspect=not args.no_aspect,
        no_enlarge=not args.enlarge,
        smoothing=max(0, min(100, args.smoothing)),
    )

    total_before = sum(os.path.getsize(p) for p in files)
    folders = len({os.path.dirname(p) for p in files})
    print(f"{len(files)} файлов в {folders} папках, {core.human_size(total_before)}")

    in_place = bool(args.in_place and not args.dry_run)
    totals = core.BatchTotals(files=len(files))
    payload: list[tuple[str, bytes]] = []
    names = core.archive_names(files) if args.output else {}
    keep_bytes = bool(args.output) and not args.dry_run

    for result in core.squeeze_many(files, options, in_place=in_place, workers=args.jobs):
        totals.add(result)
        label = os.path.relpath(result.source) if result.source else "?"
        if not result.ok:
            print(f"  FAIL  {label}: {result.error}")
            continue
        print(f"  {label}  {core.human_size(result.original_size)} -> "
              f"{core.human_size(result.new_size)}  "
              f"-{result.saved_ratio * 100:.1f}%  [{result.method}]")
        if keep_bytes and result.data is not None:
            payload.append((names[result.source], result.data))

    print()
    print(f"ИТОГО  {core.human_size(totals.original_size)} -> "
          f"{core.human_size(totals.new_size)}  "
          f"(-{totals.saved_ratio * 100:.1f}%, "
          f"экономия {core.human_size(totals.saved_bytes)})")
    if totals.failed:
        print(f"ошибок: {totals.failed}")

    if args.dry_run:
        print("dry-run: ни один файл не изменён")
    elif args.output:
        size = core.write_zip(args.output, payload)
        print(f"архив: {args.output} ({core.human_size(size)})")

    return 1 if totals.failed else 0


#///////////////////////////////////////////////////////////////////////////////
def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        from .app import run

        return run()

    args = _build_parser().parse_args(argv)
    if not args.paths:
        from .app import run

        return run()
    return _run_cli(args)


#///////////////////////////////////////////////////////////////////////////////
if __name__ == "__main__":
    multiprocessing.freeze_support()
    sys.exit(main())
