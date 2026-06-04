from __future__ import annotations

import tkinter as tk
from pathlib import Path
from tkinter import ttk

from ..config import DEFAULT_SOURCES, SOURCE_LABELS
from ..utils.app_config import load_config
from ..utils.pipeline_runner import SCALING_MODES, list_ris_files


class QueryPanel(ttk.LabelFrame):
    def __init__(self, master: tk.Misc) -> None:
        super().__init__(master, text="Pipeline Inputs", padding=12)

        self.query_text = tk.Text(self, height=4, wrap="word")
        self.query_text.grid(row=1, column=0, columnspan=4, sticky="nsew", pady=(4, 10))
        ttk.Label(self, text="Search query").grid(row=0, column=0, sticky="w")

        self.source_vars: dict[str, tk.BooleanVar] = {}
        sources_frame = ttk.LabelFrame(self, text="Sources", padding=8)
        sources_frame.grid(row=2, column=0, columnspan=2, sticky="nsew", padx=(0, 8), pady=(0, 8))
        for index, (source, enabled) in enumerate(DEFAULT_SOURCES.items()):
            variable = tk.BooleanVar(value=enabled)
            self.source_vars[source] = variable
            ttk.Checkbutton(
                sources_frame,
                text=SOURCE_LABELS.get(source, source),
                variable=variable,
            ).grid(row=index // 2, column=index % 2, sticky="w", padx=(0, 12), pady=2)

        filters_frame = ttk.LabelFrame(self, text="Filters", padding=8)
        filters_frame.grid(row=2, column=2, columnspan=2, sticky="nsew", pady=(0, 8))
        self.review_filter = tk.StringVar(value="any")
        self.early_filter = tk.StringVar(value="any")
        self.oa_filter = tk.StringVar(value="any")
        self._add_filter_row(filters_frame, 0, "Review articles", self.review_filter)
        self._add_filter_row(filters_frame, 1, "Early access", self.early_filter)
        self._add_filter_row(filters_frame, 2, "Open access", self.oa_filter)

        options_frame = ttk.LabelFrame(self, text="Run options", padding=8)
        options_frame.grid(row=3, column=0, columnspan=4, sticky="nsew")
        ttk.Label(options_frame, text="Scaling mode").grid(row=0, column=0, sticky="w")
        self.scaling_mode = tk.StringVar(value="medium")
        ttk.Combobox(
            options_frame,
            textvariable=self.scaling_mode,
            values=SCALING_MODES,
            state="readonly",
            width=10,
        ).grid(row=0, column=1, sticky="w", padx=(8, 20))

        self.include_ris = tk.BooleanVar(value=True)
        ttk.Checkbutton(options_frame, text="Include selected RIS files", variable=self.include_ris).grid(row=0, column=2, sticky="w")
        self.ris_as_covidence = tk.BooleanVar(value=False)
        ttk.Checkbutton(options_frame, text="Treat RIS as Covidence", variable=self.ris_as_covidence).grid(row=0, column=3, sticky="w", padx=(12, 0))

        ttk.Label(options_frame, text="RIS files from DansBib/data/raw").grid(row=1, column=0, columnspan=4, sticky="w", pady=(10, 4))
        self.ris_vars: dict[Path, tk.BooleanVar] = {}
        self.ris_list = tk.Frame(options_frame)
        self.ris_list.grid(row=2, column=0, columnspan=4, sticky="ew")
        self.refresh_ris_files()

        self.skip_rxnorm = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            options_frame,
            text="Skip live RxNorm during visualization",
            variable=self.skip_rxnorm,
        ).grid(row=3, column=0, columnspan=4, sticky="w", pady=(10, 0))

        self.columnconfigure(0, weight=1)
        self.columnconfigure(1, weight=1)
        self.columnconfigure(2, weight=1)
        self.columnconfigure(3, weight=1)
        self.rowconfigure(1, weight=1)

    def get_query_text(self) -> str:
        return self.query_text.get("1.0", "end").strip()

    def get_selected_sources(self) -> list[str]:
        return [source for source, variable in self.source_vars.items() if variable.get()]

    def get_filters(self) -> dict[str, bool | None]:
        return {
            "review": self._tri_state_value(self.review_filter.get()),
            "early_access": self._tri_state_value(self.early_filter.get()),
            "open_access": self._tri_state_value(self.oa_filter.get()),
        }

    def get_selected_ris_files(self) -> list[Path]:
        return [path for path, variable in self.ris_vars.items() if variable.get()]

    def refresh_ris_files(self) -> None:
        for child in self.ris_list.winfo_children():
            child.destroy()
        self.ris_vars = {}
        ris_files = list_ris_files(load_config())
        if not ris_files:
            ttk.Label(self.ris_list, text="No RIS files found.").grid(row=0, column=0, sticky="w")
            return
        for index, path in enumerate(ris_files):
            variable = tk.BooleanVar(value=True)
            self.ris_vars[path] = variable
            ttk.Checkbutton(self.ris_list, text=path.name, variable=variable).grid(row=index // 2, column=index % 2, sticky="w", padx=(0, 12), pady=2)

    def _add_filter_row(self, master: tk.Misc, row: int, label: str, variable: tk.StringVar) -> None:
        ttk.Label(master, text=label).grid(row=row, column=0, sticky="w", pady=2)
        ttk.Combobox(
            master,
            textvariable=variable,
            values=("any", "include", "exclude"),
            state="readonly",
            width=9,
        ).grid(row=row, column=1, sticky="w", padx=(8, 0), pady=2)

    def _tri_state_value(self, value: str) -> bool | None:
        if value == "include":
            return True
        if value == "exclude":
            return False
        return None
