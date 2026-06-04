from __future__ import annotations

import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk
from typing import Callable

from ..config import DANSBIB_OUTPUTS_DIR, DANSBIB_VISUALS_DIR, DANSBIB_VOS_DIR
from ..utils.file_utils import open_path as open_system_path


class ResultsPanel(ttk.LabelFrame):
    def __init__(self, master: tk.Misc, status_callback: Callable[[str], None]) -> None:
        super().__init__(master, text="Outputs", padding=12)
        self.status_callback = status_callback
        self.output_paths: dict[str, str] = {}
        self.output_files: list[str] = []

        self.total_var = tk.StringVar(value="0")
        self.citations_var = tk.StringVar(value="0")
        self.h_index_var = tk.StringVar(value="0")
        self.duplicates_var = tk.StringVar(value="0")

        self._metric("Total records", self.total_var, 0)
        self._metric("Total citations", self.citations_var, 1)
        self._metric("H-index", self.h_index_var, 2)
        self._metric("Duplicates removed", self.duplicates_var, 3)

        ttk.Label(self, text="Files created/updated").grid(row=4, column=0, columnspan=2, sticky="w", pady=(12, 4))
        self.files_list = tk.Listbox(self, height=12)
        scrollbar = ttk.Scrollbar(self, orient="vertical", command=self.files_list.yview)
        self.files_list.configure(yscrollcommand=scrollbar.set)
        self.files_list.grid(row=5, column=0, sticky="nsew")
        scrollbar.grid(row=5, column=1, sticky="ns")
        self.files_list.bind("<Double-Button-1>", self.open_selected_file)

        self.open_file_button = ttk.Button(self, text="Open selected file", command=self.open_selected_file)
        self.outputs_button = ttk.Button(self, text="Open data/outputs", command=lambda: self.open_path(DANSBIB_OUTPUTS_DIR))
        self.visuals_button = ttk.Button(self, text="Open data/visuals", command=lambda: self.open_path(DANSBIB_VISUALS_DIR))
        self.vos_button = ttk.Button(self, text="Open data/VOS", command=lambda: self.open_path(DANSBIB_VOS_DIR))

        self.open_file_button.grid(row=6, column=0, columnspan=2, sticky="ew", pady=(10, 4))
        self.outputs_button.grid(row=7, column=0, columnspan=2, sticky="ew", pady=4)
        self.visuals_button.grid(row=8, column=0, columnspan=2, sticky="ew", pady=4)
        self.vos_button.grid(row=9, column=0, columnspan=2, sticky="ew", pady=4)

        self.columnconfigure(0, weight=1)
        self.rowconfigure(5, weight=1)
        self._set_file_button_state()

    def update_results(self, result: dict[str, object]) -> None:
        self.total_var.set(str(result.get("total_records", 0)))
        self.citations_var.set(str(result.get("total_citations", 0)))
        self.h_index_var.set(str(result.get("h_index", 0)))
        self.duplicates_var.set(str(result.get("duplicates_removed", 0)))
        output_paths = result.get("output_paths", {})
        self.output_paths = output_paths if isinstance(output_paths, dict) else {}
        self.set_files([str(path) for path in result.get("output_files", [])])

    def append_files(self, files: list[str]) -> None:
        merged = list(dict.fromkeys([*files, *self.output_files]))
        self.set_files(merged)

    def set_files(self, files: list[str]) -> None:
        self.output_files = [file for file in files if file]
        self.files_list.delete(0, "end")
        for file in self.output_files:
            self.files_list.insert("end", self._display_path(file))
        self._set_file_button_state()

    def reset(self) -> None:
        self.total_var.set("0")
        self.citations_var.set("0")
        self.h_index_var.set("0")
        self.duplicates_var.set("0")
        self.output_paths = {}
        self.set_files([])

    def open_selected_file(self, _event: tk.Event | None = None) -> None:
        selection = self.files_list.curselection()
        if not selection:
            messagebox.showerror("Missing file", "Select an output file first.")
            return
        self.open_path(Path(self.output_files[selection[0]]))

    def open_path(self, path: Path | str) -> None:
        target = Path(path)
        if not target.exists():
            messagebox.showerror("Missing path", f"Path does not exist:\n{target}")
            return

        try:
            open_system_path(target)
            self.status_callback(f"Opened {target.name}")
        except Exception as exc:
            messagebox.showerror("Open failed", str(exc))

    def _metric(self, label: str, variable: tk.StringVar, row: int) -> None:
        ttk.Label(self, text=label).grid(row=row, column=0, sticky="w", pady=(0, 4))
        ttk.Label(self, textvariable=variable).grid(row=row, column=1, sticky="w", padx=(8, 0), pady=(0, 4))

    def _display_path(self, file: str) -> str:
        path = Path(file)
        try:
            return str(path.relative_to(DANSBIB_OUTPUTS_DIR.parent))
        except ValueError:
            return path.name

    def _set_file_button_state(self) -> None:
        self.open_file_button.configure(state="normal" if self.output_files else "disabled")
