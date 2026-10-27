"""`tiled` CLI with a Windows DLL-order workaround (used by start_control_host.py).

On transport PC (Python 3.14) awkward's C++ extension crashes (access violation)
if numba / numexpr load the Microsoft C++ runtime first - an older
msvcp140.dll gets picked. Importing awkward first avoids it. The proper fix is
an up-to-date "Microsoft Visual C++ Redistributable" on the host.
"""
import runpy
import sys

import awkward  # noqa: F401  - must be the first C++ extension loaded

sys.argv = ["tiled", *sys.argv[1:]]
runpy.run_module("tiled", run_name="__main__")
