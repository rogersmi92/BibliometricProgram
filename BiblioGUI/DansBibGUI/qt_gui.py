from __future__ import annotations

import traceback
from dataclasses import replace
from pathlib import Path
from typing import Callable

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QTabWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from DansBib.pipeline_capabilities import RIS_SOURCE_LABELS, SOURCE_DISPLAY_LABELS, RisInput, normalize_ris_source

from .utils.app_config import APP_VERSION, AppConfig, load_config, save_config
from .utils.diagnostics import collect_diagnostics, friendly_error_message
from .utils.file_utils import open_path
from .utils.pipeline_runner import (
    CONCEPT_PROFILES,
    LIVE_API_SOURCES,
    SCALING_MODES,
    PipelineRequest,
    list_ris_files,
    run_pipeline,
    run_visualizations,
    run_vos_networks,
    run_vos_validator,
)
from .utils.query_builder import (
    BLOCK_LABELS,
    FIELD_TARGETS,
    PRESETS,
    ConceptBlock,
    build_wos_numbered_query,
    parse_terms,
)

class TaskThread(QThread):
    log = Signal(str)
    done = Signal(object)
    failed = Signal(str, str)

    def __init__(self, task: Callable[[Callable[[str], None]], dict[str, object]], parent: QWidget | None = None):
        super().__init__(parent)
        self._task = task

    def run(self) -> None:
        try:
            self.done.emit(self._task(self.log.emit))
        except Exception as exc:  # pragma: no cover - exercised by GUI runtime
            self.failed.emit(friendly_error_message(exc), traceback.format_exc())


class DansBibQtWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.settings = load_config()
        self.last_result: dict[str, object] = {}
        self.active_thread: TaskThread | None = None
        self.concept_widgets: dict[str, tuple[QLineEdit, QComboBox]] = {}
        self.source_checks: dict[str, QCheckBox] = {}
        self.ris_checks: dict[Path, QCheckBox] = {}
        self.ris_source_combos: dict[Path, QComboBox] = {}
        self.manual_ris_files: list[Path] = []

        self.setWindowTitle("DansBib GUI")
        self.resize(1260, 860)
        self._build_ui()
        self._refresh_diagnostics()
        self._refresh_ris_files()
        self._validate_run_state()

    def _build_ui(self) -> None:
        self._apply_style()
        root = QWidget()
        root_layout = QVBoxLayout(root)
        root_layout.setContentsMargins(18, 16, 18, 18)
        root_layout.setSpacing(12)
        self.setCentralWidget(root)

        header = QFrame()
        header.setObjectName("Header")
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(16, 12, 16, 12)
        title = QLabel("DansBib Qt GUI Loaded")
        title.setObjectName("Title")
        title.setFont(QFont("Arial", 20, QFont.Weight.Bold))
        subtitle = QLabel("Staff workflow for query creation, RIS selection, pipeline runs, outputs, and diagnostics")
        subtitle.setObjectName("Subtitle")
        title_box = QVBoxLayout()
        title_box.addWidget(title)
        title_box.addWidget(subtitle)
        header_layout.addLayout(title_box, 1)
        self.status_label = QLabel("Ready")
        self.status_label.setObjectName("StatusPill")
        header_layout.addWidget(self.status_label, 0, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        root_layout.addWidget(header)

        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(False)
        root_layout.addWidget(self.tabs, 1)

        self.tabs.addTab(self._build_run_tab(), "Build & Run")
        self.tabs.addTab(self._build_outputs_tab(), "Outputs & Logs")
        self.tabs.addTab(self._build_setup_tab(), "Setup & Diagnostics")

    def _build_run_tab(self) -> QWidget:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(14, 14, 14, 18)
        layout.setSpacing(14)
        scroll.setWidget(page)

        query_box = QGroupBox("Search query")
        query_layout = QVBoxLayout(query_box)
        query_layout.setSpacing(8)
        query_layout.addWidget(QLabel("Main research question or topic"))
        self.query_text = QPlainTextEdit()
        self.query_text.setPlaceholderText("Example: telehealth cancer treatment in rural West Texas")
        self.query_text.setMinimumHeight(92)
        self.query_text.textChanged.connect(self._validate_run_state)
        query_layout.addWidget(self.query_text)
        self.query_error = QLabel("")
        self.query_error.setObjectName("ErrorText")
        query_layout.addWidget(self.query_error)
        layout.addWidget(query_box)

        builder_box = QGroupBox("Guided Query Builder")
        builder_layout = QVBoxLayout(builder_box)
        helper = QLabel(
            "Terms within a box are joined with OR. Different boxes are joined with AND. "
            "Exclusion terms are joined with NOT. Year range limits publication years."
        )
        helper.setWordWrap(True)
        builder_layout.addWidget(helper)

        preset_row = QHBoxLayout()
        preset_row.addWidget(QLabel("Starter template"))
        self.preset_combo = QComboBox()
        self.preset_combo.addItems(PRESETS.keys())
        self.preset_combo.currentTextChanged.connect(self._apply_preset)
        preset_row.addWidget(self.preset_combo, 1)
        self.generate_button = QPushButton("Generate Query Preview")
        self.generate_button.clicked.connect(self._generate_query_preview)
        preset_row.addWidget(self.generate_button)
        builder_layout.addLayout(preset_row)

        grid = QGridLayout()
        grid.setColumnStretch(1, 1)
        for row, key in enumerate(("geography", "topic", "intervention", "outcomes", "exclusion")):
            label = QLabel(BLOCK_LABELS[key])
            entry = QLineEdit()
            entry.setPlaceholderText(self._example_for_block(key))
            entry.textChanged.connect(self._validate_run_state)
            field = QComboBox()
            field.addItems(FIELD_TARGETS)
            field.setCurrentText("All Fields" if key == "exclusion" else "Topic")
            field.currentTextChanged.connect(self._validate_run_state)
            clear_button = QPushButton("Clear")
            clear_button.clicked.connect(entry.clear)
            grid.addWidget(label, row, 0)
            grid.addWidget(entry, row, 1)
            grid.addWidget(field, row, 2)
            grid.addWidget(clear_button, row, 3)
            self.concept_widgets[key] = (entry, field)
        builder_layout.addLayout(grid)

        year_row = QHBoxLayout()
        year_row.addWidget(QLabel("Publication year range"))
        self.start_year = QSpinBox()
        self.start_year.setRange(0, 2100)
        self.start_year.setSpecialValueText("Any")
        self.start_year.setValue(self.settings.default_start_year or 0)
        self.end_year = QSpinBox()
        self.end_year.setRange(0, 2100)
        self.end_year.setSpecialValueText("Any")
        self.end_year.setValue(self.settings.default_end_year or 0)
        self.start_year.valueChanged.connect(self._validate_run_state)
        self.end_year.valueChanged.connect(self._validate_run_state)
        year_row.addWidget(self.start_year)
        year_row.addWidget(QLabel("to"))
        year_row.addWidget(self.end_year)
        year_row.addStretch(1)
        builder_layout.addLayout(year_row)

        preview_row = QHBoxLayout()
        self.copy_query_button = QPushButton("Copy Query")
        self.copy_query_button.clicked.connect(self._copy_query)
        self.use_query_button = QPushButton("Use This Query")
        self.use_query_button.clicked.connect(self._use_query_preview)
        preview_row.addStretch(1)
        preview_row.addWidget(self.copy_query_button)
        preview_row.addWidget(self.use_query_button)
        builder_layout.addLayout(preview_row)

        self.query_preview = QPlainTextEdit()
        self.query_preview.setReadOnly(True)
        self.query_preview.setMinimumHeight(140)
        self.query_preview.setPlaceholderText("Generated Web of Science-style query lines will appear here.")
        builder_layout.addWidget(self.query_preview)
        layout.addWidget(builder_box)

        controls = QHBoxLayout()
        controls.addWidget(self._build_source_box(), 1)
        controls.addWidget(self._build_ris_box(), 1)
        controls.addWidget(self._build_options_box(), 1)
        layout.addLayout(controls)

        action_row = QHBoxLayout()
        self.live_warning = QLabel("")
        self.live_warning.setObjectName("WarningText")
        self.live_warning.setWordWrap(True)
        action_row.addWidget(self.live_warning, 1)
        self.run_button = QPushButton("Run Pipeline")
        self.run_button.setObjectName("PrimaryButton")
        self.run_button.clicked.connect(self._run_pipeline)
        action_row.addWidget(self.run_button)
        layout.addLayout(action_row)
        return scroll

    def _build_source_box(self) -> QGroupBox:
        box = QGroupBox("Live API sources")
        layout = QVBoxLayout(box)
        for key in LIVE_API_SOURCES:
            check = QCheckBox(SOURCE_DISPLAY_LABELS[key])
            check.setChecked(bool(self.settings.default_sources.get(key, False)))
            check.stateChanged.connect(self._validate_run_state)
            self.source_checks[key] = check
            layout.addWidget(check)
        layout.addStretch(1)
        return box

    def _build_ris_box(self) -> QGroupBox:
        box = QGroupBox("RIS files")
        layout = QVBoxLayout(box)
        path_row = QHBoxLayout()
        self.ris_folder_label = QLabel(str(self.settings.resolved_ris_input_folder()))
        self.ris_folder_label.setWordWrap(True)
        path_row.addWidget(self.ris_folder_label, 1)
        browse = QPushButton("Browse")
        browse.clicked.connect(self._browse_ris_folder)
        path_row.addWidget(browse)
        layout.addLayout(path_row)
        self.ris_list = QWidget()
        self.ris_list_layout = QVBoxLayout(self.ris_list)
        self.ris_list_layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.ris_list)
        button_row = QHBoxLayout()
        add_files = QPushButton("Add RIS Files")
        add_files.clicked.connect(self._add_ris_files)
        refresh = QPushButton("Refresh RIS")
        refresh.clicked.connect(self._refresh_ris_files)
        button_row.addWidget(add_files)
        button_row.addWidget(refresh)
        layout.addLayout(button_row)
        self.ris_warning = QLabel("")
        self.ris_warning.setObjectName("WarningText")
        self.ris_warning.setWordWrap(True)
        layout.addWidget(self.ris_warning)
        return box

    def _build_options_box(self) -> QGroupBox:
        box = QGroupBox("Pipeline options")
        layout = QVBoxLayout(box)
        self.dry_run_check = QCheckBox("Dry Run")
        self.dry_run_check.setChecked(True)
        self.dry_run_check.stateChanged.connect(self._validate_run_state)
        self.safe_local_check = QCheckBox("Safe Local Test")
        self.safe_local_check.stateChanged.connect(self._validate_run_state)
        self.ris_only_check = QCheckBox("RIS-only mode")
        self.ris_only_check.stateChanged.connect(self._validate_run_state)
        self.include_ris_check = QCheckBox("Include selected RIS files")
        self.include_ris_check.setChecked(True)
        self.include_ris_check.stateChanged.connect(self._validate_run_state)
        layout.addWidget(self.dry_run_check)
        layout.addWidget(self.safe_local_check)
        layout.addWidget(self.ris_only_check)
        layout.addWidget(self.include_ris_check)

        self.advanced_check = QCheckBox("Show advanced options")
        self.advanced_check.stateChanged.connect(self._toggle_advanced)
        layout.addWidget(self.advanced_check)
        self.advanced_panel = QWidget()
        advanced_layout = QFormLayout(self.advanced_panel)
        self.scaling_combo = QComboBox()
        self.scaling_combo.addItems(SCALING_MODES)
        self.scaling_combo.setCurrentText("medium")
        self.concept_profile_combo = QComboBox()
        self.concept_profile_combo.addItem("")
        self.concept_profile_combo.addItems(CONCEPT_PROFILES)
        self.slug_edit = QLineEdit()
        self.qa_only_check = QCheckBox("QA only")
        self.enforce_concepts_check = QCheckBox("Enforce concept blocks")
        self.geo_extract_check = QCheckBox("Extract geography")
        self.demo_extract_check = QCheckBox("Extract demographics")
        self.rebuild_geo_check = QCheckBox("Rebuild geography cache")
        self.rebuild_demo_check = QCheckBox("Rebuild demographic cache")
        self.skip_rxnorm_check = QCheckBox("Skip RxNorm during visualizations")
        advanced_layout.addRow("Scaling", self.scaling_combo)
        advanced_layout.addRow("Concept profile", self.concept_profile_combo)
        advanced_layout.addRow("Output slug", self.slug_edit)
        for check in (
            self.qa_only_check,
            self.enforce_concepts_check,
            self.geo_extract_check,
            self.demo_extract_check,
            self.rebuild_geo_check,
            self.rebuild_demo_check,
            self.skip_rxnorm_check,
        ):
            advanced_layout.addRow(check)
        self.advanced_panel.setVisible(False)
        layout.addWidget(self.advanced_panel)
        return box

    def _build_outputs_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(14, 14, 14, 18)
        layout.setSpacing(12)
        self.progress = QProgressBar()
        self.progress.setRange(0, 1)
        self.progress.setValue(0)
        layout.addWidget(self.progress)

        split = QHBoxLayout()
        results_box = QGroupBox("Actual output files")
        results_layout = QVBoxLayout(results_box)
        self.results_list = QListWidget()
        self.results_list.itemDoubleClicked.connect(self._open_selected_output)
        results_layout.addWidget(self.results_list)
        result_buttons = QHBoxLayout()
        open_selected = QPushButton("Open Selected")
        open_selected.clicked.connect(self._open_selected_output)
        open_output = QPushButton("Open Output Folder")
        open_output.clicked.connect(lambda: self._open_path(self.settings.resolved_output_folder()))
        result_buttons.addWidget(open_selected)
        result_buttons.addWidget(open_output)
        results_layout.addLayout(result_buttons)
        split.addWidget(results_box, 1)

        logs_box = QGroupBox("Run log")
        logs_layout = QVBoxLayout(logs_box)
        self.log_text = QTextEdit()
        self.log_text.setReadOnly(True)
        logs_layout.addWidget(self.log_text)
        log_buttons = QHBoxLayout()
        clear_logs = QPushButton("Clear Display")
        clear_logs.clicked.connect(self.log_text.clear)
        open_log_folder = QPushButton("Open Logs Folder")
        open_log_folder.clicked.connect(lambda: self._open_path(self.settings.resolved_logs_folder()))
        log_buttons.addWidget(clear_logs)
        log_buttons.addWidget(open_log_folder)
        logs_layout.addLayout(log_buttons)
        split.addWidget(logs_box, 1)
        layout.addLayout(split, 1)

        secondary = QHBoxLayout()
        visuals = QPushButton("Generate Visuals")
        visuals.clicked.connect(self._run_visualizations)
        vos = QPushButton("Generate VOS Networks")
        vos.clicked.connect(self._run_vos_networks)
        csv_to_vos = QPushButton("Convert CSV to VOS TXT")
        csv_to_vos.clicked.connect(self._run_vos_converter)
        secondary.addWidget(visuals)
        secondary.addWidget(vos)
        secondary.addWidget(csv_to_vos)
        secondary.addStretch(1)
        layout.addLayout(secondary)
        return page

    def _build_setup_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(14, 14, 14, 18)
        layout.setSpacing(12)

        setup_box = QGroupBox("Folders and pipeline")
        form = QFormLayout(setup_box)
        self.dansbib_path = QLineEdit(str(self.settings.resolved_dansbib_path()))
        self.output_path = QLineEdit(str(self.settings.resolved_output_folder()))
        self.ris_path = QLineEdit(str(self.settings.resolved_ris_input_folder()))
        self.logs_path = QLineEdit(str(self.settings.resolved_logs_folder()))
        self.python_path = QLineEdit(str(self.settings.resolved_pipeline_python() or ""))
        form.addRow("DansBib folder", self._path_picker_row(self.dansbib_path, self._browse_dansbib_folder))
        form.addRow("Output folder", self._path_picker_row(self.output_path, self._browse_output_folder))
        form.addRow("RIS input folder", self._path_picker_row(self.ris_path, self._browse_ris_folder_from_setup))
        form.addRow("Logs folder", self._path_picker_row(self.logs_path, self._browse_logs_folder))
        form.addRow("Pipeline Python", self._path_picker_row(self.python_path, self._browse_python_file))
        save = QPushButton("Save Settings")
        save.clicked.connect(self._save_settings_from_setup)
        form.addRow(save)
        layout.addWidget(setup_box)

        diagnostics_box = QGroupBox("About / Diagnostics")
        diagnostics_layout = QVBoxLayout(diagnostics_box)
        self.diagnostics_text = QTextEdit()
        self.diagnostics_text.setReadOnly(True)
        diagnostics_layout.addWidget(self.diagnostics_text)
        diag_buttons = QHBoxLayout()
        refresh = QPushButton("Refresh Diagnostics")
        refresh.clicked.connect(self._refresh_diagnostics)
        copy = QPushButton("Copy Diagnostic Report")
        copy.clicked.connect(self._copy_diagnostics)
        diag_buttons.addWidget(refresh)
        diag_buttons.addWidget(copy)
        diag_buttons.addStretch(1)
        diagnostics_layout.addLayout(diag_buttons)
        layout.addWidget(diagnostics_box, 1)
        return page

    def _path_picker_row(self, edit: QLineEdit, callback: Callable[[], None]) -> QWidget:
        row = QWidget()
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(edit, 1)
        button = QPushButton("Browse")
        button.clicked.connect(callback)
        layout.addWidget(button)
        return row

    def _apply_style(self) -> None:
        self.setStyleSheet(
            """
            QMainWindow { background: #f4f6f8; }
            QFrame#Header { background: #17324d; border-radius: 6px; }
            QLabel#Title { color: white; }
            QLabel#Subtitle { color: #d7e4ef; }
            QLabel#StatusPill { background: #e7f6ef; color: #0d5f3c; padding: 6px 10px; border-radius: 4px; }
            QGroupBox { font-weight: 600; border: 1px solid #c8d0d8; border-radius: 6px; margin-top: 10px; padding: 10px; background: #ffffff; }
            QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 4px; }
            QLineEdit, QPlainTextEdit, QTextEdit, QListWidget, QComboBox, QSpinBox {
                background: white; border: 1px solid #aeb7c2; border-radius: 4px; padding: 5px;
            }
            QPushButton { padding: 7px 12px; border: 1px solid #9aa6b2; border-radius: 4px; background: #eef2f6; }
            QPushButton:hover { background: #e1e8ef; }
            QPushButton#PrimaryButton { background: #195d8f; color: white; border-color: #195d8f; font-weight: 700; }
            QPushButton#PrimaryButton:disabled { background: #9fb5c8; border-color: #9fb5c8; }
            QLabel#ErrorText { color: #a62929; font-weight: 600; }
            QLabel#WarningText { color: #8a5a00; }
            """
        )

    def _example_for_block(self, key: str) -> str:
        examples = {
            "geography": "west texas, texas, rural",
            "topic": "cancer treatment, neoplasms, oncology",
            "intervention": "telehealth, telemedicine, mobile health",
            "outcomes": "healthcare access, survival, outcomes",
            "exclusion": "animal",
        }
        return examples[key]

    def _apply_preset(self, name: str) -> None:
        preset = PRESETS.get(name, {})
        for key, (entry, _) in self.concept_widgets.items():
            entry.setText(str(preset.get(key, "")))
        self.start_year.setValue(int(preset.get("start_year") or self.settings.default_start_year or 0))
        self.end_year.setValue(int(preset.get("end_year") or self.settings.default_end_year or 0))
        self._generate_query_preview()

    def _concept_blocks(self) -> list[ConceptBlock]:
        blocks = []
        for key, (entry, field) in self.concept_widgets.items():
            blocks.append(ConceptBlock(key=key, terms=parse_terms(entry.text()), field_target=field.currentText()))
        return blocks

    def _year_value(self, spinbox: QSpinBox) -> int | None:
        value = spinbox.value()
        return value if value else None

    def _build_query_result(self):
        return build_wos_numbered_query(
            self._concept_blocks(),
            start_year=self._year_value(self.start_year),
            end_year=self._year_value(self.end_year),
        )

    def _generate_query_preview(self) -> None:
        result = self._build_query_result()
        text = result.preview
        if result.errors:
            text = f"{text}\n\nValidation:\n" + "\n".join(f"- {error}" for error in result.errors)
        self.query_preview.setPlainText(text.strip())
        self._validate_run_state()

    def _copy_query(self) -> None:
        QApplication.clipboard().setText(self.query_preview.toPlainText())

    def _use_query_preview(self) -> None:
        result = self._build_query_result()
        if not result.has_query:
            self.query_error.setText("; ".join(result.errors) or "Add query terms first.")
            return
        self.query_text.setPlainText(result.machine_query)

    def _selected_sources(self) -> list[str]:
        if self.ris_only_check.isChecked() or self.safe_local_check.isChecked():
            return []
        return [key for key, check in self.source_checks.items() if check.isChecked()]

    def _selected_ris_files(self) -> list[Path]:
        return [path for path, check in self.ris_checks.items() if check.isChecked()]

    def _selected_ris_inputs(self) -> list[RisInput]:
        inputs: list[RisInput] = []
        for path, check in self.ris_checks.items():
            if not check.isChecked():
                continue
            combo = self.ris_source_combos.get(path)
            source = normalize_ris_source(combo.currentData() if combo else "unknown")
            inputs.append(RisInput(path=path, source=source))
        return inputs

    def _validate_run_state(self) -> None:
        query = self.query_text.toPlainText().strip()
        result = self._build_query_result()
        has_query = bool(query) or result.has_query
        errors = []
        if not has_query:
            errors.append("Enter a research question/topic or generate a guided query.")
        errors.extend(result.errors if not query else [])
        ris_only = self.ris_only_check.isChecked() or self.safe_local_check.isChecked()
        for check in self.source_checks.values():
            check.setEnabled(not ris_only)
        if self.include_ris_check.isChecked() and (self.ris_only_check.isChecked() or self._selected_ris_files()):
            if not self._selected_ris_files():
                self.ris_warning.setText("RIS is selected, but no RIS files are checked.")
            else:
                self.ris_warning.setText("")
        else:
            self.ris_warning.setText("")
        live_sources = [source for source in self._selected_sources() if source in LIVE_API_SOURCES]
        if live_sources and not self.dry_run_check.isChecked():
            self.live_warning.setText("Live API sources are selected. This may take several minutes and requires network/API access.")
        else:
            self.live_warning.setText("")
        self.query_error.setText("; ".join(errors))
        self.run_button.setEnabled(has_query and not errors and self.active_thread is None)

    def _refresh_ris_files(self) -> None:
        while self.ris_list_layout.count():
            item = self.ris_list_layout.takeAt(0)
            widget = item.widget()
            if widget:
                widget.deleteLater()
        self.ris_checks.clear()
        self.ris_source_combos.clear()
        files = list(dict.fromkeys([*list_ris_files(self.settings), *self.manual_ris_files]))
        if not files:
            self.ris_list_layout.addWidget(QLabel("No .ris files found in the configured RIS folder."))
        for path in files:
            row = QWidget()
            row_layout = QHBoxLayout(row)
            row_layout.setContentsMargins(0, 0, 0, 0)
            check = QCheckBox(path.name)
            check.setToolTip(str(path))
            check.stateChanged.connect(self._validate_run_state)
            combo = QComboBox()
            for source in RIS_SOURCE_LABELS:
                combo.addItem(SOURCE_DISPLAY_LABELS[source], source)
            combo.setCurrentIndex(combo.findData("unknown"))
            combo.currentIndexChanged.connect(self._validate_run_state)
            self.ris_checks[path] = check
            self.ris_source_combos[path] = combo
            row_layout.addWidget(check, 1)
            row_layout.addWidget(combo)
            self.ris_list_layout.addWidget(row)
        self.ris_list_layout.addStretch(1)
        self.ris_folder_label.setText(str(self.settings.resolved_ris_input_folder()))
        self._validate_run_state()

    def _request_from_ui(self) -> PipelineRequest:
        query = self.query_text.toPlainText().strip()
        if not query:
            result = self._build_query_result()
            query = result.machine_query
        return PipelineRequest(
            query=query,
            sources=self._selected_sources(),
            filters={"review": None, "early_access": None, "open_access": None},
            ris_files=self._selected_ris_files(),
            include_ris=self.include_ris_check.isChecked(),
            scaling_mode=self.scaling_combo.currentText(),
            ris_inputs=self._selected_ris_inputs(),
            ris_only_mode=self.ris_only_check.isChecked(),
            slug=self.slug_edit.text().strip(),
            start_year=self._year_value(self.start_year),
            end_year=self._year_value(self.end_year),
            concept_profile=self.concept_profile_combo.currentText(),
            qa_only=self.qa_only_check.isChecked(),
            enforce_concept_blocks=self.enforce_concepts_check.isChecked(),
            rebuild_geo_cache=self.rebuild_geo_check.isChecked(),
            rebuild_demographic_cache=self.rebuild_demo_check.isChecked(),
            extract_geography=self.geo_extract_check.isChecked(),
            extract_demographics=self.demo_extract_check.isChecked(),
            dry_run=self.dry_run_check.isChecked(),
            safe_local_test=self.safe_local_check.isChecked(),
        )

    def _run_pipeline(self) -> None:
        live_sources = [source for source in self._selected_sources() if source in LIVE_API_SOURCES]
        if live_sources and not self.dry_run_check.isChecked():
            answer = QMessageBox.question(
                self,
                "Confirm live run",
                "This may take several minutes and requires network/API access. Continue?",
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
        request = self._request_from_ui()
        self._start_task(lambda log: run_pipeline(request, self.settings, log), "Running pipeline...")

    def _run_visualizations(self) -> None:
        core = str(self.last_result.get("core_dataset") or "")
        query = self.query_text.toPlainText().strip()
        self._start_task(lambda log: run_visualizations(core or None, query, self.skip_rxnorm_check.isChecked(), self.settings, log), "Generating visuals...")

    def _run_vos_networks(self) -> None:
        core = str(self.last_result.get("core_dataset") or "")
        self._start_task(lambda log: run_vos_networks(core or None, self.settings, log), "Generating VOS networks...")

    def _run_vos_converter(self) -> None:
        csv_path, _ = QFileDialog.getOpenFileName(self, "Select CSV file", str(self.settings.resolved_output_folder()), "CSV files (*.csv)")
        if not csv_path:
            return
        self._start_task(lambda log: run_vos_validator(csv_path, self.settings, log), "Converting CSV to VOS TXT...")

    def _start_task(self, task: Callable[[Callable[[str], None]], dict[str, object]], status: str) -> None:
        if self.active_thread is not None:
            return
        self.status_label.setText(status)
        self.progress.setRange(0, 0)
        self.run_button.setEnabled(False)
        self.tabs.setCurrentIndex(1)
        self.active_thread = TaskThread(task, self)
        self.active_thread.log.connect(self._append_log)
        self.active_thread.done.connect(self._task_done)
        self.active_thread.failed.connect(self._task_failed)
        self.active_thread.finished.connect(self._task_finished)
        self.active_thread.start()

    def _task_done(self, result: object) -> None:
        if isinstance(result, dict):
            self.last_result = result
            self._display_results(result)
            self._append_log("Completed.")

    def _task_failed(self, friendly: str, technical: str) -> None:
        self._append_log(friendly)
        self._append_log(technical)
        QMessageBox.critical(self, "DansBib run failed", f"{friendly}\n\nTechnical details were saved in the log display and run log.")

    def _task_finished(self) -> None:
        self.progress.setRange(0, 1)
        self.progress.setValue(1)
        self.status_label.setText("Ready")
        self.active_thread = None
        self._validate_run_state()
        self._refresh_diagnostics()

    def _append_log(self, message: str) -> None:
        self.log_text.append(str(message))

    def _display_results(self, result: dict[str, object]) -> None:
        self.results_list.clear()
        summary = result.get("summary")
        if isinstance(summary, dict) and summary:
            self.results_list.addItem(f"Summary: {summary}")
        for raw_path in result.get("output_files", []) if isinstance(result.get("output_files"), list) else []:
            item = QListWidgetItem(str(raw_path))
            item.setData(Qt.ItemDataRole.UserRole, str(raw_path))
            self.results_list.addItem(item)

    def _open_selected_output(self) -> None:
        item = self.results_list.currentItem()
        if not item:
            return
        path = item.data(Qt.ItemDataRole.UserRole) or item.text()
        self._open_path(Path(str(path)))

    def _open_path(self, path: Path) -> None:
        try:
            open_path(path)
        except Exception as exc:
            QMessageBox.warning(self, "Could not open path", friendly_error_message(exc))

    def _toggle_advanced(self) -> None:
        self.advanced_panel.setVisible(self.advanced_check.isChecked())

    def _browse_folder(self, title: str, start: Path) -> str:
        return QFileDialog.getExistingDirectory(self, title, str(start))

    def _browse_dansbib_folder(self) -> None:
        path = self._browse_folder("Select DansBib folder", self.settings.resolved_dansbib_path())
        if path:
            self.dansbib_path.setText(path)

    def _browse_output_folder(self) -> None:
        path = self._browse_folder("Select output folder", self.settings.resolved_output_folder())
        if path:
            self.output_path.setText(path)

    def _browse_ris_folder(self) -> None:
        path = self._browse_folder("Select RIS input folder", self.settings.resolved_ris_input_folder())
        if path:
            self.settings = replace(self.settings, ris_input_folder=path)
            save_config(self.settings)
            self.ris_path.setText(path)
            self._refresh_ris_files()

    def _add_ris_files(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(self, "Select RIS files", str(self.settings.resolved_ris_input_folder()), "RIS files (*.ris)")
        if not paths:
            return
        for raw_path in paths:
            path = Path(raw_path)
            if path not in self.manual_ris_files:
                self.manual_ris_files.append(path)
        self._refresh_ris_files()

    def _browse_ris_folder_from_setup(self) -> None:
        path = self._browse_folder("Select RIS input folder", self.settings.resolved_ris_input_folder())
        if path:
            self.ris_path.setText(path)

    def _browse_logs_folder(self) -> None:
        path = self._browse_folder("Select logs folder", self.settings.resolved_logs_folder())
        if path:
            self.logs_path.setText(path)

    def _browse_python_file(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Select pipeline Python executable", str(self.settings.resolved_dansbib_path()))
        if path:
            self.python_path.setText(path)

    def _save_settings_from_setup(self) -> None:
        self.settings = replace(
            self.settings,
            dansbib_path=self.dansbib_path.text().strip(),
            output_folder=self.output_path.text().strip(),
            ris_input_folder=self.ris_path.text().strip(),
            logs_folder=self.logs_path.text().strip(),
            pipeline_python=self.python_path.text().strip(),
            default_start_year=self._year_value(self.start_year),
            default_end_year=self._year_value(self.end_year),
            default_sources={key: check.isChecked() for key, check in self.source_checks.items()},
        )
        path = save_config(self.settings)
        self._append_log(f"Settings saved: {path}")
        self._refresh_ris_files()
        self._refresh_diagnostics()
        QMessageBox.information(self, "Settings saved", "Settings were saved successfully.")

    def _refresh_diagnostics(self) -> None:
        self.diagnostics_text.setPlainText(collect_diagnostics(self.settings).as_text())

    def _copy_diagnostics(self) -> None:
        QApplication.clipboard().setText(self.diagnostics_text.toPlainText())


def main(smoke_path: str | None = None) -> int:
    app = QApplication.instance() or QApplication([])
    app.setApplicationName("DansBib GUI")
    app.setApplicationVersion(APP_VERSION)
    window = DansBibQtWindow()
    window.show()
    if smoke_path:
        app.processEvents()
        target = Path(smoke_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        ok = window.grab().save(str(target))
        print(f"Qt smoke screenshot saved: {target} ({'ok' if ok else 'failed'})", flush=True)
        window.close()
        return 0 if ok else 1
    return app.exec()
