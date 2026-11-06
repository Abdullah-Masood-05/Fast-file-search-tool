"""Entry script.

Keep this file light: a spawned engine process re-runs the top level of the entry
script, so nothing heavy (Qt, config, sqlite) may be imported here at module level.
"""
import multiprocessing
import os
import sys

# Source checkouts run from ./src; frozen builds carry the package.
_SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "src")
if os.path.isdir(_SRC) and _SRC not in sys.path:
    sys.path.insert(0, _SRC)


def _run():
    from fast_file_search.main import main

    return main()


if __name__ == "__main__":
    multiprocessing.freeze_support()
    sys.exit(_run())
