"""Dialogo impostazioni: scelta motore OCR primario, soglia confidenza, provider cloud."""

import html
import os

from qgis.PyQt.QtCore import QSettings, Qt
from qgis.PyQt.QtWidgets import (
    QDialog,
    QVBoxLayout,
    QHBoxLayout,
    QFormLayout,
    QGroupBox,
    QRadioButton,
    QButtonGroup,
    QLineEdit,
    QDoubleSpinBox,
    QSpinBox,
    QComboBox,
    QLabel,
    QDialogButtonBox,
    QCheckBox,
    QTabWidget,
    QWidget,
    QPushButton,
    QFileDialog,
    QMessageBox,
    QApplication,
)

from ..core import dependency_check
from ..core.i18n import tr
from ..core.plugin_version import get_plugin_version
from ..core.usage_tracker import CLAUDE_MODELS, GPT_MODELS, default_model
from ..core.wms_capabilities import scale_to_meters_per_pixel
from ..ocr import tesseract_engine

SETTINGS_GROUP = "ToponimiExtractor"


class SettingsDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle(tr("Toponimi Extractor v{version} - Impostazioni").format(version=get_plugin_version()))
        self.setMinimumWidth(520)

        self._build_ui()
        self._load_settings()

    def _build_ui(self):
        layout = QVBoxLayout(self)

        # Una scheda per argomento, con gli stessi nomi delle sezioni della
        # guida: in un'unica colonna la finestra usciva dallo schermo.
        self.tabs = QTabWidget()
        layout.addWidget(self.tabs)

        engine_page = QWidget()
        engine_page_layout = QVBoxLayout(engine_page)
        cloud_page = QWidget()
        cloud_page_layout = QVBoxLayout(cloud_page)
        resolution_page = QWidget()
        resolution_page_layout = QVBoxLayout(resolution_page)
        grid_page = QWidget()
        grid_page_layout = QVBoxLayout(grid_page)

        # --- Motore OCR primario ---
        engine_group = QGroupBox(tr("Motore OCR primario"))
        engine_layout = QVBoxLayout(engine_group)
        self.radio_tesseract = QRadioButton(tr("Tesseract locale"))
        self.radio_cloud_only = QRadioButton(tr("Solo cloud (Claude/GPT)"))
        self.radio_tesseract.setChecked(True)
        self.engine_button_group = QButtonGroup(self)
        self.engine_button_group.addButton(self.radio_tesseract)
        self.engine_button_group.addButton(self.radio_cloud_only)
        engine_layout.addWidget(self.radio_tesseract)
        engine_layout.addWidget(self.radio_cloud_only)
        engine_page_layout.addWidget(engine_group)

        # --- Tesseract ---
        tesseract_group = QGroupBox("Tesseract")
        tesseract_form = QFormLayout(tesseract_group)
        self.tesseract_cmd_edit = QLineEdit()
        self.tesseract_cmd_edit.setPlaceholderText(
            r"C:\Program Files\Tesseract-OCR\tesseract.exe " + tr("(vuoto = PATH di sistema)")
        )
        browse_btn = QPushButton(tr("Sfoglia..."))
        browse_btn.clicked.connect(self._browse_tesseract)
        detect_btn = QPushButton(tr("Rileva automaticamente"))
        detect_btn.clicked.connect(self._detect_tesseract)
        path_row = QHBoxLayout()
        path_row.addWidget(self.tesseract_cmd_edit, 1)
        path_row.addWidget(browse_btn)
        path_row.addWidget(detect_btn)
        tesseract_form.addRow(tr("Percorso eseguibile:"), path_row)
        self.tesseract_lang_edit = QLineEdit("ita")
        tesseract_form.addRow(tr("Lingua (codice Tesseract):"), self.tesseract_lang_edit)
        test_btn = QPushButton(tr("Verifica Tesseract"))
        test_btn.clicked.connect(self._test_tesseract)
        self.tesseract_status_label = QLabel("")
        self.tesseract_status_label.setWordWrap(True)
        self.tesseract_status_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        status_row = QHBoxLayout()
        status_row.addWidget(test_btn)
        status_row.addWidget(self.tesseract_status_label, 1)
        tesseract_form.addRow("", status_row)
        tesseract_links = QLabel(
            tr("Tesseract non è installato? ")
            + f'<a href="{tesseract_engine.INSTALL_GUIDE_URL}">{tr("Istruzioni di installazione")}</a> · '
            + f'<a href="{tesseract_engine.PREBUILT_INSTALLERS_URL}">{tr("Installer già pronti")}</a>'
        )
        tesseract_links.setOpenExternalLinks(True)
        tesseract_links.setWordWrap(True)
        tesseract_form.addRow("", tesseract_links)
        engine_page_layout.addWidget(tesseract_group)
        engine_page_layout.addStretch(1)

        # --- Fallback cloud ---
        cloud_group = QGroupBox(tr("Fallback cloud"))
        cloud_layout = QVBoxLayout(cloud_group)

        self.use_cloud_checkbox = QCheckBox(tr("Abilita fallback cloud per testo a bassa confidenza"))
        self.use_cloud_checkbox.setChecked(True)
        cloud_layout.addWidget(self.use_cloud_checkbox)

        cloud_form = QFormLayout()
        self.provider_combo = QComboBox()
        self.provider_combo.addItem("Claude (Anthropic)", "claude")
        self.provider_combo.addItem("GPT (OpenAI)", "gpt")
        self.provider_combo.addItem(tr("Entrambi"), "both")
        cloud_form.addRow(tr("Provider:"), self.provider_combo)

        self.confidence_spin = QDoubleSpinBox()
        self.confidence_spin.setRange(0, 100)
        self.confidence_spin.setValue(60)
        self.confidence_spin.setSuffix(" %")
        cloud_form.addRow(tr("Soglia confidenza minima:"), self.confidence_spin)

        openai_env = tr("impostata") if os.environ.get("OPENAI_API_KEY") else tr("NON impostata")

        self.anthropic_env_var_combo = QComboBox()
        self.anthropic_env_var_combo.setEditable(True)
        for var_name in ("ANTHROPIC_API_KEY", "CLAUDE_API_KEY"):
            state = tr("impostata") if os.environ.get(var_name) else tr("NON impostata")
            self.anthropic_env_var_combo.addItem(f"{var_name} ({state})", var_name)
        cloud_form.addRow(tr("Variabile d'ambiente API key Claude:"), self.anthropic_env_var_combo)

        self.anthropic_key_edit = QLineEdit()
        self.anthropic_key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.anthropic_key_edit.setPlaceholderText(tr("Override manuale (vuoto = usa la variabile d'ambiente sopra)"))
        cloud_form.addRow(tr("Override API key Claude:"), self.anthropic_key_edit)

        self.openai_key_edit = QLineEdit()
        self.openai_key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.openai_key_edit.setPlaceholderText(tr("Da variabile d'ambiente OPENAI_API_KEY ({state})").format(state=openai_env))
        cloud_form.addRow(tr("Override API key GPT:"), self.openai_key_edit)

        self.claude_model_combo = QComboBox()
        for model_id, label, in_price, out_price in CLAUDE_MODELS:
            self.claude_model_combo.addItem(
                tr("{label} — ${in_price:.2f}/${out_price:.2f} per 1M token").format(
                    label=label, in_price=in_price, out_price=out_price
                ),
                model_id,
            )
        cloud_form.addRow(tr("Modello Claude:"), self.claude_model_combo)

        self.gpt_model_combo = QComboBox()
        for model_id, label, in_price, out_price in GPT_MODELS:
            self.gpt_model_combo.addItem(
                tr("{label} — ${in_price:.2f}/${out_price:.2f} per 1M token").format(
                    label=label, in_price=in_price, out_price=out_price
                ),
                model_id,
            )
        cloud_form.addRow(tr("Modello GPT:"), self.gpt_model_combo)

        cloud_layout.addLayout(cloud_form)
        cloud_page_layout.addWidget(cloud_group)
        cloud_page_layout.addStretch(1)

        # --- Griglia / quadranti ---
        grid_group = QGroupBox(tr("Quadranti"))
        grid_form = QFormLayout(grid_group)
        resolution_group = QGroupBox(tr("Risoluzione: quanto ingrandire la carta"))
        resolution_form = QFormLayout(resolution_group)

        self.max_tile_spin = QSpinBox()
        self.max_tile_spin.setRange(0, 16384)
        self.max_tile_spin.setValue(0)
        self.max_tile_spin.setSpecialValueText(tr("Automatico (max 1400, adatto all'OCR)"))
        grid_form.addRow(tr("Dimensione max tile (px):"), self.max_tile_spin)

        self.radio_resolution_auto = QRadioButton(tr("Automatica — lo zoom della mappa quando disegni l'area"))
        self.radio_resolution_scale = QRadioButton(tr("Scala (es. 1:2000)"))
        self.radio_resolution_manual = QRadioButton(tr("Risoluzione manuale"))
        self.radio_resolution_calibrate = QRadioButton(
            tr("Calibrazione automatica — prova alcune scale con Tesseract")
        )
        self.radio_resolution_ai_driven = QRadioButton(
            tr("AI DRIVEN — decide un agente AI (max 5 prove)")
        )
        self.radio_resolution_auto.setChecked(True)
        self.resolution_mode_group = QButtonGroup(self)
        self.resolution_mode_group.addButton(self.radio_resolution_auto)
        self.resolution_mode_group.addButton(self.radio_resolution_scale)
        self.resolution_mode_group.addButton(self.radio_resolution_manual)
        self.resolution_mode_group.addButton(self.radio_resolution_calibrate)
        self.resolution_mode_group.addButton(self.radio_resolution_ai_driven)
        resolution_form.addRow(self.radio_resolution_auto)
        resolution_form.addRow(self.radio_resolution_scale)

        self.scale_spin = QSpinBox()
        self.scale_spin.setRange(1, 10_000_000)
        self.scale_spin.setValue(2000)
        self.scale_spin.setPrefix("1 : ")
        self.scale_spin.setGroupSeparatorShown(True)
        self.scale_spin.valueChanged.connect(self._update_scale_hint)
        resolution_form.addRow(tr("Denominatore scala:"), self.scale_spin)

        self.scale_hint_label = QLabel("")
        self.scale_hint_label.setStyleSheet("color: #555; font-size: 11px;")
        resolution_form.addRow("", self.scale_hint_label)

        resolution_form.addRow(self.radio_resolution_manual)

        self.resolution_spin = QDoubleSpinBox()
        self.resolution_spin.setRange(0.0001, 100000)
        self.resolution_spin.setDecimals(4)
        self.resolution_spin.setValue(1.0)
        resolution_form.addRow(tr("Risoluzione (unità CRS/px):"), self.resolution_spin)

        resolution_form.addRow(self.radio_resolution_calibrate)

        self.calibrate_min_confidence_spin = QDoubleSpinBox()
        self.calibrate_min_confidence_spin.setRange(0, 100)
        self.calibrate_min_confidence_spin.setValue(50)
        self.calibrate_min_confidence_spin.setSuffix(" %")
        resolution_form.addRow(tr("Confidenza minima:"), self.calibrate_min_confidence_spin)

        self.calibrate_min_words_spin = QSpinBox()
        self.calibrate_min_words_spin.setRange(1, 100)
        self.calibrate_min_words_spin.setValue(1)
        resolution_form.addRow(tr("Parole minime nel test:"), self.calibrate_min_words_spin)

        resolution_form.addRow(self.radio_resolution_ai_driven)

        self.ai_agent_provider_combo = QComboBox()
        self.ai_agent_provider_combo.addItem("Claude (Anthropic)", "claude")
        self.ai_agent_provider_combo.addItem("GPT (OpenAI)", "gpt")
        resolution_form.addRow(tr("Agente decisionale:"), self.ai_agent_provider_combo)

        ai_driven_hint = QLabel(
            tr(
                "L'agente guarda un tile di prova al centro dell'area, legge il risultato OCR e decide "
                "da solo risoluzione/tile/overlap/motore, fino a 5 tentativi. Richiede una API key valida "
                "(Claude o GPT, a seconda della scelta sopra) già configurata nella sezione Fallback cloud. "
                "Viene proposta dopo ogni selezione dell'area, qualunque sia la modalità scelta qui."
            )
        )
        ai_driven_hint.setStyleSheet("color: #555; font-size: 11px;")
        ai_driven_hint.setWordWrap(True)
        resolution_form.addRow("", ai_driven_hint)

        self.radio_resolution_auto.toggled.connect(self._update_resolution_mode)
        self.radio_resolution_scale.toggled.connect(self._update_resolution_mode)
        self.radio_resolution_manual.toggled.connect(self._update_resolution_mode)
        self.radio_resolution_calibrate.toggled.connect(self._update_resolution_mode)
        self.radio_resolution_ai_driven.toggled.connect(self._update_resolution_mode)

        self.overlap_spin = QSpinBox()
        self.overlap_spin.setRange(0, 300)
        self.overlap_spin.setValue(200)
        grid_form.addRow(tr("Sovrapposizione tile (px):"), self.overlap_spin)

        resolution_page_layout.addWidget(resolution_group)
        resolution_page_layout.addStretch(1)

        grid_hint = QLabel(
            tr(
                "Un quadrante è un'immagine della carta inviata al motore di lettura. "
                "Lascia \"Automatico\" se non hai un motivo preciso per cambiarlo."
            )
        )
        grid_hint.setWordWrap(True)
        grid_hint.setStyleSheet("color: #555; font-size: 11px;")
        grid_page_layout.addWidget(grid_group)
        grid_page_layout.addWidget(grid_hint)
        grid_page_layout.addStretch(1)

        self.tabs.addTab(resolution_page, tr("Risoluzione"))
        self.tabs.addTab(engine_page, tr("Motore di lettura"))
        self.tabs.addTab(cloud_page, tr("Cloud e chiavi"))
        self.tabs.addTab(grid_page, tr("Quadranti"))

        # --- Requisiti ---
        req_page = QWidget()
        req_layout = QVBoxLayout(req_page)
        self.requirements_label = QLabel("")
        self.requirements_label.setWordWrap(True)
        self.requirements_label.setOpenExternalLinks(True)
        self.requirements_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextBrowserInteraction)
        req_layout.addWidget(self.requirements_label)
        req_buttons = QHBoxLayout()
        recheck_btn = QPushButton(tr("Ricontrolla"))
        recheck_btn.clicked.connect(self._refresh_requirements)
        self.install_packages_btn = QPushButton(tr("Installa librerie mancanti"))
        self.install_packages_btn.clicked.connect(self._install_missing_packages)
        req_buttons.addWidget(recheck_btn)
        req_buttons.addWidget(self.install_packages_btn)
        req_buttons.addStretch(1)
        req_layout.addLayout(req_buttons)
        req_layout.addStretch(1)
        self._requirements_tab_index = self.tabs.addTab(req_page, tr("Requisiti"))
        self.tabs.currentChanged.connect(self._on_tab_changed)

        self._update_resolution_mode()

        # --- Buttons ---
        button_box = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        button_box.accepted.connect(self.accept)
        button_box.rejected.connect(self.reject)
        layout.addWidget(button_box)

        self.accepted.connect(self._save_settings)

    def _on_tab_changed(self, index):
        if index == self._requirements_tab_index:
            self._refresh_requirements()

    def _refresh_requirements(self):
        """Controlla Tesseract, librerie Python e API key con i valori
        attualmente nel dialogo (anche se non ancora salvati)."""
        env_var = self.anthropic_env_var_combo.currentData() or self.anthropic_env_var_combo.currentText().split(" ")[0]
        items = dependency_check.requirements_report(
            tesseract_cmd=self.tesseract_cmd_edit.text().strip() or None,
            tesseract_lang=self.tesseract_lang_edit.text().strip(),
            claude_key=self.anthropic_key_edit.text().strip() or None,
            claude_env_var=env_var.strip() or None,
            openai_key=self.openai_key_edit.text().strip() or None,
        )
        icons = {dependency_check.OK: "✓", dependency_check.WARN: "⚠", dependency_check.MISSING: "✗"}
        rows = []
        for it in items:
            link = f' — <a href="{it["url"]}">{it["url_label"]}</a>' if it["url"] else ""
            rows.append(f'{icons[it["status"]]} <b>{html.escape(it["label"])}</b>: {html.escape(it["detail"])}{link}')
        self.requirements_label.setText("<br><br>".join(rows))
        self._missing_pip = [it["pip_name"] for it in items if it["pip_name"] and it["status"] == dependency_check.MISSING]
        self.install_packages_btn.setEnabled(bool(self._missing_pip))

    def _install_missing_packages(self):
        names = list(self._missing_pip)
        if not names:
            return
        answer = QMessageBox.question(
            self, "Toponimi Extractor",
            tr("Installare ora: {packages}?\n(richiede connessione internet)").format(packages=", ".join(names)),
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            ok, output = dependency_check.install_packages(names)
        finally:
            QApplication.restoreOverrideCursor()
        text = output + "\n\n" + tr("Riavvia QGIS perché le modifiche abbiano effetto.")
        (QMessageBox.information if ok else QMessageBox.warning)(self, "Toponimi Extractor", text)
        self._refresh_requirements()

    def _browse_tesseract(self):
        current = self.tesseract_cmd_edit.text().strip()
        start_dir = os.path.dirname(current) if current else (
            os.path.dirname(tesseract_engine.find_tesseract() or "") or os.path.expanduser("~")
        )
        path, _ = QFileDialog.getOpenFileName(
            self,
            tr("Seleziona l'eseguibile di Tesseract"),
            start_dir,
            "Tesseract (tesseract*);;" + tr("Tutti i file (*)"),
        )
        if path:
            self.tesseract_cmd_edit.setText(os.path.normpath(path))
            self._test_tesseract()

    def _detect_tesseract(self):
        path = tesseract_engine.find_tesseract()
        if not path:
            self.tesseract_status_label.setText(
                "✗ " + tr("Tesseract non trovato nel PATH né nelle cartelle abituali. Usa \"Sfoglia...\".")
            )
            return
        self.tesseract_cmd_edit.setText(os.path.normpath(path))
        self._test_tesseract()

    def _test_tesseract(self):
        """Prova il percorso indicato (o il PATH, se vuoto) e mostra versione
        e lingue installate; segnala le lingue richieste che mancano."""
        cmd = self.tesseract_cmd_edit.text().strip() or None
        try:
            info = tesseract_engine.probe(cmd)
        except tesseract_engine.TesseractNotAvailable as exc:
            self.tesseract_status_label.setText(
                "✗ " + tr("Tesseract non utilizzabile: {error}").format(error=exc)
            )
            return

        requested = [code for code in self.tesseract_lang_edit.text().strip().split("+") if code]
        missing = [code for code in requested if code not in info["languages"]]
        text = "✓ " + tr("{version} — lingue installate: {languages}").format(
            version=info["version"], languages=", ".join(info["languages"]) or "-"
        )
        if missing:
            text += "\n⚠ " + tr("Lingua non installata: {languages}").format(languages=", ".join(missing))
        self.tesseract_status_label.setText(text)

    def _update_resolution_mode(self):
        self.scale_spin.setEnabled(self.radio_resolution_scale.isChecked())
        self.resolution_spin.setEnabled(self.radio_resolution_manual.isChecked())
        calibrate = self.radio_resolution_calibrate.isChecked()
        self.calibrate_min_confidence_spin.setEnabled(calibrate)
        self.calibrate_min_words_spin.setEnabled(calibrate)
        self.ai_agent_provider_combo.setEnabled(self.radio_resolution_ai_driven.isChecked())
        self._update_scale_hint()

    def _update_scale_hint(self):
        meters_per_pixel = scale_to_meters_per_pixel(self.scale_spin.value())
        self.scale_hint_label.setText(
            tr("≈ {value:.4f} m/pixel (per CRS proiettati in metri; in gradi il calcolo non è valido)").format(
                value=meters_per_pixel
            )
        )

    def _load_settings(self):
        s = QSettings()
        s.beginGroup(SETTINGS_GROUP)

        use_tesseract = s.value("use_tesseract", True, type=bool)
        self.radio_tesseract.setChecked(use_tesseract)
        self.radio_cloud_only.setChecked(not use_tesseract)

        self.tesseract_cmd_edit.setText(s.value("tesseract_cmd", "", type=str))
        self.tesseract_lang_edit.setText(s.value("tesseract_lang", "ita", type=str))

        self.use_cloud_checkbox.setChecked(s.value("use_cloud_fallback", True, type=bool))
        provider = s.value("cloud_provider", "claude", type=str)
        idx = self.provider_combo.findData(provider)
        if idx >= 0:
            self.provider_combo.setCurrentIndex(idx)
        self.confidence_spin.setValue(s.value("confidence_threshold", 60.0, type=float))

        anthropic_env_var = s.value("anthropic_api_key_env_var", "ANTHROPIC_API_KEY", type=str)
        idx = self.anthropic_env_var_combo.findData(anthropic_env_var)
        if idx >= 0:
            self.anthropic_env_var_combo.setCurrentIndex(idx)
        else:
            self.anthropic_env_var_combo.setEditText(anthropic_env_var)

        self.anthropic_key_edit.setText(s.value("anthropic_api_key", "", type=str))
        self.openai_key_edit.setText(s.value("openai_api_key", "", type=str))

        claude_model = s.value("claude_model", default_model("claude"), type=str)
        idx = self.claude_model_combo.findData(claude_model)
        if idx >= 0:
            self.claude_model_combo.setCurrentIndex(idx)

        gpt_model = s.value("gpt_model", default_model("gpt"), type=str)
        idx = self.gpt_model_combo.findData(gpt_model)
        if idx >= 0:
            self.gpt_model_combo.setCurrentIndex(idx)

        self.max_tile_spin.setValue(s.value("max_tile_px", 0, type=int))

        resolution_mode = s.value("resolution_mode", "auto", type=str)
        if resolution_mode == "scale":
            self.radio_resolution_scale.setChecked(True)
        elif resolution_mode == "manual":
            self.radio_resolution_manual.setChecked(True)
        elif resolution_mode == "calibrate":
            self.radio_resolution_calibrate.setChecked(True)
        elif resolution_mode == "ai_driven":
            self.radio_resolution_ai_driven.setChecked(True)
        else:
            self.radio_resolution_auto.setChecked(True)

        self.scale_spin.setValue(s.value("scale_denominator", 2000, type=int))
        self.resolution_spin.setValue(s.value("resolution_override", 1.0, type=float) or 1.0)
        self.calibrate_min_confidence_spin.setValue(s.value("calibrate_min_confidence", 50.0, type=float))
        self.calibrate_min_words_spin.setValue(s.value("calibrate_min_words", 1, type=int))

        ai_agent_provider = s.value("ai_agent_provider", "claude", type=str)
        idx = self.ai_agent_provider_combo.findData(ai_agent_provider)
        if idx >= 0:
            self.ai_agent_provider_combo.setCurrentIndex(idx)

        self._update_resolution_mode()

        self.overlap_spin.setValue(s.value("overlap_px", 200, type=int))

        s.endGroup()

    def _save_settings(self):
        s = QSettings()
        s.beginGroup(SETTINGS_GROUP)

        s.setValue("use_tesseract", self.radio_tesseract.isChecked())
        s.setValue("tesseract_cmd", self.tesseract_cmd_edit.text().strip())
        s.setValue("tesseract_lang", self.tesseract_lang_edit.text().strip() or "ita")

        s.setValue("use_cloud_fallback", self.use_cloud_checkbox.isChecked())
        s.setValue("cloud_provider", self.provider_combo.currentData())
        s.setValue("confidence_threshold", self.confidence_spin.value())

        anthropic_env_var = self.anthropic_env_var_combo.currentText().strip()
        idx = self.anthropic_env_var_combo.currentIndex()
        if idx >= 0 and self.anthropic_env_var_combo.itemText(idx) == anthropic_env_var:
            anthropic_env_var = self.anthropic_env_var_combo.itemData(idx)
        s.setValue("anthropic_api_key_env_var", anthropic_env_var or "ANTHROPIC_API_KEY")

        s.setValue("anthropic_api_key", self.anthropic_key_edit.text().strip())
        s.setValue("openai_api_key", self.openai_key_edit.text().strip())

        s.setValue("claude_model", self.claude_model_combo.currentData())
        s.setValue("gpt_model", self.gpt_model_combo.currentData())

        s.setValue("max_tile_px", self.max_tile_spin.value())

        if self.radio_resolution_scale.isChecked():
            s.setValue("resolution_mode", "scale")
        elif self.radio_resolution_manual.isChecked():
            s.setValue("resolution_mode", "manual")
        elif self.radio_resolution_calibrate.isChecked():
            s.setValue("resolution_mode", "calibrate")
        elif self.radio_resolution_ai_driven.isChecked():
            s.setValue("resolution_mode", "ai_driven")
        else:
            s.setValue("resolution_mode", "auto")
        s.setValue("scale_denominator", self.scale_spin.value())
        s.setValue("resolution_override", self.resolution_spin.value())
        s.setValue("calibrate_min_confidence", self.calibrate_min_confidence_spin.value())
        s.setValue("calibrate_min_words", self.calibrate_min_words_spin.value())
        s.setValue("ai_agent_provider", self.ai_agent_provider_combo.currentData())

        s.setValue("overlap_px", self.overlap_spin.value())

        s.endGroup()


def load_ocr_settings():
    """Legge le impostazioni salvate e ritorna il dict pronto per ocr.cascade.run_cascade,
    più eventuali override per griglia/risoluzione.
    """
    s = QSettings()
    s.beginGroup(SETTINGS_GROUP)

    use_tesseract = s.value("use_tesseract", True, type=bool)
    use_cloud_fallback = s.value("use_cloud_fallback", True, type=bool)
    if not use_tesseract:
        # Modalità "solo cloud": il fallback cloud è l'unico motore, va sempre attivo.
        use_cloud_fallback = True

    resolution_mode = s.value("resolution_mode", "auto", type=str)
    if resolution_mode == "scale":
        scale_denominator = s.value("scale_denominator", 2000, type=int)
        meters_per_pixel_override = scale_to_meters_per_pixel(scale_denominator)
    elif resolution_mode == "manual":
        meters_per_pixel_override = s.value("resolution_override", 1.0, type=float) or None
    else:
        # "auto", "calibrate" e "ai_driven" non hanno un valore fisso qui: sono
        # risolti dal chiamante (serve l'AOI, non disponibile in questa funzione).
        meters_per_pixel_override = None

    result = {
        "ocr": {
            "use_tesseract": use_tesseract,
            "tesseract_lang": s.value("tesseract_lang", "ita", type=str),
            "tesseract_cmd": s.value("tesseract_cmd", "", type=str) or None,
            "use_cloud_fallback": use_cloud_fallback,
            "cloud_provider": s.value("cloud_provider", "claude", type=str),
            "confidence_threshold": s.value("confidence_threshold", 60.0, type=float),
            "anthropic_api_key": s.value("anthropic_api_key", "", type=str) or None,
            "anthropic_api_key_env_var": s.value("anthropic_api_key_env_var", "ANTHROPIC_API_KEY", type=str) or None,
            "openai_api_key": s.value("openai_api_key", "", type=str) or None,
            "claude_model": s.value("claude_model", default_model("claude"), type=str),
            "gpt_model": s.value("gpt_model", default_model("gpt"), type=str),
        },
        "max_tile_px_override": s.value("max_tile_px", 0, type=int) or None,
        "meters_per_pixel_override": meters_per_pixel_override,
        "overlap_px": s.value("overlap_px", 200, type=int),
        "resolution_mode": resolution_mode,
        "calibrate_min_confidence": s.value("calibrate_min_confidence", 50.0, type=float),
        "calibrate_min_words": s.value("calibrate_min_words", 1, type=int),
        "ai_agent_provider": s.value("ai_agent_provider", "claude", type=str),
    }

    s.endGroup()
    return result
