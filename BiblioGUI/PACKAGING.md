# Packaging Notes

This project is being prepared for a future Windows export, but it is not packaged yet.

## Recommended Option

Start with **PyInstaller** for the first staff-facing Windows build.

Reasons:

- works well for a Tkinter desktop app
- can produce a single executable or an app folder
- simple enough for internal deployment
- lets DansBib remain external and user-configurable while the pipeline is still evolving

Expected starting command:

```bash
pyinstaller --name DansBibGUI --windowed --onedir run_gui.py
```

After validation, consider adding an icon and a custom spec file.

## Other Options

**Briefcase**

Good for polished native app packaging, but it adds project structure and packaging conventions that are heavier than needed right now.

**auto-py-to-exe**

Useful as a PyInstaller GUI wrapper. It is convenient for experimentation, but the final build should keep a reproducible PyInstaller command or `.spec` file.

## Known Dependencies

GUI-only:

- Python 3.10+
- Tkinter, normally included with Python

Pipeline dependencies are owned by DansBib and should be installed from the DansBib requirements file. Common packages include:

- pandas
- requests
- matplotlib
- networkx
- plotly
- seaborn
- pycountry
- rapidfuzz

## Bundle With The GUI

- `run_gui.py`
- `DansBibGUI/`
- `README.md`
- `PACKAGING.md`
- `requirements-gui.txt`

## Keep External/User Configurable

- DansBib project folder
- DansBib `data/raw` RIS files
- DansBib output folders
- logs folder
- API credentials and institutional access settings
- large reference/cache files
- virtual environments

## Important Current Limitation

The GUI stores a configurable output folder, but current DansBib pipeline code primarily writes to `DansBib/data/outputs` internally. Before a final packaged release that promises arbitrary output locations, DansBib should expose a real `--output-dir` option or honor an environment variable consistently.

## Packaging Day Checklist

- Build on Windows, not only macOS.
- Confirm Tkinter is included in the target Python distribution.
- Confirm the GUI starts without a terminal window.
- Confirm first-run setup can select the DansBib folder.
- Confirm Dry Run works without network access.
- Confirm RIS-only mode works with a local sample RIS file.
- Confirm live API runs fail gracefully when credentials/network access are unavailable.
- Confirm logs are created under the configured logs folder.
- Confirm About / Diagnostics copies useful troubleshooting text.
