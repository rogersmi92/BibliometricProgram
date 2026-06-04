from __future__ import annotations

import os

os.environ["TK_SILENCE_DEPRECATION"] = "1"

print(f"Launching DansBib GUI from {__file__}", flush=True)

from BiblioGUI.DansBibGUI.gui import main


if __name__ == "__main__":
    main()
