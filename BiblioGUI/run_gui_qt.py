from __future__ import annotations

import os
import sys
import traceback


def main() -> int:
    os.environ.setdefault("QT_MAC_WANTS_LAYER", "1")
    print("Starting DansBib Qt GUI...", flush=True)
    try:
        from DansBibGUI.qt_gui import main as qt_main
    except ModuleNotFoundError as exc:
        if exc.name == "PySide6":
            print(
                "PySide6 is not installed. Install GUI dependencies with:\n"
                "  python3 -m pip install -r requirements-gui.txt",
                file=sys.stderr,
                flush=True,
            )
            return 1
        traceback.print_exc()
        return 1
    except Exception:
        traceback.print_exc()
        return 1
    smoke_path = None
    if "--smoke-shot" in sys.argv:
        index = sys.argv.index("--smoke-shot")
        if len(sys.argv) <= index + 1:
            print("--smoke-shot requires an output PNG path.", file=sys.stderr, flush=True)
            return 2
        smoke_path = sys.argv[index + 1]
    return qt_main(smoke_path=smoke_path)


if __name__ == "__main__":
    raise SystemExit(main())
