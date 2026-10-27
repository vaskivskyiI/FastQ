"""FastQ Bluesky control package (see README.md)."""
import sys

if sys.platform == "win32":
    try:  # load awkward's C++ runtime before numba/numexpr - see control/tiled_server.py
        import awkward  # noqa: F401
    except ImportError:
        pass
