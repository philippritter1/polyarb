"""Give freed memory back to the system after a large listing (19.10.: ~19 processes on a 1.9 GB server).

Python keeps freed heap pages for reuse, so a process that once parsed 7,000 Gamma markets stays at that size.
glibc's malloc_trim returns them; on other systems this does nothing."""
from __future__ import annotations

import ctypes
import gc

_libc = None


def release_memory() -> None:
    global _libc
    gc.collect()
    try:
        if _libc is None:
            _libc = ctypes.CDLL("libc.so.6")
        _libc.malloc_trim(0)
    except (OSError, AttributeError):
        _libc = False  # not glibc: nothing to do
