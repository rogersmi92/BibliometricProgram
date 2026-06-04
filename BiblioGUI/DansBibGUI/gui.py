from __future__ import annotations

import queue
import threading
import tkinter as tk
import traceback
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk
from typing import Callable

from .config import APP_TITLE, LOG_POLL_INTERVAL_MS, SOURCE_LABELS
from .utils.app_config import AppConfig, load_config, save_config
from .utils.diagnostics import collect_diagnostics, friendly_error_message
from .utils.file_utils import open_path
from .utils.pipeline_runner import (
    CONCEPT_PROFILES,
    LIVE_API_SOURCES,
    PipelineRequest,
    SCALING_MODES,
    create_run_log,
    list_ris_files,
    paths_from_config,
    run_pipeline,
    run_visualizations,
    run_vos_networks,
    run_vos_validator,
)
from .utils.query_builder import (
    FIELD_TARGETS,
    PRESETS,
    ConceptBlock,
    build_wos_numbered_query,
    parse_terms,
)


BLOCK_DEFINITIONS = (
    ("geography", "Geography / population terms", "west texas, texas, rural, southwest united states"),
    ("topic", "Main topic / disease terms", "cancer treatment, cancer screening, neoplasms, oncology"),
    ("intervention", "Intervention / method terms", "telemedicine, telehealth, mobile health, remote consultation"),
    ("outcomes", "Outcomes / access terms", "healthcare access, survival, mortality, outcomes"),
    ("exclusion", "Exclusion terms", "animal"),
)


class DansBibApp(tk.Tk):
    def __init__(self) -> None:
        self._startup_log("app start")
        super().__init__()
        self.title(APP_TITLE)
        self.geometry("1280x900")
        self.minsize(1080, 760)
        self.configure(bg="red")
        self._startup_log("root/window configured")

        try:
            self.settings: AppConfig = load_config()
            self._startup_log("settings loaded")
            self.runtime_paths = paths_from_config(self.settings)
            self._startup_log("diagnostics initialized")
            self.log_queue: "queue.Queue[tuple[str, object]]" = queue.Queue()
            self.worker_thread: threading.Thread | None = None
            self.core_dataset: str | None = None
            self.output_paths: dict[str, str] = {}
            self.output_files: list[str] = []
            self.ris_vars: dict[Path, tk.BooleanVar] = {}
            self.block_terms: dict[str, tk.StringVar] = {}
            self.block_fields: dict[str, tk.StringVar] = {}

            self._configure_style()
            self._build_ui()
            self.update_idletasks()
            self._startup_log("app ready")
            self.after(LOG_POLL_INTERVAL_MS, self.process_messages)
            self.after(100, self._force_initial_paint)
            self.after(500, self._post_startup_render_check)
            self._startup_log("setup dialog checked/opened")
            if not self.settings.is_ready():
                self.after(200, self.show_setup_dialog)
        except Exception as exc:
            formatted = traceback.format_exc()
            print(formatted, flush=True)
            self._show_startup_failure(exc, formatted)

    def _configure_style(self) -> None:
        self.configure(bg="red")
        style = ttk.Style(self)
        print(f"[DansBib GUI startup] ttk themes available: {style.theme_names()}", flush=True)
        style.theme_use("clam")
        print(f"[DansBib GUI startup] ttk theme in use: {style.theme_use()}", flush=True)
        style.configure("TFrame", background="#00ffff")
        style.configure("Panel.TFrame", background="#7cfc00")
        style.configure("TLabel", background="#00ffff", foreground="#000000")
        style.configure("Panel.TLabel", background="#7cfc00", foreground="#000000")
        style.configure("TCheckbutton", background="#7cfc00", foreground="#000000")
        style.configure("TLabelframe", background="#ffb6c1", foreground="#000000", padding=12)
        style.configure("TLabelframe.Label", background="#ffb6c1", foreground="#000000", font=("Helvetica", 11, "bold"))
        style.configure("TNotebook", background="#ff00ff", foreground="#000000")
        style.configure("TNotebook.Tab", background="#ffff00", foreground="#000000", padding=(12, 6))
        style.configure("Primary.TButton", font=("Helvetica", 11, "bold"), padding=(14, 8))
        style.configure("Secondary.TButton", padding=(10, 6))
        style.configure("Danger.TLabel", background="#ffb6c1", foreground="#000000")
        style.configure("Muted.TLabel", background="#7cfc00", foreground="#000000")

    def _startup_log(self, message: str) -> None:
        print(f"[DansBib GUI startup] {message}", flush=True)

    def _show_startup_failure(self, exc: BaseException, formatted: str) -> None:
        self.title(f"{APP_TITLE} - startup failed")
        fallback = tk.Frame(self, bg="white", padx=24, pady=24)
        fallback.pack(fill="both", expand=True)
        fallback.grid_columnconfigure(0, weight=1)
        tk.Label(
            fallback,
            text="DansBib GUI startup failed",
            font=("Helvetica", 18, "bold"),
            bg="white",
            fg="#991b1b",
        ).grid(row=0, column=0, sticky="w")
        tk.Label(
            fallback,
            text="The full traceback was printed to the terminal. Copy diagnostics or quit and check the terminal/log.",
            bg="white",
            fg="#111827",
            wraplength=720,
            justify="left",
        ).grid(row=1, column=0, sticky="w", pady=(8, 12))
        text = tk.Text(fallback, height=14, wrap="word")
        text.grid(row=2, column=0, sticky="nsew")
        fallback.grid_rowconfigure(2, weight=1)
        text.insert("1.0", f"{exc}\n\n{formatted}")
        text.configure(state="disabled")
        buttons = tk.Frame(fallback, bg="white")
        buttons.grid(row=3, column=0, sticky="ew", pady=(12, 0))
        tk.Button(buttons, text="Copy diagnostics", command=lambda: self._copy_startup_diagnostics(formatted)).pack(side="left")
        tk.Button(buttons, text="Quit", command=self.destroy).pack(side="right")
        try:
            messagebox.showerror("GUI startup failed", "DansBib GUI startup failed. See the terminal/log for technical details.")
        except Exception:
            pass

    def _copy_startup_diagnostics(self, text: str) -> None:
        self.clipboard_clear()
        self.clipboard_append(text)

    def _widget_text(self, widget: tk.Misc) -> str:
        for option in ("text", "title"):
            try:
                value = widget.cget(option)  # type: ignore[attr-defined]
            except Exception:
                continue
            if value:
                return str(value)
        return ""

    def _dump_widget_tree(self, widget: tk.Misc | None = None, depth: int = 0) -> None:
        widget = widget or self
        indent = "  " * depth
        try:
            manager = widget.winfo_manager()
        except Exception:
            manager = "unknown"
        try:
            mapped = widget.winfo_ismapped()
            req = f"{widget.winfo_reqwidth()}x{widget.winfo_reqheight()}"
            actual = f"{widget.winfo_width()}x{widget.winfo_height()}"
            klass = widget.winfo_class()
            parent = widget.winfo_parent()
        except Exception:
            mapped = "unknown"
            req = "unknown"
            actual = "unknown"
            klass = widget.__class__.__name__
            parent = "unknown"
        text = self._widget_text(widget)
        print(
            f"[DansBib GUI widget] {indent}{str(widget)} class={klass} parent={parent} "
            f"manager={manager or 'none'} mapped={mapped} req={req} actual={actual} text={text!r}",
            flush=True,
        )
        for child in widget.winfo_children():
            self._dump_widget_tree(child, depth + 1)

    def _verify_rendered_ui(self) -> None:
        self.update_idletasks()
        checks = {
            "root has children": bool(self.winfo_children()),
            "main_frame managed": bool(self.main_frame.winfo_manager()),
            "main_frame visible": bool(self.main_frame.winfo_ismapped()),
            "notebook managed": bool(self.notebook.winfo_manager()),
            "notebook visible": bool(self.notebook.winfo_ismapped()),
            "notebook has at least two tabs": len(self.notebook.tabs()) >= 2,
            "Build & Run tab has children": bool(self.build_tab.winfo_children()),
            "Outputs & Logs tab has children": bool(self.outputs_tab.winfo_children()),
        }
        for label, ok in checks.items():
            print(f"[DansBib GUI verify] {label}: {'yes' if ok else 'NO'}", flush=True)

        original_tab = self.notebook.select()
        for tab_id, label in ((self.build_tab, "Build & Run"), (self.outputs_tab, "Outputs & Logs")):
            self.notebook.select(tab_id)
            self.update_idletasks()
            visible_children = [child for child in tab_id.winfo_children() if child.winfo_ismapped()]
            print(
                f"[DansBib GUI verify] {label} visible children after selecting tab: {len(visible_children)}",
                flush=True,
            )
        if original_tab:
            self.notebook.select(original_tab)
            self.update_idletasks()

    def _post_startup_render_check(self) -> None:
        print("[DansBib GUI startup] post-mainloop render check", flush=True)
        self._verify_rendered_ui()
        self._dump_widget_tree()

    def _build_ui(self) -> None:
        self.root_paint_label = tk.Label(
            self,
            text="VISIBLE ROOT PAINT TEST",
            bg="yellow",
            fg="black",
            font=("Arial", 24, "bold"),
        )
        self.root_paint_label.pack(fill="x")

        self.main_frame = ttk.Frame(self)
        self.main_frame.pack(fill="both", expand=True)
        self.main_frame.grid_rowconfigure(1, weight=1)
        self.main_frame.grid_columnconfigure(0, weight=1)
        self._startup_log("main container created")

        header = ttk.Frame(self.main_frame, padding=(18, 14), style="Panel.TFrame")
        header.pack(fill="x")
        ttk.Label(header, text="DansBib GUI", font=("Helvetica", 20, "bold"), style="Panel.TLabel").pack(side="left")
        ttk.Label(header, text="guided bibliometric pipeline launcher", font=("Helvetica", 12), style="Panel.TLabel").pack(side="left", padx=(12, 0))
        ttk.Button(header, text="Setup", command=self.show_setup_dialog).pack(side="right", padx=(8, 0))
        ttk.Button(header, text="About / Diagnostics", command=self.show_diagnostics).pack(side="right")

        self.notebook = ttk.Notebook(self.main_frame)
        self.notebook.pack(fill="both", expand=True, padx=14, pady=14)
        self.build_tab = ttk.Frame(self.notebook)
        self.outputs_tab = ttk.Frame(self.notebook)
        self.notebook.add(self.build_tab, text="Build & Run")
        self.notebook.add(self.outputs_tab, text="Outputs & Logs")
        self._startup_log("tabs created")

        self._build_run_tab()
        self._startup_log("Build & Run tab rendered")
        self._build_outputs_tab()
        self._startup_log("Outputs & Logs tab rendered")

    def _build_run_tab(self) -> None:
        self.build_tab.columnconfigure(0, weight=3)
        self.build_tab.columnconfigure(1, weight=2)
        self.build_tab.rowconfigure(0, weight=0)
        self.build_tab.rowconfigure(1, weight=1)

        tk.Label(
            self.build_tab,
            text="BUILD TAB PAINT TEST",
            bg="orange",
            fg="black",
            font=("Arial", 20, "bold"),
        ).grid(row=0, column=0, columnspan=2, sticky="ew")

        left = ttk.Frame(self.build_tab, style="Panel.TFrame", padding=16)
        right = ttk.Frame(self.build_tab, style="Panel.TFrame", padding=16)
        left.grid(row=1, column=0, sticky="nsew", padx=(0, 10), pady=(8, 0))
        right.grid(row=1, column=1, sticky="nsew", pady=(8, 0))
        left.columnconfigure(0, weight=1)
        right.columnconfigure(0, weight=1)

        self._build_query_area(left)
        self._build_guided_builder(left)
        self._build_source_panel(right)
        self._build_ris_panel(right)
        self._build_options_panel(right)
        self._build_action_panel(right)

    def _build_query_area(self, parent: ttk.Frame) -> None:
        frame = ttk.LabelFrame(parent, text="Search query", padding=14)
        frame.grid(row=0, column=0, sticky="ew")
        frame.columnconfigure(0, weight=1)
        ttk.Label(frame, text="Main research question or topic.", font=("Helvetica", 13, "bold"), style="Panel.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(frame, text="You can type naturally, or generate a structured query below.", style="Muted.TLabel").grid(row=1, column=0, sticky="w", pady=(2, 8))
        self.query_text = tk.Text(frame, height=4, wrap="word", font=("Helvetica", 12), relief="solid", bd=1)
        self.query_text.grid(row=2, column=0, sticky="ew")
        self.query_placeholder = "Example: telehealth cancer treatment in rural West Texas"
        self.query_placeholder_active = True
        self.query_text.insert("1.0", self.query_placeholder)
        self.query_text.configure(fg="#6b7280")
        self.query_text.bind("<FocusIn>", self._clear_query_placeholder)
        self.query_text.bind("<FocusOut>", self._restore_query_placeholder)
        self.query_text.bind("<KeyRelease>", lambda _event: self._update_run_state())
        self.query_error_var = tk.StringVar(value="")
        ttk.Label(frame, textvariable=self.query_error_var, style="Danger.TLabel").grid(row=3, column=0, sticky="w", pady=(6, 0))

    def _build_guided_builder(self, parent: ttk.Frame) -> None:
        frame = ttk.LabelFrame(parent, text="Guided Query Builder", padding=14)
        frame.grid(row=1, column=0, sticky="nsew", pady=(12, 0))
        frame.columnconfigure(1, weight=1)
        parent.rowconfigure(1, weight=1)

        ttk.Label(
            frame,
            text="Terms within one box are joined with OR. Different boxes are joined with AND. Exclusions are joined with NOT. Year range limits publication years.",
            style="Muted.TLabel",
            wraplength=720,
        ).grid(row=0, column=0, columnspan=4, sticky="ew", pady=(0, 10))

        ttk.Label(frame, text="Starter template", style="Panel.TLabel").grid(row=1, column=0, sticky="w")
        self.preset_var = tk.StringVar(value="Blank custom query")
        ttk.Combobox(frame, textvariable=self.preset_var, values=tuple(PRESETS), state="readonly", width=34).grid(row=1, column=1, sticky="w", padx=(8, 8))
        ttk.Button(frame, text="Load Template", command=self.load_preset).grid(row=1, column=2, sticky="w")

        row = 2
        for key, label, example in BLOCK_DEFINITIONS:
            ttk.Label(frame, text=label, style="Panel.TLabel").grid(row=row, column=0, sticky="nw", pady=(12, 2))
            term_var = tk.StringVar(value="")
            field_var = tk.StringVar(value="Topic" if key != "exclusion" else "All Fields")
            self.block_terms[key] = term_var
            self.block_fields[key] = field_var
            entry = ttk.Entry(frame, textvariable=term_var)
            entry.grid(row=row, column=1, sticky="ew", padx=(8, 8), pady=(12, 2))
            entry.bind("<KeyRelease>", lambda _event: self._update_run_state())
            ttk.Combobox(frame, textvariable=field_var, values=FIELD_TARGETS, state="readonly", width=18).grid(row=row, column=2, sticky="w", pady=(12, 2))
            buttons = ttk.Frame(frame, style="Panel.TFrame")
            buttons.grid(row=row, column=3, sticky="w", padx=(8, 0), pady=(12, 2))
            ttk.Button(buttons, text="Add", command=lambda block_key=key: self.add_term(block_key)).pack(side="left")
            ttk.Button(buttons, text="Clear", command=lambda block_key=key: self.clear_block(block_key)).pack(side="left", padx=(4, 0))
            ttk.Label(frame, text=f"Examples: {example}", style="Muted.TLabel").grid(row=row + 1, column=1, columnspan=3, sticky="w", padx=(8, 0))
            row += 2

        year_frame = ttk.Frame(frame, style="Panel.TFrame")
        year_frame.grid(row=row, column=0, columnspan=4, sticky="ew", pady=(12, 0))
        ttk.Label(year_frame, text="Publication year range", style="Panel.TLabel").pack(side="left")
        self.start_year_var = tk.StringVar(value=str(self.settings.default_start_year or ""))
        self.end_year_var = tk.StringVar(value=str(self.settings.default_end_year or ""))
        ttk.Entry(year_frame, textvariable=self.start_year_var, width=8).pack(side="left", padx=(12, 4))
        ttk.Label(year_frame, text="to", style="Panel.TLabel").pack(side="left")
        ttk.Entry(year_frame, textvariable=self.end_year_var, width=8).pack(side="left", padx=(4, 12))
        self.start_year_var.trace_add("write", lambda *_args: self._update_run_state())
        self.end_year_var.trace_add("write", lambda *_args: self._update_run_state())

        preview_actions = ttk.Frame(frame, style="Panel.TFrame")
        preview_actions.grid(row=row + 1, column=0, columnspan=4, sticky="ew", pady=(12, 6))
        ttk.Button(preview_actions, text="Generate Query Preview", command=self.generate_query_preview).pack(side="left")
        ttk.Button(preview_actions, text="Copy Query", command=self.copy_generated_query).pack(side="left", padx=(8, 0))
        ttk.Button(preview_actions, text="Use This Query", command=self.use_generated_query).pack(side="left", padx=(8, 0))

        self.preview_text = tk.Text(frame, height=10, wrap="word", state="disabled", font=("Menlo", 11), relief="solid", bd=1)
        self.preview_text.grid(row=row + 2, column=0, columnspan=4, sticky="nsew")
        frame.rowconfigure(row + 2, weight=1)
        self.generated_query = ""

    def _build_source_panel(self, parent: ttk.Frame) -> None:
        frame = ttk.LabelFrame(parent, text="Sources", padding=14)
        frame.grid(row=0, column=0, sticky="ew")
        self.source_vars: dict[str, tk.BooleanVar] = {}
        for index, (source, enabled) in enumerate(self.settings.default_sources.items()):
            var = tk.BooleanVar(value=enabled)
            self.source_vars[source] = var
            ttk.Checkbutton(frame, text=SOURCE_LABELS.get(source, source), variable=var, command=self._update_run_state).grid(row=index // 2, column=index % 2, sticky="w", padx=(0, 20), pady=4)

    def _build_ris_panel(self, parent: ttk.Frame) -> None:
        frame = ttk.LabelFrame(parent, text="RIS files", padding=14)
        frame.grid(row=1, column=0, sticky="nsew", pady=(12, 0))
        parent.rowconfigure(1, weight=1)
        frame.columnconfigure(0, weight=1)
        self.include_ris = tk.BooleanVar(value=True)
        ttk.Checkbutton(frame, text="Include selected RIS files", variable=self.include_ris, command=self._update_run_state).grid(row=0, column=0, sticky="w")
        ttk.Button(frame, text="Browse RIS Folder", command=self.browse_ris_folder).grid(row=0, column=1, sticky="e")
        self.ris_folder_var = tk.StringVar(value=str(self.settings.resolved_ris_input_folder()))
        ttk.Label(frame, textvariable=self.ris_folder_var, style="Muted.TLabel", wraplength=360).grid(row=1, column=0, columnspan=2, sticky="ew", pady=(4, 8))
        self.ris_frame = ttk.Frame(frame, style="Panel.TFrame")
        self.ris_frame.grid(row=2, column=0, columnspan=2, sticky="nsew")
        frame.rowconfigure(2, weight=1)
        ttk.Button(frame, text="Refresh RIS", command=self.refresh_ris_files).grid(row=3, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        self.ris_warning_var = tk.StringVar(value="")
        ttk.Label(frame, textvariable=self.ris_warning_var, style="Danger.TLabel", wraplength=360).grid(row=4, column=0, columnspan=2, sticky="ew", pady=(6, 0))
        self.refresh_ris_files()

    def _build_options_panel(self, parent: ttk.Frame) -> None:
        frame = ttk.LabelFrame(parent, text="Pipeline options", padding=14)
        frame.grid(row=2, column=0, sticky="ew", pady=(12, 0))
        self.dry_run = tk.BooleanVar(value=False)
        self.ris_only = tk.BooleanVar(value=False)
        self.safe_local_test = tk.BooleanVar(value=False)
        self.ris_as_covidence = tk.BooleanVar(value=False)
        ttk.Checkbutton(frame, text="Dry Run", variable=self.dry_run, command=self._update_run_state).grid(row=0, column=0, sticky="w", pady=3)
        ttk.Checkbutton(frame, text="RIS-only mode", variable=self.ris_only, command=self._apply_ris_only_mode).grid(row=0, column=1, sticky="w", padx=(18, 0), pady=3)
        ttk.Checkbutton(frame, text="Safe Local Test", variable=self.safe_local_test, command=self._apply_safe_local_test).grid(row=1, column=0, sticky="w", pady=3)
        ttk.Checkbutton(frame, text="Treat RIS as Covidence", variable=self.ris_as_covidence).grid(row=1, column=1, sticky="w", padx=(18, 0), pady=3)

        self.live_warning_var = tk.StringVar(value="")
        ttk.Label(frame, textvariable=self.live_warning_var, style="Muted.TLabel", wraplength=380).grid(row=2, column=0, columnspan=2, sticky="ew", pady=(8, 0))

        self.show_advanced = tk.BooleanVar(value=False)
        ttk.Checkbutton(frame, text="Show advanced options", variable=self.show_advanced, command=self._toggle_advanced).grid(row=3, column=0, columnspan=2, sticky="w", pady=(10, 0))
        self.advanced_frame = ttk.Frame(frame, style="Panel.TFrame")
        self.advanced_frame.grid(row=4, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        self.scaling_mode = tk.StringVar(value="medium")
        self.concept_profile = tk.StringVar(value="")
        self.qa_only = tk.BooleanVar(value=False)
        self.enforce_concept_blocks = tk.BooleanVar(value=False)
        self.extract_geography = tk.BooleanVar(value=False)
        self.extract_demographics = tk.BooleanVar(value=False)
        self.rebuild_geo_cache = tk.BooleanVar(value=False)
        self.rebuild_demographic_cache = tk.BooleanVar(value=False)
        self.skip_rxnorm = tk.BooleanVar(value=True)
        ttk.Label(self.advanced_frame, text="Scaling", style="Panel.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Combobox(self.advanced_frame, textvariable=self.scaling_mode, values=SCALING_MODES, state="readonly", width=10).grid(row=0, column=1, sticky="w", padx=(8, 0))
        ttk.Label(self.advanced_frame, text="Concept profile", style="Panel.TLabel").grid(row=1, column=0, sticky="w", pady=(6, 0))
        ttk.Combobox(self.advanced_frame, textvariable=self.concept_profile, values=("", *CONCEPT_PROFILES), state="readonly", width=24).grid(row=1, column=1, sticky="w", padx=(8, 0), pady=(6, 0))
        ttk.Checkbutton(self.advanced_frame, text="QA only", variable=self.qa_only).grid(row=2, column=0, sticky="w", pady=(6, 0))
        ttk.Checkbutton(self.advanced_frame, text="Enforce concept blocks", variable=self.enforce_concept_blocks).grid(row=2, column=1, sticky="w", pady=(6, 0))
        ttk.Checkbutton(self.advanced_frame, text="Extract geography terms", variable=self.extract_geography).grid(row=3, column=0, sticky="w")
        ttk.Checkbutton(self.advanced_frame, text="Extract demographic terms", variable=self.extract_demographics).grid(row=3, column=1, sticky="w")
        ttk.Checkbutton(self.advanced_frame, text="Rebuild geography cache", variable=self.rebuild_geo_cache).grid(row=4, column=0, sticky="w")
        ttk.Checkbutton(self.advanced_frame, text="Rebuild demographic cache", variable=self.rebuild_demographic_cache).grid(row=4, column=1, sticky="w")
        ttk.Checkbutton(self.advanced_frame, text="Skip live RxNorm during visualization", variable=self.skip_rxnorm).grid(row=5, column=0, columnspan=2, sticky="w")
        self._toggle_advanced()

    def _build_action_panel(self, parent: ttk.Frame) -> None:
        frame = ttk.LabelFrame(parent, text="Run", padding=14)
        frame.grid(row=3, column=0, sticky="ew", pady=(12, 0))
        frame.columnconfigure(0, weight=1)
        self.run_button = ttk.Button(frame, text="Run Pipeline", style="Primary.TButton", command=self.start_pipeline)
        self.run_button.grid(row=0, column=0, sticky="ew")
        self.run_help_var = tk.StringVar(value="")
        ttk.Label(frame, textvariable=self.run_help_var, style="Danger.TLabel", wraplength=380).grid(row=1, column=0, sticky="ew", pady=(8, 0))

        self.secondary_actions = ttk.Frame(frame, style="Panel.TFrame")
        self.secondary_actions.grid(row=2, column=0, sticky="ew", pady=(12, 0))
        ttk.Button(self.secondary_actions, text="Generate visuals", style="Secondary.TButton", command=self.start_visualizations).pack(fill="x", pady=2)
        ttk.Button(self.secondary_actions, text="Generate VOS networks", style="Secondary.TButton", command=self.start_vos_networks).pack(fill="x", pady=2)
        ttk.Button(self.secondary_actions, text="Convert CSV to VOS TXT", style="Secondary.TButton", command=self.start_vos_validator).pack(fill="x", pady=2)
        self.status_var = tk.StringVar(value="Ready")
        ttk.Label(frame, textvariable=self.status_var, style="Muted.TLabel").grid(row=3, column=0, sticky="w", pady=(10, 0))
        self._toggle_advanced()
        self._update_run_state()

    def _build_outputs_tab(self) -> None:
        self.outputs_tab.columnconfigure(0, weight=1)
        self.outputs_tab.columnconfigure(1, weight=1)
        self.outputs_tab.rowconfigure(0, weight=0)
        self.outputs_tab.rowconfigure(1, weight=1)
        tk.Label(
            self.outputs_tab,
            text="OUTPUTS TAB PAINT TEST",
            bg="cyan",
            fg="black",
            font=("Arial", 20, "bold"),
        ).grid(row=0, column=0, columnspan=2, sticky="ew")
        log_box = ttk.LabelFrame(self.outputs_tab, text="Live log", padding=14)
        log_box.grid(row=1, column=0, sticky="nsew", padx=(0, 10), pady=(8, 0))
        log_box.rowconfigure(0, weight=1)
        log_box.columnconfigure(0, weight=1)
        self.log_text = tk.Text(log_box, wrap="word", state="normal")
        self.log_text.grid(row=0, column=0, sticky="nsew")
        log_scroll = ttk.Scrollbar(log_box, command=self.log_text.yview)
        log_scroll.grid(row=0, column=1, sticky="ns")
        self.log_text.configure(yscrollcommand=log_scroll.set)

        outputs_box = ttk.LabelFrame(self.outputs_tab, text="Real outputs", padding=14)
        outputs_box.grid(row=1, column=1, sticky="nsew", pady=(8, 0))
        outputs_box.rowconfigure(1, weight=1)
        outputs_box.columnconfigure(0, weight=1)
        self.metrics_var = tk.StringVar(value="No run yet")
        ttk.Label(outputs_box, textvariable=self.metrics_var, style="Panel.TLabel", wraplength=460).grid(row=0, column=0, columnspan=2, sticky="ew")
        self.files_list = tk.Listbox(outputs_box, height=16)
        self.files_list.grid(row=1, column=0, sticky="nsew", pady=(10, 8))
        self.files_list.bind("<Double-Button-1>", self.open_selected_file)
        file_scroll = ttk.Scrollbar(outputs_box, command=self.files_list.yview)
        file_scroll.grid(row=1, column=1, sticky="ns", pady=(10, 8))
        self.files_list.configure(yscrollcommand=file_scroll.set)
        ttk.Button(outputs_box, text="Open selected file", command=self.open_selected_file).grid(row=2, column=0, columnspan=2, sticky="ew", pady=2)
        ttk.Button(outputs_box, text="Open output folder", command=lambda: self.open_path(self.runtime_paths.outputs_dir)).grid(row=3, column=0, columnspan=2, sticky="ew", pady=2)
        ttk.Button(outputs_box, text="Open logs folder", command=lambda: self.open_path(self.runtime_paths.logs_dir)).grid(row=4, column=0, columnspan=2, sticky="ew", pady=2)

    def load_preset(self) -> None:
        preset = PRESETS.get(self.preset_var.get(), PRESETS["Blank custom query"])
        for key in self.block_terms:
            self.block_terms[key].set(str(preset.get(key, "")))
        self.start_year_var.set(str(preset.get("start_year", "")))
        self.end_year_var.set(str(preset.get("end_year", "")))
        self.generate_query_preview()

    def clear_block(self, key: str) -> None:
        self.block_terms[key].set("")
        self._update_run_state()

    def add_term(self, key: str) -> None:
        term = simpledialog.askstring("Add term", "Enter one term to add:", parent=self)
        if not term:
            return
        existing = parse_terms(self.block_terms[key].get())
        updated = [*existing, *parse_terms(term)]
        self.block_terms[key].set(", ".join(dict.fromkeys(updated)))
        self._update_run_state()

    def generate_query_preview(self) -> None:
        result = self._build_guided_query()
        self.generated_query = result.machine_query if result.has_query else ""
        preview = result.preview or "\n".join(result.errors)
        if result.errors:
            preview = f"{preview}\n\nValidation:\n" + "\n".join(f"- {error}" for error in result.errors)
        self.preview_text.configure(state="normal")
        self.preview_text.delete("1.0", "end")
        self.preview_text.insert("1.0", preview)
        self.preview_text.configure(state="disabled")
        self._update_run_state()

    def copy_generated_query(self) -> None:
        if not self.generated_query:
            self.generate_query_preview()
        if self.generated_query:
            self.clipboard_clear()
            self.clipboard_append(self.generated_query)
            self.status_var.set("Generated query copied")

    def use_generated_query(self) -> None:
        if not self.generated_query:
            self.generate_query_preview()
        if not self.generated_query:
            return
        self.query_text.delete("1.0", "end")
        self.query_text.insert("1.0", self.generated_query)
        self.query_text.configure(fg="#111827")
        self.query_placeholder_active = False
        self.query_error_var.set("")
        self._update_run_state()

    def _build_guided_query(self):
        start_year, end_year, year_errors = self._read_years()
        blocks = [
            ConceptBlock(key=key, terms=parse_terms(variable.get()), field_target=self.block_fields[key].get())
            for key, variable in self.block_terms.items()
        ]
        result = build_wos_numbered_query(blocks, start_year=start_year, end_year=end_year)
        if year_errors:
            return type(result)(result.preview, result.machine_query, False, [*result.errors, *year_errors])
        return result

    def refresh_ris_files(self) -> None:
        for child in self.ris_frame.winfo_children():
            child.destroy()
        self.ris_vars = {}
        ris_files = list_ris_files(self.settings)
        if not ris_files:
            ttk.Label(self.ris_frame, text="No RIS files found.", style="Muted.TLabel").grid(row=0, column=0, sticky="w")
            self._update_run_state()
            return
        for index, path in enumerate(ris_files):
            var = tk.BooleanVar(value=True)
            self.ris_vars[path] = var
            ttk.Checkbutton(self.ris_frame, text=path.name, variable=var, command=self._update_run_state).grid(row=index, column=0, sticky="w", pady=2)
        self._update_run_state()

    def start_pipeline(self) -> None:
        self.generate_query_preview()
        query = self._query_value() or self.generated_query
        if not query:
            self.query_error_var.set("Enter a research question or generate a guided query before running.")
            self._update_run_state()
            return

        sources = [source for source, var in self.source_vars.items() if var.get()]
        include_ris = self.include_ris.get()
        selected_ris = [path for path, var in self.ris_vars.items() if var.get()]
        if include_ris and not selected_ris:
            self.ris_warning_var.set("RIS is enabled, but no RIS file is selected.")
            return
        if not sources and not include_ris:
            self.run_help_var.set("Select at least one source or include RIS files.")
            return

        start_year, end_year, year_errors = self._read_years()
        if year_errors:
            self.query_error_var.set(" ".join(year_errors))
            return

        live_sources = [source for source in sources if source in LIVE_API_SOURCES]
        if live_sources and not self.dry_run.get() and not self.safe_local_test.get():
            proceed = messagebox.askyesno(
                "Confirm live API run",
                "This may take several minutes and requires network/API access. Continue?",
            )
            if not proceed:
                return

        log_path = create_run_log(self.settings, "pipeline")
        self.settings.last_run_log = str(log_path)
        save_config(self.settings)
        request = PipelineRequest(
            query=query,
            sources=sources,
            filters=self._filters(),
            ris_files=selected_ris,
            include_ris=include_ris,
            ris_as_covidence=self.ris_as_covidence.get(),
            scaling_mode=self.scaling_mode.get(),
            slug=self.slug_from_query(query),
            start_year=start_year,
            end_year=end_year,
            concept_profile=self.concept_profile.get().strip(),
            qa_only=self.qa_only.get(),
            enforce_concept_blocks=self.enforce_concept_blocks.get(),
            rebuild_geo_cache=self.rebuild_geo_cache.get(),
            rebuild_demographic_cache=self.rebuild_demographic_cache.get(),
            extract_geography=self.extract_geography.get(),
            extract_demographics=self.extract_demographics.get(),
            dry_run=self.dry_run.get(),
            safe_local_test=self.safe_local_test.get(),
            log_path=log_path,
        )
        self.output_files = []
        self.output_paths = {}
        self.core_dataset = None
        self.files_list.delete(0, "end")
        self.notebook.select(self.outputs_tab)
        self._start_worker("pipeline", lambda: run_pipeline(request, self.settings, log_callback=self._emit_log), "Running pipeline...")

    def start_visualizations(self) -> None:
        query = self._query_value()
        self.notebook.select(self.outputs_tab)
        self._start_worker("files", lambda: run_visualizations(self.core_dataset, query, self.skip_rxnorm.get(), self.settings, log_callback=self._emit_log), "Generating visuals...")

    def start_vos_networks(self) -> None:
        self.notebook.select(self.outputs_tab)
        self._start_worker("files", lambda: run_vos_networks(self.core_dataset, self.settings, log_callback=self._emit_log), "Generating VOS networks...")

    def start_vos_validator(self) -> None:
        csv_path = self.core_dataset or self.output_paths.get("main_results")
        if not csv_path:
            csv_files = sorted(self.runtime_paths.outputs_dir.glob("*.csv"), key=lambda path: path.stat().st_mtime, reverse=True)
            csv_path = str(csv_files[0]) if csv_files else None
        if not csv_path:
            messagebox.showerror("Missing CSV", "Run the pipeline first or place a CSV in the configured output folder.")
            return
        self.notebook.select(self.outputs_tab)
        self._start_worker("files", lambda: run_vos_validator(str(csv_path), self.settings, log_callback=self._emit_log), "Converting VOS TXT...")

    def process_messages(self) -> None:
        try:
            while True:
                message_type, payload = self.log_queue.get_nowait()
                if message_type == "log":
                    self._append_log(str(payload))
                elif message_type == "result":
                    self._handle_result(payload)  # type: ignore[arg-type]
                elif message_type == "error":
                    self._handle_error(payload)
        except queue.Empty:
            pass
        self.after(LOG_POLL_INTERVAL_MS, self.process_messages)

    def _start_worker(self, result_type: str, work: Callable[[], dict[str, object]], status: str) -> None:
        if self.worker_thread and self.worker_thread.is_alive():
            return
        self.log_text.delete("1.0", "end")
        self._set_running(True, status)
        self._emit_log(status)
        self.worker_thread = threading.Thread(target=self._run_worker, args=(result_type, work), daemon=True)
        self.worker_thread.start()

    def _run_worker(self, result_type: str, work: Callable[[], dict[str, object]]) -> None:
        try:
            self.log_queue.put(("result", {"type": result_type, "data": work()}))
        except Exception as exc:
            formatted = traceback.format_exc()
            if self.settings.last_run_log:
                try:
                    Path(self.settings.last_run_log).parent.mkdir(parents=True, exist_ok=True)
                    with Path(self.settings.last_run_log).open("a", encoding="utf-8", errors="replace") as handle:
                        handle.write("\nUnhandled GUI exception:\n")
                        handle.write(formatted)
                except OSError:
                    pass
            self.log_queue.put(("error", {"friendly": friendly_error_message(exc), "technical": formatted}))

    def _handle_result(self, payload: dict[str, object]) -> None:
        result_type = payload.get("type")
        data = payload.get("data", {})
        result = data if isinstance(data, dict) else {}
        if result_type == "pipeline":
            self.output_paths = result.get("output_paths", {}) if isinstance(result.get("output_paths"), dict) else {}
            self.core_dataset = result.get("core_dataset") if isinstance(result.get("core_dataset"), str) else None
            summary = result.get("summary", {}) if isinstance(result.get("summary"), dict) else {}
            row_text = summary.get("rows", result.get("total_records", 0))
            self.metrics_var.set(
                f"Records: {row_text} | Citations: {result.get('total_citations', 0)} | "
                f"H-index: {result.get('h_index', 0)} | Duplicates removed: {result.get('duplicates_removed', 0)} | "
                f"QA failures: {result.get('qa_failures', 0)}"
            )
            self._set_files([str(path) for path in result.get("output_files", [])])
            log_file = self.output_paths.get("log_file")
            if isinstance(log_file, str):
                self.settings.last_run_log = log_file
                save_config(self.settings)
            self._append_log("Pipeline complete")
            status = "Dry run complete" if result.get("dry_run") else "Pipeline complete"
            self._set_running(False, status)
        else:
            self._set_files([str(path) for path in result.get("output_files", [])] + self.output_files)
            self._append_log("Post-processing complete")
            self._set_running(False, "Post-processing complete")

    def _handle_error(self, payload: object) -> None:
        friendly = str(payload.get("friendly")) if isinstance(payload, dict) else friendly_error_message(RuntimeError(str(payload)))
        self._append_log(friendly)
        self._append_log("Technical details were saved in the run log.")
        self._set_running(False, "Failed")
        messagebox.showerror("DansBib error", friendly)

    def _set_running(self, running: bool, status: str) -> None:
        state = "disabled" if running else "normal"
        self.run_button.configure(state=state if running else ("normal" if self._has_runnable_query() else "disabled"))
        for child in self.secondary_actions.winfo_children():
            child.configure(state=state)
        self.status_var.set(status)

    def _update_run_state(self) -> None:
        if not hasattr(self, "run_button"):
            return
        has_query = self._has_runnable_query()
        self.run_button.configure(state="normal" if has_query else "disabled")
        self.run_help_var.set("" if has_query else "Enter a research question or add enough guided query terms.")
        if self._query_value():
            self.query_error_var.set("")
        selected_sources = [source for source, var in self.source_vars.items() if var.get()]
        live_sources = [source for source in selected_sources if source in LIVE_API_SOURCES]
        self.live_warning_var.set(
            "Live API sources selected. A real run may take several minutes and requires network/API access."
            if live_sources and not self.dry_run.get()
            else ""
        )
        selected_ris = [path for path, var in self.ris_vars.items() if var.get()]
        self.ris_warning_var.set("RIS is enabled, but no RIS file is selected." if self.include_ris.get() and not selected_ris else "")

    def _has_runnable_query(self) -> bool:
        if self._query_value():
            return True
        result = self._build_guided_query()
        return result.has_query

    def _query_value(self) -> str:
        if self.query_placeholder_active:
            return ""
        return self.query_text.get("1.0", "end").strip()

    def _clear_query_placeholder(self, _event: tk.Event | None = None) -> None:
        if not self.query_placeholder_active:
            return
        self.query_text.delete("1.0", "end")
        self.query_text.configure(fg="#111827")
        self.query_placeholder_active = False

    def _restore_query_placeholder(self, _event: tk.Event | None = None) -> None:
        if self.query_text.get("1.0", "end").strip():
            return
        self.query_text.insert("1.0", self.query_placeholder)
        self.query_text.configure(fg="#6b7280")
        self.query_placeholder_active = True
        self._update_run_state()

    def _read_years(self) -> tuple[int | None, int | None, list[str]]:
        errors: list[str] = []

        def parse_year(value: str, label: str) -> int | None:
            text = value.strip()
            if not text:
                return None
            try:
                return int(text)
            except ValueError:
                errors.append(f"{label} must be a number.")
                return None

        start_year = parse_year(self.start_year_var.get(), "Start year")
        end_year = parse_year(self.end_year_var.get(), "End year")
        if start_year is not None and end_year is not None and start_year > end_year:
            errors.append("Start year must be earlier than or equal to end year.")
        return start_year, end_year, errors

    def _filters(self) -> dict[str, bool | None]:
        return {"review": None, "early_access": None, "open_access": None}

    def _emit_log(self, message: str) -> None:
        self.log_queue.put(("log", message))

    def _append_log(self, message: str) -> None:
        self.log_text.insert("end", f"{message}\n")
        self.log_text.see("end")

    def _set_files(self, files: list[str]) -> None:
        self.output_files = list(dict.fromkeys(file for file in files if file))
        self.files_list.delete(0, "end")
        for file in self.output_files:
            self.files_list.insert("end", self._display_path(file))

    def _display_path(self, file: str) -> str:
        path = Path(file)
        try:
            return str(path.relative_to(self.runtime_paths.root))
        except ValueError:
            return path.name

    def open_selected_file(self, _event: tk.Event | None = None) -> None:
        selection = self.files_list.curselection()
        if not selection:
            messagebox.showerror("Missing file", "Select an output file first.")
            return
        self.open_path(Path(self.output_files[selection[0]]))

    def open_path(self, path: Path | str) -> None:
        try:
            open_path(path)
        except Exception as exc:
            messagebox.showerror("Open failed", str(exc))

    def browse_ris_folder(self) -> None:
        selected = filedialog.askdirectory(initialdir=str(self.settings.resolved_ris_input_folder()))
        if not selected:
            return
        self.settings.ris_input_folder = selected
        self.ris_folder_var.set(selected)
        self.runtime_paths = paths_from_config(self.settings)
        save_config(self.settings)
        self.refresh_ris_files()

    def show_setup_dialog(self) -> None:
        dialog = tk.Toplevel(self)
        dialog.title("DansBib GUI Setup")
        dialog.transient(self)
        dialog.grab_set()
        dialog.configure(bg="#eef2f7", padx=14, pady=14)

        dansbib_var = tk.StringVar(value=str(self.settings.resolved_dansbib_path()))
        output_var = tk.StringVar(value=str(self.settings.resolved_output_folder()))
        ris_var = tk.StringVar(value=str(self.settings.resolved_ris_input_folder()))
        logs_var = tk.StringVar(value=str(self.settings.resolved_logs_folder()))

        def choose_folder(variable: tk.StringVar) -> None:
            selected = filedialog.askdirectory(initialdir=variable.get() or str(Path.home()))
            if selected:
                variable.set(selected)

        for row, (label, variable) in enumerate(
            (("DansBib folder", dansbib_var), ("Output folder", output_var), ("RIS input folder", ris_var), ("Logs folder", logs_var))
        ):
            ttk.Label(dialog, text=label).grid(row=row, column=0, sticky="w", pady=4)
            ttk.Entry(dialog, textvariable=variable, width=72).grid(row=row, column=1, sticky="ew", padx=8, pady=4)
            ttk.Button(dialog, text="Browse", command=lambda var=variable: choose_folder(var)).grid(row=row, column=2, pady=4)

        status_var = tk.StringVar(value="")
        ttk.Label(dialog, textvariable=status_var, wraplength=620).grid(row=4, column=0, columnspan=3, sticky="ew", pady=(10, 4))

        def refresh_status() -> None:
            root = Path(dansbib_var.get()).expanduser()
            status_var.set(
                f"main.py found: {'yes' if (root / 'main.py').exists() else 'no'}\n"
                f"Python/pipeline availability: {'ready enough to try' if (root / 'main.py').exists() else 'select a DansBib folder that contains main.py'}"
            )

        def save_and_close() -> None:
            self.settings.dansbib_path = dansbib_var.get().strip()
            self.settings.output_folder = output_var.get().strip()
            self.settings.ris_input_folder = ris_var.get().strip()
            self.settings.logs_folder = logs_var.get().strip()
            self.settings.default_sources = {source: var.get() for source, var in self.source_vars.items()}
            start_year, end_year, _errors = self._read_years()
            self.settings.default_start_year = start_year
            self.settings.default_end_year = end_year
            save_config(self.settings)
            self.runtime_paths = paths_from_config(self.settings)
            self.ris_folder_var.set(str(self.settings.resolved_ris_input_folder()))
            self.refresh_ris_files()
            dialog.destroy()

        refresh_status()
        ttk.Button(dialog, text="Check", command=refresh_status).grid(row=5, column=0, sticky="w", pady=(10, 0))
        ttk.Button(dialog, text="Save Settings", command=save_and_close).grid(row=5, column=2, sticky="e", pady=(10, 0))
        dialog.columnconfigure(1, weight=1)

    def show_diagnostics(self) -> None:
        report = collect_diagnostics(self.settings).as_text()
        dialog = tk.Toplevel(self)
        dialog.title("About / Diagnostics")
        dialog.geometry("760x560")
        dialog.configure(bg="#eef2f7", padx=12, pady=12)
        text = tk.Text(dialog, wrap="word")
        text.insert("1.0", report)
        text.configure(state="disabled")
        text.pack(fill="both", expand=True)
        buttons = ttk.Frame(dialog)
        buttons.pack(fill="x", pady=(8, 0))
        ttk.Button(buttons, text="Copy diagnostic report", command=lambda: self._copy_text(report)).pack(side="left")
        ttk.Button(buttons, text="Open logs folder", command=lambda: self.open_path(self.runtime_paths.logs_dir)).pack(side="left", padx=(8, 0))

    def _copy_text(self, text: str) -> None:
        self.clipboard_clear()
        self.clipboard_append(text)
        self.status_var.set("Copied")

    def _toggle_advanced(self) -> None:
        if self.show_advanced.get():
            self.advanced_frame.grid()
            if hasattr(self, "secondary_actions"):
                self.secondary_actions.grid()
        else:
            self.advanced_frame.grid_remove()
            if hasattr(self, "secondary_actions"):
                self.secondary_actions.grid_remove()

    def _apply_ris_only_mode(self) -> None:
        if self.ris_only.get():
            self.include_ris.set(True)
            self.ris_as_covidence.set(True)
            for source, variable in self.source_vars.items():
                variable.set(source == "covidence")
        self._update_run_state()

    def _apply_safe_local_test(self) -> None:
        if self.safe_local_test.get():
            self.dry_run.set(False)
            self.ris_only.set(True)
            self._apply_ris_only_mode()
        self._update_run_state()

    def slug_from_query(self, query: str) -> str:
        words = [part.strip("()\"=,;:#").lower() for part in query.split()[:6]]
        return "_".join(part for part in words if part.isalnum())[:80]

    def _force_initial_paint(self) -> None:
        self.deiconify()
        self.lift()
        self.update_idletasks()


def main() -> None:
    app = DansBibApp()
    app.update_idletasks()
    app.mainloop()


if __name__ == "__main__":
    main()
