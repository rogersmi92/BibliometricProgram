from __future__ import annotations

import re
import traceback
import ssl
from dataclasses import replace
from pathlib import Path
from typing import Callable
from urllib import error as urlerror
from urllib import parse as urlparse
from urllib import request as urlrequest

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
    QSizePolicy,
    QSpinBox,
    QTabWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from DansBib.pipeline_capabilities import RIS_SOURCE_LABELS, SOURCE_DISPLAY_LABELS, RisInput, normalize_ris_source

from .utils.app_config import APP_VERSION, AppConfig, clear_secret, get_secret, load_config, masked_secret, save_config, set_secret
from .utils.diagnostics import collect_diagnostics, friendly_error_message
from .utils.file_utils import open_path
from .utils.pipeline_runner import (
    ANALYSIS_OPTIONS,
    CONCEPT_PROFILES,
    LIVE_API_SOURCES,
    MAP_OPTIONS,
    SCALING_MODES,
    PipelineRequest,
    check_map_data_status,
    cleanup_generated_output_folders,
    list_ris_files,
    paths_from_config,
    run_pipeline,
    run_preflight_check,
    run_sample_ris_test,
    run_visualizations,
    run_vos_networks,
    run_vos_validator,
    setup_checklist,
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
        self.analysis_checks: dict[str, QCheckBox] = {}
        self.map_checks: dict[str, QCheckBox] = {}
        self.ris_checks: dict[Path, QCheckBox] = {}
        self.ris_source_combos: dict[Path, QComboBox] = {}
        self.manual_ris_files: list[Path] = []

        self.setWindowTitle("DansBib Bibliometric Workflow")
        self.setMinimumSize(560, 420)
        self._build_ui()
        self._apply_initial_window_geometry()
        self._update_api_credentials_status()
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
        title = QLabel("DansBib Bibliometric Workflow")
        title.setObjectName("Title")
        title_font = QFont()
        title_font.setPointSize(20)
        title_font.setBold(True)
        title.setFont(title_font)
        subtitle = QLabel("Build a search, choose sources, run analysis, and review outputs")
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

        self.tabs.addTab(self._build_run_tab(), "Build && Run")
        self.tabs.addTab(self._build_outputs_tab(), "Outputs && Logs")
        self.tabs.addTab(self._build_setup_tab(), "Setup && Diagnostics")

    def _apply_initial_window_geometry(self) -> None:
        screen = QApplication.primaryScreen()
        if screen is None:
            self.resize(1100, 760)
            return
        available = screen.availableGeometry()
        saved_width = int(self.settings.window_width or 0)
        saved_height = int(self.settings.window_height or 0)
        width = saved_width if saved_width else min(1260, max(self.minimumWidth(), int(available.width() * 0.88)))
        height = saved_height if saved_height else min(860, max(self.minimumHeight(), int(available.height() * 0.88)))
        width = min(width, max(self.minimumWidth(), available.width() - 40))
        height = min(height, max(self.minimumHeight(), available.height() - 40))
        self.resize(width, height)
        saved_x = self.settings.window_x
        saved_y = self.settings.window_y
        if saved_x is not None and saved_y is not None and available.contains(saved_x, saved_y):
            self.move(saved_x, saved_y)
        else:
            frame = self.frameGeometry()
            frame.moveCenter(available.center())
            self.move(frame.topLeft())

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt override
        self._save_window_state()
        super().closeEvent(event)

    def _save_window_state(self) -> None:
        geometry = self.geometry()
        self.settings = replace(
            self.settings,
            window_width=geometry.width(),
            window_height=geometry.height(),
            window_x=geometry.x(),
            window_y=geometry.y(),
        )
        save_config(self.settings)

    def _scroll_page(self, page: QWidget) -> QScrollArea:
        page.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        scroll.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        scroll.setWidget(page)
        return scroll

    def _build_run_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(14, 14, 14, 18)
        layout.setSpacing(14)

        query_box = QGroupBox("Step 1: Search Query")
        query_layout = QVBoxLayout(query_box)
        query_layout.setSpacing(8)
        query_layout.addWidget(QLabel("Research question or search topic"))
        self.query_text = QPlainTextEdit()
        self.query_text.setPlaceholderText("Example: telehealth cancer treatment in rural West Texas")
        self.query_text.setMinimumHeight(92)
        self.query_text.textChanged.connect(self._validate_run_state)
        query_layout.addWidget(self.query_text)
        self.query_error = QLabel("")
        self.query_error.setObjectName("ErrorText")
        self.query_error.setWordWrap(True)
        query_layout.addWidget(self.query_error)
        layout.addWidget(query_box)

        mode_box = QGroupBox("Run mode")
        mode_layout = QVBoxLayout(mode_box)
        self.run_mode_combo = QComboBox()
        self.run_mode_combo.addItems(("RIS-only QA", "Local RIS + visuals", "Live database search", "Maps only"))
        self.run_mode_combo.setToolTip("Choose a common librarian workflow; individual settings can still be adjusted below.")
        mode_layout.addWidget(self.run_mode_combo)
        self.run_mode_note = QLabel("")
        self.run_mode_note.setWordWrap(True)
        self.run_mode_note.setObjectName("StatusNote")
        mode_layout.addWidget(self.run_mode_note)
        layout.addWidget(mode_box)

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
            entry.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            entry.setPlaceholderText(self._example_for_block(key))
            entry.textChanged.connect(self._validate_run_state)
            field = QComboBox()
            field.addItems(FIELD_TARGETS)
            field.setCurrentText("All Fields" if key == "exclusion" else "Topic")
            field.setMinimumContentsLength(8)
            field.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
            field.currentTextChanged.connect(self._validate_run_state)
            clear_button = QPushButton("Clear")
            clear_button.setMaximumWidth(72)
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

        layout.addWidget(self._build_sources_section())
        layout.addWidget(self._build_analysis_box())
        layout.addWidget(self._build_map_box())
        layout.addWidget(self._build_options_box())

        run_box = QGroupBox("Step 4: Review & Run")
        run_layout = QVBoxLayout(run_box)
        self.run_status_note = QLabel("")
        self.run_status_note.setObjectName("StatusNote")
        self.run_status_note.setWordWrap(True)
        run_layout.addWidget(self.run_status_note)
        action_grid = QGridLayout()
        self.live_warning = QLabel("")
        self.live_warning.setObjectName("WarningText")
        self.live_warning.setWordWrap(True)
        self.maps_button = QPushButton("Maps Only")
        self.maps_button.clicked.connect(self._run_maps_only)
        self.visuals_button = QPushButton("Visuals Only")
        self.visuals_button.clicked.connect(self._run_visualizations)
        self.clear_generated_button = QPushButton("Clear Generated Files")
        self.clear_generated_button.clicked.connect(self._clear_generated_files_only)
        self.run_button = QPushButton("Run Full Pipeline")
        self.run_button.setObjectName("PrimaryButton")
        self.run_button.clicked.connect(self._run_pipeline)
        action_grid.setColumnStretch(0, 1)
        action_grid.setColumnStretch(1, 1)
        action_grid.addWidget(self.run_button, 0, 0, 1, 2)
        action_grid.addWidget(self.clear_generated_button, 1, 0)
        action_grid.addWidget(self.maps_button, 1, 1)
        action_grid.addWidget(self.visuals_button, 2, 0, 1, 2)
        run_layout.addLayout(action_grid)
        run_layout.addWidget(self.live_warning)
        layout.addWidget(run_box)
        self.run_mode_combo.currentTextChanged.connect(self._apply_run_mode)
        self._apply_run_mode(self.run_mode_combo.currentText())
        return self._scroll_page(page)

    def _build_sources_section(self) -> QGroupBox:
        box = QGroupBox("Step 2: Sources")
        layout = QVBoxLayout(box)
        layout.addWidget(self._build_source_box())
        layout.addWidget(self._build_ris_box())
        return box

    def _build_source_box(self) -> QGroupBox:
        box = QGroupBox("Online database searches")
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
        box = QGroupBox("Local RIS files")
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
        self.include_ris_check = QCheckBox("Include selected RIS files")
        self.include_ris_check.setChecked(True)
        self.include_ris_check.stateChanged.connect(self._validate_run_state)
        layout.addWidget(self.include_ris_check)
        self.ris_warning = QLabel("")
        self.ris_warning.setObjectName("WarningText")
        self.ris_warning.setWordWrap(True)
        layout.addWidget(self.ris_warning)
        return box

    def _build_options_box(self) -> QGroupBox:
        box = QGroupBox("Optional settings")
        box.setCheckable(True)
        box.setChecked(False)
        layout = QVBoxLayout(box)
        self.advanced_panel = QWidget()
        advanced_layout = QVBoxLayout(self.advanced_panel)
        advanced_layout.setContentsMargins(0, 8, 0, 0)

        self.dry_run_check = QCheckBox("Dry Run")
        self.dry_run_check.setChecked(False)
        self.dry_run_check.stateChanged.connect(self._validate_run_state)
        self.safe_local_check = QCheckBox("Safe Local Test")
        self.safe_local_check.stateChanged.connect(self._validate_run_state)
        self.ris_only_check = QCheckBox("RIS-only mode")
        self.ris_only_check.stateChanged.connect(self._validate_run_state)
        advanced_layout.addWidget(self.dry_run_check)
        advanced_layout.addWidget(self.safe_local_check)
        advanced_layout.addWidget(self.ris_only_check)

        advanced_form = QFormLayout()
        advanced_form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)
        advanced_form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
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
        self.clear_similar_outputs_check = QCheckBox("Also clear similar old runs with same base query")
        self.clear_similar_outputs_check.setChecked(False)
        advanced_form.addRow("Scaling", self.scaling_combo)
        advanced_form.addRow("Concept profile", self.concept_profile_combo)
        advanced_form.addRow("Output slug", self.slug_edit)
        for check in (
            self.qa_only_check,
            self.enforce_concepts_check,
            self.geo_extract_check,
            self.demo_extract_check,
            self.rebuild_geo_check,
            self.rebuild_demo_check,
            self.skip_rxnorm_check,
            self.clear_similar_outputs_check,
        ):
            advanced_form.addRow(check)
        advanced_layout.addLayout(advanced_form)
        advanced_buttons = QHBoxLayout()
        self.check_data_button = QPushButton("Check Setup")
        self.check_data_button.clicked.connect(self._check_data_status)
        advanced_buttons.addWidget(self.check_data_button)
        self.term_button = QPushButton("Run Geography Extraction Only")
        self.term_button.clicked.connect(self._run_terms_only)
        advanced_buttons.addWidget(self.term_button)
        advanced_buttons.addStretch(1)
        advanced_layout.addLayout(advanced_buttons)
        self.advanced_panel.setVisible(False)
        box.toggled.connect(self.advanced_panel.setVisible)
        layout.addWidget(self.advanced_panel)
        return box

    def _build_analysis_box(self) -> QGroupBox:
        box = QGroupBox("Step 3: Outputs to Generate")
        box.setToolTip("Analysis Options")
        layout = QVBoxLayout(box)
        defaults = {
            "geography": True,
            "maps": True,
            "standard_visuals": True,
            "vos_networks": False,
        }
        labels = {
            "geography": "Run geography extraction",
            "maps": "Generate maps",
            "standard_visuals": "Generate standard bibliometric visuals",
            "vos_networks": "Generate VOS/network visuals",
        }
        for key in ("geography", "maps", "standard_visuals", "vos_networks"):
            label = labels.get(key, ANALYSIS_OPTIONS.get(key, key))
            check = QCheckBox(label)
            check.setChecked(defaults.get(key, False))
            check.stateChanged.connect(self._validate_run_state)
            self.analysis_checks[key] = check
            layout.addWidget(check)
        layout.addStretch(1)
        return box

    def _build_map_box(self) -> QGroupBox:
        box = QGroupBox("Optional map options")
        box.setToolTip("Map Options")
        box.setCheckable(True)
        box.setChecked(False)
        layout = QVBoxLayout(box)
        self.map_options_panel = QWidget()
        panel_layout = QVBoxLayout(self.map_options_panel)
        panel_layout.setContentsMargins(0, 8, 0, 0)
        defaults = {
            "world": True,
            "us": True,
            "priority_adm1": True,
            "priority_adm2": False,
            "city_points": False,
            "institution_points": False,
            "build_world_adm0": True,
            "check_map_status": True,
        }
        for key, label in MAP_OPTIONS.items():
            check = QCheckBox(label)
            check.setChecked(defaults.get(key, False))
            check.stateChanged.connect(self._validate_run_state)
            self.map_checks[key] = check
            panel_layout.addWidget(check)
        self.label_point_maps_check = QCheckBox("Label top city/institution points")
        self.label_point_maps_check.setChecked(True)
        self.label_point_maps_check.stateChanged.connect(self._validate_run_state)
        label_row = QHBoxLayout()
        label_row.addWidget(self.label_point_maps_check)
        label_row.addWidget(QLabel("Top N"))
        self.label_point_maps_spin = QSpinBox()
        self.label_point_maps_spin.setRange(1, 20)
        self.label_point_maps_spin.setValue(5)
        self.label_point_maps_spin.valueChanged.connect(self._validate_run_state)
        label_row.addWidget(self.label_point_maps_spin)
        label_row.addStretch(1)
        panel_layout.addLayout(label_row)
        self.enrich_institutions_check = QCheckBox("Enrich missing institution and citation data using Scopus/OpenAlex")
        self.enrich_institutions_check.setToolTip(
            "Uses DOI/EID lookups to retrieve affiliation/institution metadata and citation counts where available."
        )
        self.enrich_institutions_check.setChecked(False)
        self.enrich_institutions_check.stateChanged.connect(self._validate_run_state)
        panel_layout.addWidget(self.enrich_institutions_check)
        credential_row = QHBoxLayout()
        self.enrichment_status_label = QLabel("")
        self.enrichment_status_label.setObjectName("StatusNote")
        self.enrichment_status_label.setWordWrap(True)
        credential_row.addWidget(self.enrichment_status_label, 1)
        self.configure_api_button = QPushButton("Configure API Credentials")
        self.configure_api_button.clicked.connect(self._show_api_credentials_tab)
        credential_row.addWidget(self.configure_api_button)
        panel_layout.addLayout(credential_row)
        self.map_options_panel.setVisible(False)
        box.toggled.connect(self.map_options_panel.setVisible)
        layout.addWidget(self.map_options_panel)
        layout.addStretch(1)
        return box

    def _build_outputs_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(14, 14, 14, 18)
        layout.setSpacing(12)
        progress_box = QGroupBox("Run Progress")
        progress_layout = QFormLayout(progress_box)
        self.current_task_label = QLabel("Idle")
        self.overall_progress = QProgressBar()
        self.overall_progress.setRange(0, 100)
        self.overall_progress.setValue(0)
        self.step_progress = QProgressBar()
        self.step_progress.setRange(0, 1)
        self.step_progress.setValue(0)
        self.progress = self.overall_progress
        progress_layout.addRow("Current task", self.current_task_label)
        progress_layout.addRow("Overall", self.overall_progress)
        progress_layout.addRow("Current step", self.step_progress)
        layout.addWidget(progress_box)

        split = QVBoxLayout()
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

        secondary = QGridLayout()
        clear_generated = QPushButton("Clear Generated Files")
        clear_generated.clicked.connect(self._clear_generated_files_only)
        visuals = QPushButton("Generate Visuals")
        visuals.clicked.connect(self._run_visualizations)
        vos = QPushButton("Generate VOS Networks")
        vos.clicked.connect(self._run_vos_networks)
        csv_to_vos = QPushButton("Convert CSV to VOS TXT")
        csv_to_vos.clicked.connect(self._run_vos_converter)
        secondary.setColumnStretch(0, 1)
        secondary.setColumnStretch(1, 1)
        secondary.addWidget(clear_generated, 0, 0)
        secondary.addWidget(visuals, 0, 1)
        secondary.addWidget(vos, 1, 0)
        secondary.addWidget(csv_to_vos, 1, 1)
        layout.addLayout(secondary)
        return self._scroll_page(page)

    def _build_setup_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(14, 14, 14, 18)
        layout.setSpacing(12)

        layout.addWidget(self._build_setup_checklist_box())

        setup_box = QGroupBox("Folders and pipeline")
        form = QFormLayout(setup_box)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
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

        api_box = QGroupBox("API credentials and enrichment")
        api_layout = QVBoxLayout(api_box)
        self.enable_scopus_enrichment_check = QCheckBox("Enable Scopus enrichment")
        self.enable_scopus_enrichment_check.setChecked(bool(self.settings.enable_scopus_enrichment))
        self.enable_openalex_enrichment_check = QCheckBox("Enable OpenAlex enrichment")
        self.enable_openalex_enrichment_check.setChecked(bool(self.settings.enable_openalex_enrichment))
        api_layout.addWidget(self.enable_scopus_enrichment_check)
        scopus_form = QFormLayout()
        scopus_form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)
        scopus_form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        self.scopus_api_key_edit = QLineEdit()
        self.scopus_api_key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.scopus_inst_token_edit = QLineEdit()
        self.scopus_inst_token_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.scopus_base_url_edit = QLineEdit(self.settings.scopus_base_url or "https://api.elsevier.com/content")
        scopus_form.addRow("Scopus API Key", self.scopus_api_key_edit)
        scopus_form.addRow("Scopus InstToken", self.scopus_inst_token_edit)
        scopus_form.addRow("Scopus base URL", self.scopus_base_url_edit)
        api_layout.addLayout(scopus_form)
        scopus_buttons = QGridLayout()
        save_scopus = QPushButton("Save")
        save_scopus.clicked.connect(self._save_api_credentials)
        test_scopus = QPushButton("Test Scopus Connection")
        test_scopus.clicked.connect(self._test_scopus_connection)
        clear_scopus = QPushButton("Clear Scopus Credentials")
        clear_scopus.clicked.connect(self._clear_scopus_credentials)
        scopus_buttons.setColumnStretch(0, 1)
        scopus_buttons.setColumnStretch(1, 1)
        scopus_buttons.addWidget(save_scopus, 0, 0)
        scopus_buttons.addWidget(test_scopus, 0, 1)
        scopus_buttons.addWidget(clear_scopus, 1, 0, 1, 2)
        api_layout.addLayout(scopus_buttons)
        self.enable_openalex_enrichment_check.stateChanged.connect(self._validate_run_state)
        self.enable_scopus_enrichment_check.stateChanged.connect(self._validate_run_state)
        api_layout.addWidget(self.enable_openalex_enrichment_check)
        openalex_form = QFormLayout()
        openalex_form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)
        openalex_form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        self.openalex_email_edit = QLineEdit(self.settings.openalex_email)
        openalex_form.addRow("OpenAlex email", self.openalex_email_edit)
        api_layout.addLayout(openalex_form)
        proxy_box = QGroupBox("Advanced network settings")
        proxy_layout = QFormLayout(proxy_box)
        proxy_layout.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)
        proxy_layout.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        self.use_system_proxy_check = QCheckBox("Use system proxy settings")
        self.use_system_proxy_check.setChecked(bool(self.settings.use_system_proxy))
        self.http_proxy_edit = QLineEdit(self.settings.http_proxy)
        self.http_proxy_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.https_proxy_edit = QLineEdit(self.settings.https_proxy)
        self.https_proxy_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.use_certifi_check = QCheckBox("Use certifi CA bundle")
        self.use_certifi_check.setChecked(bool(self.settings.use_certifi_ca_bundle))
        certifi_help = QLabel("Fixes common macOS/Python virtual environment certificate issues when external API calls fail with CERTIFICATE_VERIFY_FAILED.")
        certifi_help.setWordWrap(True)
        proxy_layout.addRow(self.use_system_proxy_check)
        proxy_layout.addRow("HTTP proxy", self.http_proxy_edit)
        proxy_layout.addRow("HTTPS proxy", self.https_proxy_edit)
        proxy_layout.addRow(self.use_certifi_check)
        proxy_layout.addRow(certifi_help)
        api_layout.addWidget(proxy_box)
        openalex_buttons = QGridLayout()
        save_openalex = QPushButton("Save")
        save_openalex.clicked.connect(self._save_api_credentials)
        test_openalex = QPushButton("Test OpenAlex Connection")
        test_openalex.clicked.connect(self._test_openalex_connection)
        clear_openalex = QPushButton("Clear OpenAlex Settings")
        clear_openalex.clicked.connect(self._clear_openalex_settings)
        test_ssl = QPushButton("Test API SSL/Connectivity")
        test_ssl.clicked.connect(self._test_api_ssl_connectivity)
        openalex_buttons.setColumnStretch(0, 1)
        openalex_buttons.setColumnStretch(1, 1)
        openalex_buttons.addWidget(save_openalex, 0, 0)
        openalex_buttons.addWidget(test_openalex, 0, 1)
        openalex_buttons.addWidget(test_ssl, 1, 0)
        openalex_buttons.addWidget(clear_openalex, 1, 1)
        api_layout.addLayout(openalex_buttons)
        self.api_credentials_status = QLabel("")
        self.api_credentials_status.setWordWrap(True)
        api_layout.addWidget(self.api_credentials_status)
        layout.addWidget(api_box)

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
        self._refresh_setup_checklist()
        return self._scroll_page(page)

    def _build_setup_checklist_box(self) -> QGroupBox:
        box = QGroupBox("First-run setup checklist")
        layout = QVBoxLayout(box)
        self.setup_checklist_rows: dict[str, QLabel] = {}
        for item in ("Python path", "DansBib folder", "RIS folder", "Output folder", "API credentials", "Map data"):
            label = QLabel("")
            label.setWordWrap(True)
            self.setup_checklist_rows[item] = label
            layout.addWidget(label)
        buttons = QGridLayout()
        refresh = QPushButton("Refresh Checklist")
        refresh.clicked.connect(self._refresh_setup_checklist)
        preflight = QPushButton("Run Preflight Check")
        preflight.clicked.connect(self._run_preflight_check)
        sample = QPushButton("Run Sample RIS Test")
        sample.clicked.connect(self._run_sample_ris_test)
        buttons.setColumnStretch(0, 1)
        buttons.setColumnStretch(1, 1)
        buttons.addWidget(refresh, 0, 0)
        buttons.addWidget(preflight, 0, 1)
        buttons.addWidget(sample, 1, 0, 1, 2)
        layout.addLayout(buttons)
        return box

    def _path_picker_row(self, edit: QLineEdit, callback: Callable[[], None]) -> QWidget:
        row = QWidget()
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(edit, 1)
        button = QPushButton("Browse")
        button.clicked.connect(callback)
        layout.addWidget(button)
        return row

    def _refresh_setup_checklist(self) -> None:
        if not hasattr(self, "setup_checklist_rows"):
            return
        icons = {"ok": "OK", "warning": "Check", "error": "Fix"}
        for check in setup_checklist(self.settings):
            item = check["item"]
            label = self.setup_checklist_rows.get(item)
            if label is None:
                continue
            status = check.get("status", "warning")
            label.setText(f"{icons.get(status, 'Check')}: {item}: {check.get('detail', '')}")

    def _run_preflight_check(self) -> None:
        self._start_task(lambda log: run_preflight_check(self.settings, log), "Running setup preflight...")

    def _run_sample_ris_test(self) -> None:
        answer = QMessageBox.question(
            self,
            "Run sample RIS test",
            "This will run a small local RIS-only QA test and write results to a timestamped run folder. Continue?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self._start_task(lambda log: run_sample_ris_test(self.settings, log), "Running sample RIS test...")

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

    def _apply_run_mode(self, mode: str) -> None:
        if not hasattr(self, "ris_only_check"):
            return
        mode = mode or "RIS-only QA"
        notes = {
            "RIS-only QA": "Checks local RIS import, cleaning, year limits, deduplication, and QA summaries without live database searches.",
            "Local RIS + visuals": "Uses selected local RIS files and generates the usual figures/maps without querying online databases.",
            "Live database search": "Queries selected online databases, optionally combines selected RIS files, and generates outputs.",
            "Maps only": "Regenerates map/visual outputs from existing geography outputs or the selected current dataset.",
        }
        self.run_mode_note.setText(notes.get(mode, ""))

        if mode == "RIS-only QA":
            self.ris_only_check.setChecked(True)
            self.safe_local_check.setChecked(True)
            self.qa_only_check.setChecked(True)
            self.analysis_checks["geography"].setChecked(False)
            self.analysis_checks["maps"].setChecked(False)
            self.analysis_checks["standard_visuals"].setChecked(False)
            self.analysis_checks["vos_networks"].setChecked(False)
        elif mode == "Local RIS + visuals":
            self.ris_only_check.setChecked(True)
            self.safe_local_check.setChecked(True)
            self.qa_only_check.setChecked(False)
            self.analysis_checks["geography"].setChecked(True)
            self.analysis_checks["maps"].setChecked(True)
            self.analysis_checks["standard_visuals"].setChecked(True)
            self.analysis_checks["vos_networks"].setChecked(False)
        elif mode == "Live database search":
            self.ris_only_check.setChecked(False)
            self.safe_local_check.setChecked(False)
            self.qa_only_check.setChecked(False)
            self.analysis_checks["geography"].setChecked(True)
            self.analysis_checks["maps"].setChecked(True)
            self.analysis_checks["standard_visuals"].setChecked(True)
        elif mode == "Maps only":
            self.ris_only_check.setChecked(True)
            self.safe_local_check.setChecked(True)
            self.qa_only_check.setChecked(False)
            self.analysis_checks["geography"].setChecked(False)
            self.analysis_checks["maps"].setChecked(True)
            self.analysis_checks["standard_visuals"].setChecked(True)
            self.analysis_checks["vos_networks"].setChecked(False)
        self._validate_run_state()

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

    def _analysis_enabled(self, key: str) -> bool:
        check = self.analysis_checks.get(key)
        return bool(check and check.isChecked())

    def _map_enabled(self, key: str) -> bool:
        check = self.map_checks.get(key)
        return bool(check and check.isChecked())

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
        elif self._analysis_enabled("maps") and not self._analysis_enabled("geography"):
            self.live_warning.setText("Maps are selected. Geography / GeoCensus extraction should be enabled unless geography outputs already exist.")
        else:
            self.live_warning.setText("")
        self.query_error.setText("; ".join(errors))
        self._update_run_status_note()
        self.run_button.setEnabled(has_query and not errors and self.active_thread is None)
        for button in (
            getattr(self, "check_data_button", None),
            getattr(self, "term_button", None),
            getattr(self, "maps_button", None),
            getattr(self, "visuals_button", None),
            getattr(self, "clear_generated_button", None),
        ):
            if button is not None:
                button.setEnabled(self.active_thread is None)

    def _slug_from_query(self, query: str) -> str:
        text = query.strip().lower()
        chars = [char if char.isalnum() else "_" for char in text]
        slug = "_".join(part for part in "".join(chars).split("_") if part)
        return (slug[:80] or "results").rstrip("_")

    def _update_run_status_note(self) -> None:
        if not hasattr(self, "run_status_note"):
            return
        selected_ris = self._selected_ris_files()
        ris_text = ", ".join(path.name for path in selected_ris[:3]) if selected_ris else f"Folder: {self.settings.resolved_ris_input_folder()}"
        if len(selected_ris) > 3:
            ris_text += f" and {len(selected_ris) - 3} more"
        live_sources = self._selected_sources()
        live_text = ", ".join(SOURCE_DISPLAY_LABELS.get(source, source) for source in live_sources) if live_sources else "off"
        enrichment_text = self._enrichment_status_text()
        input_mode = "RIS-only" if self.ris_only_check.isChecked() or self.safe_local_check.isChecked() else "live database search"
        mode = self.run_mode_combo.currentText() if hasattr(self, "run_mode_combo") else input_mode
        enrichment_enabled = "yes" if self.enrich_institutions_check.isChecked() else "no"
        if hasattr(self, "enrichment_status_label"):
            extra = "\nRIS-only input selected. External DOI enrichment will still run." if self.ris_only_check.isChecked() and self.enrich_institutions_check.isChecked() else ""
            self.enrichment_status_label.setText(f"{enrichment_text}{extra}")
        query = self.query_text.toPlainText().strip()
        if not query:
            result = self._build_query_result()
            query = result.machine_query
        slug = self.slug_edit.text().strip() or self._slug_from_query(query)
        self.run_status_note.setText(
            f"Run mode: {mode}\n"
            f"RIS: {ris_text}\n"
            f"Input mode: {input_mode}\n"
            f"Live API sources: {live_text}\n"
            f"Enrichment enabled: {enrichment_enabled}\n"
            f"External APIs configured: {enrichment_text}\n"
            + ("RIS-only input selected. External DOI enrichment will still run.\n" if self.ris_only_check.isChecked() and self.enrich_institutions_check.isChecked() else "")
            +
            f"Output slug: {slug}\n"
            "Previous outputs: choose Clear / Keep / Cancel when Run starts\n"
            f"Main output location: {self.settings.resolved_output_folder()}"
        )

    def _estimated_api_use(self, request: PipelineRequest) -> str:
        calls = []
        if request.sources:
            calls.append("online database searches: " + ", ".join(SOURCE_DISPLAY_LABELS.get(source, source) for source in request.sources))
        if request.enrich_institutions:
            calls.append(f"institution/citation enrichment: {request.enrichment_source}")
        if request.generate_maps and request.build_world_adm0 and request.map_world:
            calls.append("map build may fetch geoBoundaries data if WORLD_ADM0 is missing")
        return "; ".join(calls) if calls else "none expected"

    def _pre_run_summary_text(self, request: PipelineRequest) -> str:
        selected_ris = request.ris_inputs or [RisInput(path=path, source="unknown") for path in request.ris_files]
        ris_lines = [f"- {Path(item.path).name} ({item.source})" for item in selected_ris[:8]]
        if len(selected_ris) > 8:
            ris_lines.append(f"- and {len(selected_ris) - 8} more")
        outputs = []
        if request.qa_only:
            outputs.append("QA summaries")
        if request.extract_geography:
            outputs.append("geography extraction")
        if request.generate_maps:
            outputs.append("maps")
        if request.generate_visuals:
            outputs.append("figures")
        if request.generate_vos_networks:
            outputs.append("VOS/network files")
        mode = self.run_mode_combo.currentText() if hasattr(self, "run_mode_combo") else "Custom"
        return "\n".join(
            [
                f"Run mode: {mode}",
                f"Query: {request.query[:220]}",
                f"Live sources: {', '.join(request.sources) if request.sources else 'none'}",
                "RIS files:",
                *(ris_lines or ["- none"]),
                f"Outputs: {', '.join(outputs) if outputs else 'core CSV/QA files'}",
                f"Estimated external API use: {self._estimated_api_use(request)}",
                "Run outputs will be written to a new timestamped run folder.",
            ]
        )

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
            check.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
            check.setToolTip(str(path))
            check.stateChanged.connect(self._validate_run_state)
            combo = QComboBox()
            for source in RIS_SOURCE_LABELS:
                combo.addItem(SOURCE_DISPLAY_LABELS[source], source)
            combo.setCurrentIndex(combo.findData("unknown"))
            combo.setMinimumContentsLength(9)
            combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
            combo.currentIndexChanged.connect(self._validate_run_state)
            self.ris_checks[path] = check
            self.ris_source_combos[path] = combo
            row_layout.addWidget(check, 1)
            row_layout.addWidget(combo, 0)
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
            extract_geography=self._analysis_enabled("geography") or self.geo_extract_check.isChecked(),
            extract_drugs=self._analysis_enabled("drugs"),
            extract_procedures=self._analysis_enabled("procedures"),
            extract_demographics=self._analysis_enabled("demographics") or self.demo_extract_check.isChecked(),
            extract_keywords=True,
            enrich_institutions=self.enrich_institutions_check.isChecked(),
            enrichment_source=self._selected_enrichment_source(),
            openalex_email=self.openalex_email_edit.text().strip() if hasattr(self, "openalex_email_edit") else self.settings.openalex_email,
            scopus_base_url=self.scopus_base_url_edit.text().strip() if hasattr(self, "scopus_base_url_edit") else self.settings.scopus_base_url,
            use_system_proxy=self.use_system_proxy_check.isChecked() if hasattr(self, "use_system_proxy_check") else self.settings.use_system_proxy,
            http_proxy=self.http_proxy_edit.text().strip() if hasattr(self, "http_proxy_edit") else self.settings.http_proxy,
            https_proxy=self.https_proxy_edit.text().strip() if hasattr(self, "https_proxy_edit") else self.settings.https_proxy,
            use_certifi_ca_bundle=self.use_certifi_check.isChecked() if hasattr(self, "use_certifi_check") else self.settings.use_certifi_ca_bundle,
            generate_maps=self._analysis_enabled("maps"),
            generate_visuals=self._analysis_enabled("standard_visuals"),
            generate_vos_networks=self._analysis_enabled("vos_networks"),
            map_world=self._map_enabled("world"),
            map_us=self._map_enabled("us"),
            map_priority_adm1=self._map_enabled("priority_adm1"),
            map_priority_adm2=self._map_enabled("priority_adm2"),
            map_city_points=self._map_enabled("city_points"),
            map_institution_points=self._map_enabled("institution_points"),
            label_point_maps=self.label_point_maps_check.isChecked(),
            label_top_points=self.label_point_maps_spin.value(),
            build_world_adm0=self._map_enabled("build_world_adm0"),
            check_map_status=self._map_enabled("check_map_status"),
            clear_previous_outputs=False,
            clear_similar_runs=self.clear_similar_outputs_check.isChecked(),
            dry_run=self.dry_run_check.isChecked(),
            safe_local_test=self.safe_local_check.isChecked(),
        )

    def _run_pipeline(self) -> None:
        request = self._request_from_ui()
        if not self._confirm_run_combination(request):
            return
        if not self._confirm_enrichment_run(request):
            return
        answer = QMessageBox.question(
            self,
            "Review run before starting",
            self._pre_run_summary_text(request),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        live_sources = [source for source in request.sources if source in LIVE_API_SOURCES]
        if live_sources and not self.dry_run_check.isChecked():
            answer = QMessageBox.question(
                self,
                "Confirm live run",
                "This may take several minutes and requires network/API access. Continue?",
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
        request = self._request_clear_previous_outputs(request)
        if request is None:
            return
        self._start_task(lambda log: run_pipeline(request, self.settings, log), "Running pipeline...")

    def _check_data_status(self) -> None:
        self._start_task(lambda log: check_map_data_status(self.settings, log), "Checking data / map status...")

    def _run_terms_only(self) -> None:
        request = self._request_from_ui()
        request = replace(
            request,
            extract_geography=True,
            extract_drugs=True,
            extract_procedures=True,
            extract_demographics=True,
            extract_keywords=False,
            generate_maps=False,
            generate_visuals=False,
            generate_vos_networks=False,
            validate_outputs=True,
        )
        request = self._request_clear_previous_outputs(request)
        if request is None:
            return
        self._start_task(lambda log: run_pipeline(request, self.settings, log), "Running GeoCensus / terms...")

    def _run_maps_only(self) -> None:
        request = self._request_from_ui()
        request = replace(
            request,
            extract_geography=False,
            extract_drugs=False,
            extract_procedures=False,
            extract_demographics=False,
            extract_keywords=False,
            generate_maps=True,
            generate_visuals=True,
            generate_vos_networks=False,
            validate_outputs=True,
        )
        if not self._confirm_run_combination(request):
            return
        request = self._request_clear_previous_outputs(request)
        if request is None:
            return
        self._start_task(lambda log: run_pipeline(request, self.settings, log), "Generating maps...")

    def _confirm_run_combination(self, request: PipelineRequest | None = None) -> bool:
        request = request or self._request_from_ui()
        if request.generate_maps and not request.extract_geography and not self.dry_run_check.isChecked():
            answer = QMessageBox.warning(
                self,
                "Maps may be skipped",
                "Generate maps is selected, but geography / GeoCensus extraction is not selected. Continue only if geography outputs already exist.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            return answer == QMessageBox.StandardButton.Yes
        return True

    def _confirm_enrichment_run(self, request: PipelineRequest | None = None) -> bool:
        request = request or self._request_from_ui()
        if not request.enrich_institutions:
            return True
        scopus_key, _ = get_secret("scopus_api_key")
        scopus_enabled = bool(getattr(self, "enable_scopus_enrichment_check", None) and self.enable_scopus_enrichment_check.isChecked())
        openalex_enabled = bool(getattr(self, "enable_openalex_enrichment_check", None) and self.enable_openalex_enrichment_check.isChecked())
        if request.ris_only_mode or self._selected_ris_files():
            answer = QMessageBox.question(
                self,
                "External API enrichment",
                "Institution and citation enrichment is enabled for a RIS/local run. External API calls may be made. Continue?",
            )
            if answer != QMessageBox.StandardButton.Yes:
                return False
        if scopus_enabled and not scopus_key:
            if openalex_enabled:
                answer = QMessageBox.warning(
                    self,
                    "Scopus credentials missing",
                    "Scopus enrichment is enabled, but Scopus credentials are not configured. Continue with OpenAlex fallback?",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                )
                return answer == QMessageBox.StandardButton.Yes
            QMessageBox.warning(self, "No enrichment credentials", "Scopus enrichment is enabled, but Scopus credentials are not configured. Scopus institution/citation enrichment will be skipped.")
            return False
        if not scopus_enabled and not openalex_enabled:
            QMessageBox.warning(self, "No enrichment source", "No institution/citation enrichment source is enabled.")
            return False
        return True

    def _confirm_clear_generated_files(self, allow_keep: bool) -> bool | None:
        dialog = QMessageBox(self)
        dialog.setIcon(QMessageBox.Icon.Question)
        dialog.setWindowTitle("Clear generated output files")
        dialog.setText("Clear previous generated output files before running?" if allow_keep else "Clear generated output files?")
        dialog.setInformativeText(
            "This will remove generated files from:\n\n"
            "- data/visuals\n- data/outputs\n- data/processed\n- data/VOS\n\n"
            "This will not delete source code, map reference data, RIS input files, raw imported data, or user-selected input files."
        )
        clear_button = dialog.addButton("Clear generated files and run" if allow_keep else "Clear Generated Files", QMessageBox.ButtonRole.YesRole)
        keep_button = dialog.addButton("Keep existing files and run", QMessageBox.ButtonRole.NoRole) if allow_keep else None
        cancel_button = dialog.addButton("Cancel Run" if allow_keep else "Cancel", QMessageBox.ButtonRole.RejectRole)
        dialog.setDefaultButton(keep_button or cancel_button)
        dialog.exec()
        clicked = dialog.clickedButton()
        if clicked == clear_button:
            return True
        if clicked == keep_button:
            return False
        if clicked == cancel_button:
            return None
        return None

    def _request_clear_previous_outputs(self, request: PipelineRequest) -> PipelineRequest | None:
        clear = self._confirm_clear_generated_files(allow_keep=True)
        if clear is None:
            self._append_log("Run cancelled before cleanup or execution.")
            return None
        if clear:
            self._append_log("User chose to clear generated output folders before running.")
            self._run_folder_cleanup(show_completion=False)
        else:
            self._append_log("User chose to keep existing generated output files before running.")
        return replace(request, clear_previous_outputs=False)

    def _clear_generated_files_only(self) -> None:
        clear = self._confirm_clear_generated_files(allow_keep=False)
        if clear is None:
            self._append_log("Clear Generated Files cancelled.")
            return
        self._run_folder_cleanup(show_completion=True)

    def _run_folder_cleanup(self, show_completion: bool) -> dict[str, object]:
        runtime = paths_from_config(self.settings)
        cleanup = cleanup_generated_output_folders(
            runtime,
            dry_run=False,
            log_callback=self._append_log,
            log_path=None,
        )
        if show_completion:
            QMessageBox.information(self, "Generated files cleared", self._folder_cleanup_summary(cleanup))
        self._refresh_diagnostics()
        return cleanup

    def _folder_cleanup_summary(self, cleanup: dict[str, object]) -> str:
        counts = cleanup.get("counts_by_folder", {}) if isinstance(cleanup, dict) else {}
        total = int(cleanup.get("total_removed", 0)) if isinstance(cleanup, dict) else 0
        if not total:
            return "No generated files were found in the output folders."
        lines = ["Cleared generated files.", ""]
        for label in ("data/visuals", "data/outputs", "data/processed", "data/VOS"):
            count = int(counts.get(label, 0)) if isinstance(counts, dict) else 0
            lines.append(f"{label}: {count} files removed")
        lines.extend(["", f"Total removed: {total} files."])
        return "\n".join(lines)

    def _run_visualizations(self) -> None:
        core = self._current_or_select_core_dataset("Generate Visuals")
        if not core:
            return
        query = self.query_text.toPlainText().strip()
        self._start_task(lambda log: run_visualizations(str(core), query, self.skip_rxnorm_check.isChecked(), self.settings, log), "Generating visuals...")

    def _run_vos_networks(self) -> None:
        core = self._current_or_select_core_dataset("Generate VOS Networks")
        if not core:
            return
        self._start_task(lambda log: run_vos_networks(str(core), self.settings, log), "Generating VOS networks...")

    def _current_or_select_core_dataset(self, action_name: str) -> Path | None:
        current = Path(str(self.last_result.get("core_dataset") or ""))
        if current.exists() and self._is_valid_core_dataset(current):
            return current
        QMessageBox.information(
            self,
            "Select current dataset",
            f"{action_name} requires a valid core dataset with title, authors, and year columns. Select the current run CSV; old debug/intermediate files will be rejected.",
        )
        csv_path, _ = QFileDialog.getOpenFileName(
            self,
            "Select current core dataset",
            self.settings.last_csv_folder or str(self.settings.resolved_output_folder()),
            "CSV files (*.csv)",
        )
        if not csv_path:
            return None
        self.settings = replace(self.settings, last_csv_folder=str(Path(csv_path).parent))
        save_config(self.settings)
        selected = Path(csv_path)
        if not self._is_valid_core_dataset(selected):
            QMessageBox.warning(
                self,
                "Invalid core dataset",
                "That CSV is not a valid core dataset. Select a current pipeline output such as *_year_limited_records.csv.",
            )
            return None
        self._append_log(f"Selected standalone core dataset: {selected}")
        return selected

    def _is_valid_core_dataset(self, path: Path) -> bool:
        if not path.exists() or path.suffix.lower() != ".csv":
            return False
        lower_name = path.name.lower()
        if "debug" in lower_name or "unparsed" in lower_name:
            return False
        try:
            with path.open("r", encoding="utf-8-sig", errors="replace") as handle:
                header = handle.readline().strip().lower().split(",")
        except OSError:
            return False
        return {"title", "authors", "year"}.issubset({column.strip() for column in header})

    def _run_vos_converter(self) -> None:
        csv_path, _ = QFileDialog.getOpenFileName(self, "Select CSV file", self.settings.last_csv_folder or str(self.settings.resolved_output_folder()), "CSV files (*.csv)")
        if not csv_path:
            return
        self.settings = replace(self.settings, last_csv_folder=str(Path(csv_path).parent))
        save_config(self.settings)
        self._start_task(lambda log: run_vos_validator(csv_path, self.settings, log), "Converting CSV to VOS TXT...")

    def _start_task(self, task: Callable[[Callable[[str], None]], dict[str, object]], status: str) -> None:
        if self.active_thread is not None:
            return
        self.status_label.setText(status)
        self.current_task_label.setText(status)
        self.overall_progress.setRange(0, 100)
        self.overall_progress.setValue(0)
        self.step_progress.setRange(0, 0)
        self.run_button.setEnabled(False)
        for button in (self.check_data_button, self.term_button, self.maps_button, self.visuals_button, self.clear_generated_button):
            button.setEnabled(False)
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
        self.overall_progress.setRange(0, 100)
        self.overall_progress.setValue(100)
        self.step_progress.setRange(0, 1)
        self.step_progress.setValue(1)
        self.current_task_label.setText("Idle")
        self.status_label.setText("Ready")
        self.active_thread = None
        self._validate_run_state()
        self._refresh_diagnostics()
        self._refresh_setup_checklist()

    def _append_log(self, message: str) -> None:
        text = str(message)
        if text.startswith("PIPELINE_STEP:"):
            label = text.split(":", 1)[1].strip()
            self.current_task_label.setText(label)
            self.step_progress.setRange(0, 0)
            current = self.overall_progress.value()
            self.overall_progress.setValue(min(95, current + 12 if current else 5))
        else:
            enrichment = re.search(
                r"(Scopus|OpenAlex|Citation) enrichment:\s+(\d+)/(\d+) records checked(?:,\s+([^.]*)\.)?",
                text,
            )
            if enrichment:
                source = enrichment.group(1)
                current_record = int(enrichment.group(2))
                total_records = max(1, int(enrichment.group(3)))
                detail = enrichment.group(4) or ""
                self.current_task_label.setText(f"{source} enrichment: {current_record}/{total_records} records checked" + (f", {detail}" if detail else ""))
                self.step_progress.setRange(0, total_records)
                self.step_progress.setValue(min(current_record, total_records))
                self.overall_progress.setRange(0, 100)
                self.overall_progress.setValue(min(95, max(self.overall_progress.value(), 35 + int(50 * current_record / total_records))))
            elif "Institution links extracted:" in text or "Citation counts extracted:" in text or "Citation source summary:" in text:
                self.current_task_label.setText(text)
                self.overall_progress.setValue(min(95, max(self.overall_progress.value(), 80)))
        if text.startswith("WARNING:") or " missing" in text.lower() or "skipped" in text.lower():
            self.log_text.append(f"<span style='color:#9a3412; font-weight:600'>{text}</span>")
            return
        self.log_text.append(text)

    def _display_results(self, result: dict[str, object]) -> None:
        self.results_list.clear()
        summary = result.get("summary")
        if isinstance(summary, dict) and summary:
            self.results_list.addItem(f"Summary: {summary}")
        source_status = result.get("source_status")
        if isinstance(source_status, dict) and source_status:
            for source, status in source_status.items():
                self.results_list.addItem(f"Source status - {SOURCE_DISPLAY_LABELS.get(str(source), str(source))}: {status}")
        grouped = self._group_output_files(result.get("output_files", []))
        for group_name in ("Main CSVs", "Maps", "Figures", "VOS/network files", "Manifests", "Logs", "Other"):
            paths = grouped.get(group_name, [])
            if not paths:
                continue
            header = QListWidgetItem(group_name)
            header.setFlags(header.flags() & ~Qt.ItemFlag.ItemIsSelectable)
            self.results_list.addItem(header)
            for raw_path in paths:
                path = Path(str(raw_path))
                item = QListWidgetItem(f"  {path.name}")
                item.setToolTip(str(path))
                item.setData(Qt.ItemDataRole.UserRole, str(path))
                self.results_list.addItem(item)

    def _group_output_files(self, raw_files: object) -> dict[str, list[str]]:
        groups = {
            "Main CSVs": [],
            "Maps": [],
            "Figures": [],
            "VOS/network files": [],
            "Manifests": [],
            "Logs": [],
            "Other": [],
        }
        files = raw_files if isinstance(raw_files, list) else []
        for raw_path in files:
            path = Path(str(raw_path))
            name = path.name.lower()
            suffix = path.suffix.lower()
            parts = {part.lower() for part in path.parts}
            if suffix == ".json" and "manifest" in name:
                groups["Manifests"].append(str(path))
            elif suffix in {".log", ".txt"} and ("log" in name or "logs" in parts):
                groups["Logs"].append(str(path))
            elif "vos" in parts or "network" in name or name.endswith("_overlay.txt"):
                groups["VOS/network files"].append(str(path))
            elif suffix == ".png" and ("map" in name or "geography_heatmap" in name or "geography_points" in name):
                groups["Maps"].append(str(path))
            elif suffix == ".png":
                groups["Figures"].append(str(path))
            elif suffix == ".csv" and any(marker in name for marker in ("year_limited", "raw", "qa_summary", "deduplication", "excluded", "readme", "country", "institution", "term")):
                groups["Main CSVs"].append(str(path))
            else:
                groups["Other"].append(str(path))
        for key in groups:
            groups[key] = sorted(set(groups[key]))
        return groups

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
        paths, _ = QFileDialog.getOpenFileNames(self, "Select RIS files", self.settings.last_ris_folder or str(self.settings.resolved_ris_input_folder()), "RIS files (*.ris)")
        if not paths:
            return
        self.settings = replace(self.settings, last_ris_folder=str(Path(paths[0]).parent))
        save_config(self.settings)
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
            enable_scopus_enrichment=self.enable_scopus_enrichment_check.isChecked(),
            enable_openalex_enrichment=self.enable_openalex_enrichment_check.isChecked(),
            scopus_base_url=self.scopus_base_url_edit.text().strip() or "https://api.elsevier.com/content",
            openalex_email=self.openalex_email_edit.text().strip(),
            use_system_proxy=self.use_system_proxy_check.isChecked(),
            http_proxy=self.http_proxy_edit.text().strip(),
            https_proxy=self.https_proxy_edit.text().strip(),
            use_certifi_ca_bundle=self.use_certifi_check.isChecked(),
        )
        path = save_config(self.settings)
        self._append_log(f"Settings saved: {path}")
        self._update_api_credentials_status()
        self._refresh_ris_files()
        self._refresh_diagnostics()
        self._refresh_setup_checklist()
        QMessageBox.information(self, "Settings saved", "Settings were saved successfully.")

    def _show_api_credentials_tab(self) -> None:
        self.tabs.setCurrentIndex(2)
        self._update_api_credentials_status()

    def _selected_enrichment_source(self) -> str:
        scopus = bool(getattr(self, "enable_scopus_enrichment_check", None) and self.enable_scopus_enrichment_check.isChecked())
        openalex = bool(getattr(self, "enable_openalex_enrichment_check", None) and self.enable_openalex_enrichment_check.isChecked())
        if scopus and openalex:
            return "all"
        if scopus:
            return "scopus"
        if openalex:
            return "openalex"
        return "all"

    def _enrichment_status_text(self) -> str:
        scopus_key, _source = get_secret("scopus_api_key")
        openalex_email = ""
        if hasattr(self, "openalex_email_edit"):
            openalex_email = self.openalex_email_edit.text().strip()
        openalex_email = openalex_email or self.settings.openalex_email
        return f"Scopus {'yes' if scopus_key else 'no'}. OpenAlex {'yes' if openalex_email else 'no'}."

    def _update_api_credentials_status(self) -> None:
        if not hasattr(self, "api_credentials_status"):
            return
        scopus_key, scopus_source = get_secret("scopus_api_key")
        scopus_token, token_source = get_secret("scopus_inst_token")
        self.scopus_api_key_edit.setPlaceholderText(masked_secret(scopus_key) or "Not configured")
        self.scopus_inst_token_edit.setPlaceholderText(masked_secret(scopus_token) or "Optional")
        lines = [
            f"Scopus API key: {'configured' if scopus_key else 'not configured'} ({scopus_source})",
            f"Scopus InstToken: {'configured' if scopus_token else 'not configured'} ({token_source})",
            f"OpenAlex email: {'configured' if self.openalex_email_edit.text().strip() else 'not configured'}",
        ]
        self.api_credentials_status.setText("\n".join(lines))
        if hasattr(self, "enrichment_status_label"):
            self.enrichment_status_label.setText(self._enrichment_status_text())

    def _save_api_credentials(self) -> None:
        key = self.scopus_api_key_edit.text().strip()
        token = self.scopus_inst_token_edit.text().strip()
        stored_messages = []
        if key:
            stored_messages.append(f"Scopus API key saved to {set_secret('scopus_api_key', key)}.")
            self.scopus_api_key_edit.clear()
        if token:
            stored_messages.append(f"Scopus InstToken saved to {set_secret('scopus_inst_token', token)}.")
            self.scopus_inst_token_edit.clear()
        self.settings = replace(
            self.settings,
            enable_scopus_enrichment=self.enable_scopus_enrichment_check.isChecked(),
            enable_openalex_enrichment=self.enable_openalex_enrichment_check.isChecked(),
            scopus_base_url=self.scopus_base_url_edit.text().strip() or "https://api.elsevier.com/content",
            openalex_email=self.openalex_email_edit.text().strip(),
            use_system_proxy=self.use_system_proxy_check.isChecked(),
            http_proxy=self.http_proxy_edit.text().strip(),
            https_proxy=self.https_proxy_edit.text().strip(),
            use_certifi_ca_bundle=self.use_certifi_check.isChecked(),
        )
        save_config(self.settings)
        self._update_api_credentials_status()
        self._refresh_setup_checklist()
        self._append_log("API credential settings saved. Secrets were not logged.")
        QMessageBox.information(self, "API Credentials", "\n".join(stored_messages) if stored_messages else "API settings saved.")

    def _clear_scopus_credentials(self) -> None:
        clear_secret("scopus_api_key")
        clear_secret("scopus_inst_token")
        self.scopus_api_key_edit.clear()
        self.scopus_inst_token_edit.clear()
        self._update_api_credentials_status()
        self._append_log("Scopus credentials cleared.")

    def _clear_openalex_settings(self) -> None:
        self.openalex_email_edit.clear()
        self.enable_openalex_enrichment_check.setChecked(False)
        self.settings = replace(self.settings, openalex_email="", enable_openalex_enrichment=False)
        save_config(self.settings)
        self._update_api_credentials_status()
        self._append_log("OpenAlex settings cleared.")

    def _gui_certifi_path(self) -> str:
        if not getattr(self, "use_certifi_check", None) or not self.use_certifi_check.isChecked():
            return ""
        try:
            import certifi  # type: ignore
        except ImportError:
            return ""
        return certifi.where()

    def _gui_urlopen(self, request_or_url, timeout: int = 20):
        certifi_path = self._gui_certifi_path()
        if certifi_path:
            context = ssl.create_default_context(cafile=certifi_path)
            return urlrequest.urlopen(request_or_url, timeout=timeout, context=context)
        return urlrequest.urlopen(request_or_url, timeout=timeout)

    def _log_gui_http_settings(self, endpoint: str) -> None:
        certifi_path = self._gui_certifi_path()
        self._append_log("API client type: urllib.request")
        self._append_log(f"certifi enabled: {'yes' if certifi_path else 'no'}")
        if certifi_path:
            self._append_log(f"certifi path: {certifi_path}")
        self._append_log(f"proxy detected: {'yes' if urlrequest.getproxies() else 'no'}")
        self._append_log("SSL verification enabled: yes")
        self._append_log(f"endpoint tested: {endpoint}")

    def _test_scopus_connection(self) -> None:
        key, _source = get_secret("scopus_api_key")
        if not key:
            QMessageBox.warning(self, "Scopus Connection", "Scopus API key is not configured.")
            self._append_log("Scopus connection test failed: API key not configured.")
            return
        base_url = (self.scopus_base_url_edit.text().strip() or "https://api.elsevier.com/content").rstrip("/")
        url = f"{base_url}/search/scopus?query={urlparse.quote('TITLE(test)')}&count=1"
        self._append_log("GUI Scopus test endpoint: search/scopus count=1")
        self._append_log("Pipeline Scopus smoke-test endpoint: abstract retrieval by DOI")
        self._append_log("GUI and pipeline HTTP client: urllib.request")
        self._append_log("GUI Scopus test timeout seconds: 20; pipeline enrichment timeout seconds: 20")
        self._log_gui_http_settings("api.elsevier.com search/scopus")
        request = urlrequest.Request(url, headers={"X-ELS-APIKey": key, "Accept": "application/json"})
        token, _ = get_secret("scopus_inst_token")
        if token:
            request.add_header("X-ELS-Insttoken", token)
        try:
            with self._gui_urlopen(request, timeout=20) as response:
                ok = 200 <= int(response.status) < 300
        except urlerror.HTTPError as exc:
            ok = False
            message = f"HTTP {exc.code}"
        except Exception as exc:
            ok = False
            message = f"{exc.__class__.__name__}: {exc}"
        else:
            message = "success" if ok else "unexpected response"
        if ok:
            QMessageBox.information(self, "Scopus Connection", "Scopus connection test succeeded.")
            self._append_log("Scopus connection test succeeded.")
        else:
            QMessageBox.warning(self, "Scopus Connection", f"Scopus connection test failed: {message}")
            self._append_log(f"Scopus connection test failed: {message}")

    def _test_openalex_connection(self) -> None:
        params = {"per-page": "1", "search": "test"}
        email = self.openalex_email_edit.text().strip()
        if email:
            params["mailto"] = email
        url = "https://api.openalex.org/works?" + urlparse.urlencode(params)
        self._append_log("GUI OpenAlex test endpoint: works search test")
        self._append_log("Pipeline OpenAlex smoke-test endpoint: works filter DOI")
        self._append_log("GUI and pipeline HTTP client: urllib.request")
        self._append_log("GUI OpenAlex test timeout seconds: 20; pipeline enrichment timeout seconds: 20")
        self._log_gui_http_settings("api.openalex.org works search")
        try:
            with self._gui_urlopen(url, timeout=20) as response:
                ok = 200 <= int(response.status) < 300
        except Exception as exc:
            ok = False
            message = f"{exc.__class__.__name__}: {exc}"
        else:
            message = "success" if ok else "unexpected response"
        if ok:
            QMessageBox.information(self, "OpenAlex Connection", "OpenAlex connection test succeeded.")
            self._append_log("OpenAlex connection test succeeded.")
        else:
            QMessageBox.warning(self, "OpenAlex Connection", f"OpenAlex connection test failed: {message}")
            self._append_log(f"OpenAlex connection test failed: {message}")

    def _test_api_ssl_connectivity(self) -> None:
        checks = [
            ("OpenAlex", "https://api.openalex.org/works?per-page=1"),
            ("Elsevier", "https://api.elsevier.com"),
            ("DOI.org", "https://doi.org"),
        ]
        failures: list[str] = []
        self._append_log("Testing API SSL/connectivity using pipeline-style HTTPS settings.")
        for label, url in checks:
            domain = urlparse.urlparse(url).netloc
            self._log_gui_http_settings(domain)
            try:
                with self._gui_urlopen(url, timeout=20) as response:
                    status = int(response.status)
                self._append_log(f"{label} SSL/connectivity test succeeded: HTTP {status}")
            except urlerror.HTTPError as exc:
                self._append_log(f"{label} SSL/connectivity test reached endpoint: HTTP {exc.code}")
            except Exception as exc:
                message = f"{label}: {exc.__class__.__name__}: {exc}"
                failures.append(message)
                self._append_log(f"{label} SSL/connectivity test failed: {exc.__class__.__name__}: {exc}")
        if failures:
            QMessageBox.warning(self, "API SSL/Connectivity", "Some API connectivity checks failed:\n\n" + "\n".join(failures))
        else:
            QMessageBox.information(self, "API SSL/Connectivity", "API SSL/connectivity checks succeeded.")

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
