"""Shared runtime path helpers."""

from __future__ import annotations

import os
from pathlib import Path


def matplotlib_cache_dir(root: Path | str) -> Path:
    """Return the shared Matplotlib cache directory for a DansBib checkout."""
    return Path(root) / "data" / "cache" / "matplotlib"


def configure_matplotlib_cache(root: Path | str) -> Path:
    """Create and set the shared Matplotlib cache directory."""
    cache_dir = matplotlib_cache_dir(root)
    cache_dir.mkdir(parents=True, exist_ok=True)
    os.environ["MPLCONFIGDIR"] = str(cache_dir)
    return cache_dir
