"""Pannello dockable con la tabella editabile dei toponimi trovati.

L'utente può correggere il testo trascritto male, escludere righe con la
checkbox "includi", selezionare più righe (click, CTRL, Shift) per spuntarle
o deselezionarle in blocco, usare trova-e-sostituisci limitato a lettere,
numeri o entrambi, rilanciare Claude sull'intero quadrante di una o più righe
selezionate quando Tesseract ha letto male o ha perso dei toponimi, e infine
confermare per creare i punti nel GeoPackage.
"""

import re
import shutil

from qgis.PyQt.QtCore import Qt, pyqtSignal
from qgis.PyQt.QtWidgets import (
    QDockWidget,
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QTableWidget,
    QTableWidgetItem,
    QPushButton,
    QLabel,
    QCheckBox,
    QLineEdit,
    QRadioButton,
    QButtonGroup,
    QGroupBox,
    QAbstractItemView,
    QMessageBox,
    QComboBox,
)

from qgis.core import QgsDistanceArea, QgsMessageLog, QgsPointXY, QgsProject, Qgis

from ..core.i18n import tr
from ..core.plugin_version import get_plugin_version
from ..core import georeference
from ..core import deduplicator
from ..core.usage_tracker import default_model
from ..ocr import claude_engine

LOG_TAG = "Toponimi Extractor"


class _TileExtent:
    """Estensione geografica minimale di un tile, sufficiente per riusare
    core.georeference.word_center_to_map_xy senza dipendere da core.grid.Tile."""

    def __init__(self, xmin, ymin, xmax, ymax, width_px, height_px):
        self.xmin = xmin
        self.ymin = ymin
        self.xmax = xmax
        self.ymax = ymax
        self.width_px = width_px
        self.height_px = height_px

COL_INCLUDE = 0
COL_TOPONIMO = 1
COL_ENGINE = 2
COL_CONFIDENCE = 3
COL_X = 4
COL_Y = 5

def _headers():
    return [tr("Includi"), tr("Toponimo"), tr("Motore"), tr("Confidenza"), "X", "Y"]

DEDUP_DISTANCES_M = [5, 10, 20, 70]
DEDUP_DEFAULT_DISTANCE_M = 20


class ResultsDock(QDockWidget):
    confirmed = pyqtSignal(list)

    def __init__(self, parent=None):
        super().__init__(tr("Toponimi Extractor v{version} - Toponimi trovati").format(version=get_plugin_version()), parent)
        self.setAllowedAreas(Qt.DockWidgetArea.AllDockWidgetAreas)
        self._results = []
        self._ocr_settings = {}
        self._usage_tracker = None
        self._tile_cache_dir = None
        self._crs = None

        container = QWidget()
        layout = QVBoxLayout(container)

        self.info_label = QLabel(tr("Nessun risultato."))
        layout.addWidget(self.info_label)

        self.usage_label = QLabel("")
        self.usage_label.setWordWrap(True)
        self.usage_label.setStyleSheet("color: #555; font-size: 11px;")
        layout.addWidget(self.usage_label)

        # --- Selezione multipla: spunta/despunta in blocco ---
        selection_layout = QHBoxLayout()
        self.check_selected_btn = QPushButton(tr("Spunta selezionate"))
        self.check_selected_btn.clicked.connect(lambda: self._set_checked_for_selection(True))
        self.uncheck_selected_btn = QPushButton(tr("Deseleziona spunta"))
        self.uncheck_selected_btn.clicked.connect(lambda: self._set_checked_for_selection(False))
        selection_layout.addWidget(self.check_selected_btn)
        selection_layout.addWidget(self.uncheck_selected_btn)
        layout.addLayout(selection_layout)

        # --- Rilancio manuale di Claude sul quadrante di origine ---
        rerun_layout = QHBoxLayout()
        self.rerun_claude_btn = QPushButton(tr("Rilancia con Claude sul quadrante"))
        self.rerun_claude_btn.setToolTip(
            tr(
                "Per le righe selezionate, ripassa l'intero quadrante di origine a "
                "Claude (bypassando Tesseract): utile quando Tesseract ha letto male "
                "o ha perso toponimi (es. testo ruotato/curvo). I risultati trovati "
                "in più vengono aggiunti alla tabella, quelli già presenti aggiornati."
            )
        )
        self.rerun_claude_btn.clicked.connect(self._on_rerun_claude)
        rerun_layout.addWidget(self.rerun_claude_btn)
        layout.addLayout(rerun_layout)

        # --- Trova e sostituisci ---
        replace_group = QGroupBox(tr("Trova e sostituisci"))
        replace_layout = QVBoxLayout(replace_group)

        find_replace_row = QHBoxLayout()
        self.find_edit = QLineEdit()
        self.find_edit.setPlaceholderText(tr("Trova..."))
        self.replace_edit = QLineEdit()
        self.replace_edit.setPlaceholderText(tr("Sostituisci con..."))
        find_replace_row.addWidget(self.find_edit)
        find_replace_row.addWidget(self.replace_edit)
        replace_layout.addLayout(find_replace_row)

        scope_row = QHBoxLayout()
        self.radio_scope_both = QRadioButton(tr("Lettere e numeri"))
        self.radio_scope_letters = QRadioButton(tr("Solo lettere"))
        self.radio_scope_numbers = QRadioButton(tr("Solo numeri"))
        self.radio_scope_both.setChecked(True)
        self.scope_group = QButtonGroup(self)
        self.scope_group.addButton(self.radio_scope_both)
        self.scope_group.addButton(self.radio_scope_letters)
        self.scope_group.addButton(self.radio_scope_numbers)
        scope_row.addWidget(self.radio_scope_both)
        scope_row.addWidget(self.radio_scope_letters)
        scope_row.addWidget(self.radio_scope_numbers)
        replace_layout.addLayout(scope_row)

        apply_row = QHBoxLayout()
        self.replace_selected_only_checkbox = QCheckBox(tr("Solo righe selezionate"))
        self.replace_btn = QPushButton(tr("Sostituisci"))
        self.replace_btn.clicked.connect(self._on_replace)
        apply_row.addWidget(self.replace_selected_only_checkbox)
        apply_row.addWidget(self.replace_btn)
        replace_layout.addLayout(apply_row)

        layout.addWidget(replace_group)

        # --- Pulizia duplicati da overlap tra quadranti ---
        dedup_group = QGroupBox(tr("Rimuovi duplicati (stesso luogo letto due volte)"))
        dedup_layout = QVBoxLayout(dedup_group)
        dedup_hint = QLabel(
            tr(
                "I quadranti si sovrappongono lungo i bordi, quindi un nome sul bordo può essere letto "
                "due volte. Vengono proposti come duplicati solo i nomi uguali o molto simili, letti da "
                "due quadranti diversi nella loro zona comune, entro la distanza scelta. Due luoghi "
                "con lo stesso nome nello stesso quadrante non vengono mai uniti. Prima di rimuovere "
                "vedi l'elenco e confermi."
            )
        )
        dedup_hint.setWordWrap(True)
        dedup_hint.setStyleSheet("color: #555; font-size: 11px;")
        dedup_layout.addWidget(dedup_hint)

        dedup_row = QHBoxLayout()
        dedup_row.addWidget(QLabel(tr("Distanza massima:")))
        self.dedup_distance_combo = QComboBox()
        for meters in DEDUP_DISTANCES_M:
            self.dedup_distance_combo.addItem(f"{meters} m", meters)
        self.dedup_distance_combo.setCurrentIndex(DEDUP_DISTANCES_M.index(DEDUP_DEFAULT_DISTANCE_M))
        self.dedup_distance_combo.setToolTip(
            tr(
                "Due letture più lontane di così non vengono mai considerate lo stesso luogo. "
                "Parti da un valore basso e aumentalo solo se restano doppioni evidenti."
            )
        )
        dedup_row.addWidget(self.dedup_distance_combo)
        self.dedup_btn = QPushButton(tr("Cerca duplicati..."))
        self.dedup_btn.setToolTip(
            tr("Cerca i nomi letti due volte dal plugin e mostra l'elenco: nulla viene rimosso senza conferma.")
        )
        self.dedup_btn.clicked.connect(self._on_deduplicate)
        dedup_row.addWidget(self.dedup_btn)
        self.dedup_only_selected_checkbox = QCheckBox(tr("Solo righe selezionate"))
        self.dedup_only_selected_checkbox.setToolTip(
            tr(
                "Se spuntato, considera solo le righe attualmente selezionate nella tabella "
                "invece di tutto il risultato."
            )
        )
        dedup_row.addWidget(self.dedup_only_selected_checkbox)
        dedup_layout.addLayout(dedup_row)

        layout.addWidget(dedup_group)

        self.table = QTableWidget(0, len(_headers()))
        self.table.setHorizontalHeaderLabels(_headers())
        self.table.horizontalHeader().setStretchLastSection(False)
        self.table.setColumnWidth(COL_TOPONIMO, 200)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        layout.addWidget(self.table)

        button_layout = QHBoxLayout()
        self.confirm_btn = QPushButton(tr("OK - Crea punti nel GeoPackage"))
        self.confirm_btn.clicked.connect(self._on_confirm)
        button_layout.addWidget(self.confirm_btn)
        layout.addLayout(button_layout)

        self.setWidget(container)

    def set_ocr_context(self, ocr_settings, usage_tracker, tile_cache_dir):
        """Fornisce quanto serve per poter rilanciare Claude su un quadrante:
        le impostazioni OCR (per API key/modello), lo stesso UsageTracker
        dell'estrazione appena completata (per continuare a sommarci sopra
        chiamate/costi) e la cartella con le immagini dei tile già renderizzati."""
        self._ocr_settings = ocr_settings or {}
        self._usage_tracker = usage_tracker
        self._tile_cache_dir = tile_cache_dir

    def set_crs(self, crs):
        """CRS delle coordinate dei risultati: serve a misurare in metri le
        distanze tra i punti anche quando il CRS è in gradi."""
        self._crs = crs

    def _distance_in_meters_fn(self):
        """Funzione distanza(a, b) in metri tra due risultati. Per CRS in
        metri è la distanza piana; per CRS geografici è quella sull'ellissoide."""
        if self._crs is None or not self._crs.isGeographic():
            return None  # distanza euclidea nelle unità del CRS (metri)

        measure = QgsDistanceArea()
        measure.setSourceCrs(self._crs, QgsProject.instance().transformContext())
        measure.setEllipsoid(self._crs.ellipsoidAcronym() or "WGS84")
        return lambda a, b: measure.measureLine(QgsPointXY(a["x"], a["y"]), QgsPointXY(b["x"], b["y"]))

    def load_results(self, results, usage_summary_text=""):
        """results: lista di dict come prodotti da ExtractionTask.all_results
        (toponimo, x, y, fonte_nome_wms, fonte_indirizzo_wms, data_estrazione,
        source_engine, confidence, tile_row, tile_col, rotation_deg).
        usage_summary_text: riepilogo motori/costi (UsageTracker.summary_text())."""
        self._results = results
        self.table.setRowCount(len(results))
        self.usage_label.setText(usage_summary_text.replace("\n", " | "))

        for row, r in enumerate(results):
            include_checkbox = QCheckBox()
            # Preserva la spunta include/escludi se già presente sul dict
            # (es. dopo una deduplicazione, che ricarica la tabella da zero
            # ma non deve perdere le esclusioni manuali fatte dall'utente
            # prima di premere "Rimuovi duplicati").
            include_checkbox.setChecked(r.get("_included", True))
            self.table.setCellWidget(row, COL_INCLUDE, include_checkbox)

            text_item = QTableWidgetItem(r.get("toponimo", ""))
            text_item.setFlags(text_item.flags() | Qt.ItemFlag.ItemIsEditable)
            self.table.setItem(row, COL_TOPONIMO, text_item)

            engine_item = QTableWidgetItem(r.get("source_engine", ""))
            engine_item.setFlags(engine_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self.table.setItem(row, COL_ENGINE, engine_item)

            conf_item = QTableWidgetItem(f"{r.get('confidence', 0.0):.0f}")
            conf_item.setFlags(conf_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self.table.setItem(row, COL_CONFIDENCE, conf_item)

            x_item = QTableWidgetItem(f"{r.get('x', 0.0):.2f}")
            x_item.setFlags(x_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self.table.setItem(row, COL_X, x_item)

            y_item = QTableWidgetItem(f"{r.get('y', 0.0):.2f}")
            y_item.setFlags(y_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self.table.setItem(row, COL_Y, y_item)

        self.info_label.setText(
            tr("{count} toponimi trovati. Correggi il testo se necessario, poi conferma.").format(count=len(results))
        )

    def _selected_rows(self):
        return sorted({idx.row() for idx in self.table.selectedIndexes()})

    def _set_checked_for_selection(self, checked):
        rows = self._selected_rows()
        if not rows:
            rows = range(self.table.rowCount())
        for row in rows:
            checkbox = self.table.cellWidget(row, COL_INCLUDE)
            if checkbox is not None:
                checkbox.setChecked(checked)

    def _on_replace(self):
        find_text = self.find_edit.text()
        if not find_text:
            return
        replace_text = self.replace_edit.text()

        if self.radio_scope_letters.isChecked():
            scope_pattern = re.compile(r"[^\W\d_]", re.UNICODE)
        elif self.radio_scope_numbers.isChecked():
            scope_pattern = re.compile(r"\d")
        else:
            scope_pattern = None

        rows = self._selected_rows() if self.replace_selected_only_checkbox.isChecked() else range(
            self.table.rowCount()
        )

        for row in rows:
            item = self.table.item(row, COL_TOPONIMO)
            if item is None:
                continue
            original = item.text()
            new_text = self._replace_with_scope(original, find_text, replace_text, scope_pattern)
            if new_text != original:
                item.setText(new_text)

    @staticmethod
    def _replace_with_scope(text, find_text, replace_text, scope_pattern):
        """Sostituisce find_text con replace_text in `text`, applicando la
        sostituzione solo alle occorrenze i cui caratteri soddisfano
        `scope_pattern` (None = nessun vincolo, sostituisce ovunque)."""
        if scope_pattern is None:
            return text.replace(find_text, replace_text)

        result = []
        i = 0
        n = len(find_text)
        while i < len(text):
            chunk = text[i : i + n]
            if chunk == find_text and all(scope_pattern.match(ch) for ch in chunk):
                result.append(replace_text)
                i += n
            else:
                result.append(text[i])
                i += 1
        return "".join(result)

    def _sync_results_from_table(self):
        """Riporta su self._results le modifiche fatte a mano in tabella
        (testo corretto, spunta incluso/escluso) prima di un'operazione che
        rilegge self._results direttamente, come la deduplicazione."""
        for row, result in enumerate(self._results):
            text_item = self.table.item(row, COL_TOPONIMO)
            if text_item is not None:
                result["toponimo"] = text_item.text()
            include_checkbox = self.table.cellWidget(row, COL_INCLUDE)
            if include_checkbox is not None:
                result["_included"] = include_checkbox.isChecked()

    def _on_deduplicate(self):
        if not self._results:
            return

        self._sync_results_from_table()

        if self.dedup_only_selected_checkbox.isChecked():
            target_rows = set(self._selected_rows())
            if not target_rows:
                QMessageBox.information(
                    self, "Toponimi Extractor", tr("Seleziona prima una o più righe nella tabella.")
                )
                return
            scope_results = [r for i, r in enumerate(self._results) if i in target_rows]
            rest_results = [r for i, r in enumerate(self._results) if i not in target_rows]
        else:
            scope_results = list(self._results)
            rest_results = []

        max_distance = self.dedup_distance_combo.currentData()
        kept, removed_count, groups_report = deduplicator.deduplicate(
            scope_results, max_distance=max_distance, distance_fn=self._distance_in_meters_fn()
        )

        if removed_count == 0:
            QMessageBox.information(
                self,
                "Toponimi Extractor",
                tr(
                    "Nessun duplicato trovato entro {distance} m.\n\n"
                    "Vengono considerati solo nomi uguali o molto simili letti da due quadranti diversi "
                    "nella loro zona di sovrapposizione."
                ).format(distance=max_distance),
            )
            return

        from .report_dialog import ReportDialog

        confirm = ReportDialog(
            tr("Toponimi Extractor — duplicati trovati"),
            tr(
                "Trovati {groups} luoghi letti più volte entro {distance} m: "
                "{removed} righe verrebbero rimosse.\n"
                "Controlla l'elenco: se due voci sono in realtà luoghi distinti, annulla e riprova con una "
                "distanza più bassa, oppure escludi le righe a mano."
            ).format(groups=len(groups_report), distance=max_distance, removed=removed_count),
            "\n\n".join(groups_report),
            parent=self,
            default_filename="toponimi_extractor_duplicati.txt",
            accept_label=tr("Rimuovi {count} duplicati").format(count=removed_count),
        )
        if confirm.exec() != ReportDialog.DialogCode.Accepted:
            return

        self._results = rest_results + kept
        # L'ordine cambia (i duplicati scartati spariscono): ricostruiamo la
        # tabella da zero mantenendo lo stesso riepilogo utilizzo già mostrato.
        self.load_results(self._results, self.usage_label.text())

    def _on_rerun_claude(self):
        rows = self._selected_rows()
        if not rows:
            QMessageBox.information(
                self, "Toponimi Extractor", tr("Seleziona prima una o più righe nella tabella.")
            )
            return

        # La tabella viene ricostruita da self._results a fine rilancio: senza
        # questa sincronizzazione andrebbero perse correzioni e spunte manuali.
        self._sync_results_from_table()

        tile_paths = {}
        for row in rows:
            result = self._results[row]
            image_path = result.get("tile_image_path")
            if not image_path:
                continue
            tile_paths.setdefault(image_path, result)

        if not tile_paths:
            QMessageBox.warning(
                self,
                "Toponimi Extractor",
                tr(
                    "Le righe selezionate non hanno più l'immagine del quadrante disponibile "
                    "(sessione di estrazione troppo vecchia o già chiusa)."
                ),
            )
            return

        anthropic_env_var = self._ocr_settings.get("anthropic_api_key_env_var")
        claude_model = self._ocr_settings.get("claude_model") or default_model("claude")
        if not claude_engine.is_available(self._ocr_settings.get("anthropic_api_key"), anthropic_env_var):
            QMessageBox.warning(
                self,
                "Toponimi Extractor",
                tr(
                    "Claude non è disponibile: verifica nelle impostazioni che la libreria "
                    "'anthropic' sia installata e che la API key sia configurata."
                ),
            )
            return

        n_tiles = len(tile_paths)
        self.info_label.setText(tr("Rilancio Claude su {count} quadrante/i in corso...").format(count=n_tiles))
        self.rerun_claude_btn.setEnabled(False)
        try:
            for image_path, sample_result in tile_paths.items():
                self._rerun_claude_on_tile(image_path, sample_result, claude_model, anthropic_env_var)
        finally:
            self.rerun_claude_btn.setEnabled(True)

        self.load_results(self._results, self.usage_label.text())

    def _rerun_claude_on_tile(self, image_path, sample_result, claude_model, anthropic_env_var):
        tile = _TileExtent(
            xmin=sample_result["tile_xmin"],
            ymin=sample_result["tile_ymin"],
            xmax=sample_result["tile_xmax"],
            ymax=sample_result["tile_ymax"],
            width_px=sample_result["tile_width_px"],
            height_px=sample_result["tile_height_px"],
        )

        try:
            words, usage = claude_engine.recognize_words(
                image_path,
                tile.width_px,
                tile.height_px,
                api_key=self._ocr_settings.get("anthropic_api_key"),
                env_var=anthropic_env_var,
                model=claude_model,
            )
        except Exception as exc:
            QgsMessageLog.logMessage(
                f"Rilancio Claude fallito sul tile {image_path}: {exc}", LOG_TAG, Qgis.MessageLevel.Warning
            )
            QMessageBox.warning(
                self, "Toponimi Extractor", tr("Rilancio Claude fallito su un quadrante:\n{error}").format(error=exc)
            )
            return

        if self._usage_tracker is not None:
            self._usage_tracker.record_call(
                claude_engine.ENGINE_NAME,
                model_id=claude_model,
                input_tokens=usage.get("input_tokens", 0),
                output_tokens=usage.get("output_tokens", 0),
            )
            self._usage_tracker.record_tile_engine(claude_engine.ENGINE_NAME)

        # Sostituisce tutte le righe già presenti per questo tile con il nuovo
        # set completo trovato da Claude: evita di sommare duplicati fra il
        # vecchio risultato Tesseract e quello nuovo per lo stesso quadrante.
        kept_results = [r for r in self._results if r.get("tile_image_path") != image_path]

        new_results = []
        for word in words:
            x, y = georeference.word_center_to_map_xy(word, tile)
            new_results.append(
                {
                    "toponimo": word["text"],
                    "x": x,
                    "y": y,
                    "fonte_nome_wms": sample_result.get("fonte_nome_wms", ""),
                    "fonte_indirizzo_wms": sample_result.get("fonte_indirizzo_wms", ""),
                    "data_estrazione": sample_result.get("data_estrazione", ""),
                    "source_engine": claude_engine.ENGINE_NAME,
                    "confidence": word.get("confidence", 0.0),
                    "tile_row": sample_result.get("tile_row"),
                    "tile_col": sample_result.get("tile_col"),
                    "rotation_deg": word.get("rotation_deg", 0.0),
                    "tile_image_path": image_path,
                    "tile_width_px": tile.width_px,
                    "tile_height_px": tile.height_px,
                    "tile_xmin": tile.xmin,
                    "tile_ymin": tile.ymin,
                    "tile_xmax": tile.xmax,
                    "tile_ymax": tile.ymax,
                }
            )

        self._results = kept_results + new_results

    def cleanup_tile_cache(self):
        if self._tile_cache_dir:
            shutil.rmtree(self._tile_cache_dir, ignore_errors=True)
            self._tile_cache_dir = None

    def _on_confirm(self):
        confirmed_results = []
        for row in range(self.table.rowCount()):
            include_checkbox = self.table.cellWidget(row, COL_INCLUDE)
            if include_checkbox is not None and not include_checkbox.isChecked():
                continue

            edited_text = self.table.item(row, COL_TOPONIMO).text().strip()
            if not edited_text:
                continue

            result = dict(self._results[row])
            result["toponimo"] = edited_text
            confirmed_results.append(result)

        self.confirmed.emit(confirmed_results)
