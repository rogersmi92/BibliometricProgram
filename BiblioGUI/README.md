# DansBib GUI

DansBib GUI is a staff-facing desktop launcher for the DansBib bibliometric workflow. It helps users choose a search query, select data sources, include RIS files, run the pipeline, review logs, and open generated outputs without using a terminal.

## Launching The App

From the `BiblioGUI` folder:

```bash
python run_gui.py
```

On first launch, the setup window asks for:

- the `DansBib` folder that contains `main.py`
- the default output folder
- the RIS input folder
- the logs folder

Settings are saved as JSON in the user's application config folder. API keys are not stored by the GUI.

## Choosing A Query

Enter a plain-language research topic in **Main research question or topic**, or use the **Guided Query Builder** to build a structured Boolean query.

The guided builder follows Dan-style Web of Science logic:

- terms in the same concept box are joined with `OR`
- different concept boxes are joined with `AND`
- exclusion terms are added with `AND NOT`
- year range becomes a publication-year filter

Use **Generate Query Preview** to review the numbered query lines, then **Use This Query** to place the generated query into the main query box.

## Selecting RIS Files

RIS files are read from the configured RIS input folder. Use **Browse RIS Folder** to choose a different folder, then **Refresh RIS**.

Use **RIS-only mode** when you want to run only from local RIS files and avoid live API sources.

## Safe Modes

**Dry Run** shows the command and settings that would be used, writes a log file, and does not run DansBib.

**Safe Local Test** forces a local/RIS-style run path and avoids live API confirmation. It is intended for checking setup with local files before running network sources.

Advanced options are hidden by default because they change QA, cache, and extraction behavior.

## Outputs And Logs

Outputs appear in the configured output folder. Current DansBib pipeline versions write primarily to `DansBib/data/outputs`; if a custom output folder is configured, DansBib itself must support that path before files are written there directly.

Each run writes a log file in the configured logs folder. Use **About / Diagnostics** and **Copy diagnostic report** when sending details to Roger or Dan.

## If Something Fails

The GUI shows a plain-English error. Full technical details are saved in the run log. Common causes are:

- the selected DansBib folder does not contain `main.py`
- required Python packages are missing
- a live API source timed out or rejected access
- a RIS file was moved or deleted

## Developer Notes

### Folder Structure

- `run_gui.py`: main launcher
- `DansBibGUI/gui.py`: Tkinter interface and user workflow
- `DansBibGUI/config.py`: static labels and UI defaults kept for compatibility
- `DansBibGUI/utils/app_config.py`: saved JSON settings and default paths
- `DansBibGUI/utils/diagnostics.py`: environment and package checks
- `DansBibGUI/utils/file_utils.py`: cross-platform file/folder helpers
- `DansBibGUI/utils/pipeline_runner.py`: subprocess bridge to DansBib
- `DansBibGUI/utils/query_builder.py`: legacy query-preview helper, not used by the active GUI

### How The GUI Calls DansBib

The GUI builds a `PipelineRequest` and passes it to `pipeline_runner.run_pipeline()`. The runner starts DansBib with a command like:

```bash
python main.py "query terms" --databases openalex,pubmed --ris-files path/to/file.ris
```

The runner uses the configured DansBib path as the subprocess working directory and uses the DansBib virtual environment Python if available. Otherwise it falls back to the current `sys.executable`.

### Testing Without APIs

Use **Dry Run** to verify command construction without running anything.

Use **RIS-only mode** with local RIS files to avoid live API calls.

Use **Safe Local Test** when checking setup on a staff machine before attempting live sources.

### Future Windows Packaging

See `PACKAGING.md`. The preferred first packaging path is PyInstaller, with DansBib kept as an external configured folder until the full pipeline and scientific dependencies are validated on Windows.
