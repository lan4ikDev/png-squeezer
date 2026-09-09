"""Entry point for the standalone build.

PyInstaller freezes this file. It stays deliberately thin because on Windows
every worker process re-runs the executable: ``freeze_support()`` intercepts
that before anything else happens, and the Tk import is kept inside ``main``
so a worker never pays for loading a GUI it will not draw.
"""

from __future__ import annotations

import multiprocessing
import sys


#///////////////////////////////////////////////////////////////////////////////
def main() -> int:
    from png_squeezer.app import run

    # Files and folders dropped onto the .exe or a shortcut arrive as argv.
    return run(initial_paths=sys.argv[1:])


#///////////////////////////////////////////////////////////////////////////////
if __name__ == "__main__":
    multiprocessing.freeze_support()
    sys.exit(main())
