from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def open_path(path: Path | str) -> None:
    target = Path(path).expanduser()
    if not target.exists():
        raise FileNotFoundError(f"Path does not exist: {target}")

    if sys.platform.startswith("win"):
        os.startfile(str(target))  # type: ignore[attr-defined]
        return
    if sys.platform == "darwin":
        subprocess.run(["open", str(target)], check=False)
        return
    subprocess.run(["xdg-open", str(target)], check=False)


def ensure_folder(path: Path | str) -> Path:
    folder = Path(path).expanduser()
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def display_path(path: Path | str, base: Path | str | None = None) -> str:
    target = Path(path).expanduser()
    if base:
        try:
            return str(target.relative_to(Path(base).expanduser()))
        except ValueError:
            pass
    return str(target)
