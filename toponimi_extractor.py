import os
import sys


def _ensure_user_site_packages_on_path():
    """Il file qgis-bin.env di QGIS sovrascrive PYTHONPATH all'avvio,
    ignorando qualsiasi valore impostato a livello di sistema/utente. Le
    librerie installate con 'pip install --target' nella cartella site-packages
    utente standard (%APPDATA%\\Python\\PythonXYZ\\site-packages) restano quindi
    invisibili a QGIS anche se presenti sul disco. Le aggiungiamo qui a
    sys.path esplicitamente, all'avvio del plugin.
    """
    appdata = os.environ.get("APPDATA")
    if not appdata:
        return
    version_tag = f"Python{sys.version_info.major}{sys.version_info.minor}"
    candidate = os.path.join(appdata, "Python", version_tag, "site-packages")
    if os.path.isdir(candidate) and candidate not in sys.path:
        sys.path.insert(0, candidate)


_ensure_user_site_packages_on_path()

from qgis.PyQt.QtCore import Qt, QUrl
from qgis.PyQt.QtCore import QSettings
from qgis.PyQt.QtWidgets import QAction, QFileDialog, QMessageBox, QStyle
from qgis.PyQt.QtGui import QDesktopServices, QIcon
from qgis.core import (
    QgsApplication,
    QgsCoordinateTransform,
    QgsCsException,
    QgsProject,
    QgsUnitTypes,
)

from .qgis_integration.map_tool import RectangleMapTool
from .qgis_integration import wms_layer_utils
from .core import grid as grid_mod
from .core.plugin_version import get_plugin_version
from .ui.settings_dialog import load_ocr_settings


class ToponimiExtractorPlugin:
    """Plugin principale: registra le azioni in QGIS e coordina i componenti."""

    def __init__(self, iface):
        self.iface = iface
        self.plugin_dir = os.path.dirname(__file__)
        self.actions = []
        self.menu = "&Toponimi Extractor"

        self.action_select_aoi = None
        self.action_run_batch = None
        self.action_settings = None
        self.action_cancel = None
        self.action_show_results = None
        self.action_more_tiles = None
        self.action_fewer_tiles = None
        self.action_about = None
        self.action_help = None
        self.action_export_text = None
        self.action_panel = None

        self.toolbar = None
        self.control_panel = None

        self.map_tool = None
        # AOI nel CRS del layer su cui è stata disegnata (non del progetto).
        self.last_aoi_extent = None
        self.last_layer_id = None
        # Risoluzione della mappa a video al momento del disegno, in unità
        # del CRS del layer per pixel: base della modalità "Automatica".
        self.last_canvas_units_per_pixel = None
        # Parametri trovati dalla ricerca automatica per l'area corrente
        # (None = non eseguita: l'estrazione usa le impostazioni).
        self.calibration_overrides = None
        self.preview_layer_id = None
        # Moltiplicatore dei quadranti rispetto alla griglia di base (1, 2, 4...).
        self.tile_budget_multiplier = 1
        self._preview_ok = False

        self.progress_dialog = None
        self.results_dock = None
        self.task = None
        self.calibration_task = None
        self._pending_layer = None
        self.output_layer_name = None
        self.output_gpkg_path = None
        self.output_append = False
        # Destinazione e area dell'estrazione i cui risultati sono in tabella:
        # restano quelle anche se nel frattempo se ne sceglie un'altra.
        self._pending_output = None
        self._pending_aoi = None

    def initGui(self):
        from .ui.control_panel import ControlPanelDock

        icon_path = os.path.join(self.plugin_dir, "icons", "icon.svg")
        plugin_icon = QIcon(icon_path)
        style = self.iface.mainWindow().style()
        std = QStyle.StandardPixmap

        self.action_select_aoi = self._make_action(
            self._theme_icon("/mActionSelectRectangle.svg", plugin_icon),
            "Seleziona area",
            "Disegna un rettangolo sulla mappa ed estrai i toponimi al suo interno",
            self.activate_aoi_tool,
        )
        self.action_run_batch = self._make_action(
            style.standardIcon(std.SP_MediaPlay),
            "Estrai toponimi",
            "Avvia l'estrazione dei toponimi sull'area selezionata",
            self._start_extraction,
            enabled=False,
        )
        self.action_cancel = self._make_action(
            style.standardIcon(std.SP_DialogCancelButton),
            "Annulla estrazione",
            "Interrompe l'estrazione in corso",
            self._cancel_extraction,
            enabled=False,
        )
        self.action_show_results = self._make_action(
            style.standardIcon(std.SP_FileDialogDetailedView),
            "Mostra risultati",
            "Porta in primo piano la tabella dei toponimi trovati",
            self.show_results_dock,
            enabled=False,
        )
        self.action_more_tiles = self._make_action(
            style.standardIcon(std.SP_ArrowUp),
            "Raddoppia quadranti (più dettaglio)",
            "Nel dubbio: concede il doppio dei quadranti all'area selezionata, usati per leggere "
            "a risoluzione più alta numeri e scritte piccole. L'anteprima si aggiorna.",
            lambda: self._change_tile_budget(2),
            enabled=False,
        )
        self.action_fewer_tiles = self._make_action(
            style.standardIcon(std.SP_ArrowDown),
            "Dimezza quadranti (meno dettaglio)",
            "Annulla un raddoppio dei quadranti.",
            lambda: self._change_tile_budget(0.5),
            enabled=False,
        )
        self.action_export_text = self._make_action(
            style.standardIcon(std.SP_DialogSaveButton),
            "Esporta testo per RAG",
            "Esporta i toponimi salvati nel GeoPackage di output come file di testo leggero, "
            "una riga per toponimo, pronto da indicizzare in una RAG",
            self.export_text_for_rag,
        )
        self.action_settings = self._make_action(
            style.standardIcon(std.SP_FileDialogContentsView),
            "Impostazioni",
            "Motore OCR, chiavi API, risoluzione e quadranti",
            self.open_settings_dialog,
        )
        self.action_about = self._make_action(
            style.standardIcon(std.SP_MessageBoxInformation),
            "Info versione",
            "Mostra la versione del plugin",
            self.show_version_info,
        )

        self.action_help = self._make_action(
            style.standardIcon(std.SP_DialogHelpButton),
            "Guida",
            "Apre la guida: come si usa il plugin e cosa fa ogni impostazione",
            self.open_help,
        )

        self.control_panel = ControlPanelDock(self.iface.mainWindow())
        self.iface.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, self.control_panel)
        self.action_panel = self.control_panel.toggleViewAction()
        self.action_panel.setIcon(plugin_icon)
        self.action_panel.setText("Pannello Toponimi")
        self.actions.append(self.action_panel)
        self.iface.addPluginToMenu(self.menu, self.action_panel)

        panel_actions = (
            self.action_select_aoi,
            self.action_run_batch,
            self.action_cancel,
            self.action_show_results,
            self.action_more_tiles,
            self.action_fewer_tiles,
            self.action_export_text,
            self.action_settings,
            self.action_help,
            self.action_about,
        )
        for action in panel_actions:
            self.control_panel.add_action_button(action)

        self.toolbar = self.iface.addToolBar("Toponimi Extractor")
        self.toolbar.setObjectName("ToponimiExtractorToolbar")
        self.toolbar.addAction(self.action_panel)
        self.toolbar.addSeparator()
        for action in panel_actions[:4]:
            self.toolbar.addAction(action)
        self.toolbar.addSeparator()
        self.toolbar.addAction(self.action_settings)

        self.iface.currentLayerChanged.connect(self._refresh_panel)
        self._refresh_panel()

        self._check_dependencies()

    def _make_action(self, icon, text, tooltip, slot, enabled=True):
        """Crea un'azione condivisa da barra, pannello e menu plugin."""
        action = QAction(icon, text, self.iface.mainWindow())
        action.setToolTip(tooltip)
        action.setStatusTip(tooltip)
        action.triggered.connect(slot)
        action.setEnabled(enabled)
        self.iface.addPluginToMenu(self.menu, action)
        self.actions.append(action)
        return action

    @staticmethod
    def _theme_icon(name, fallback):
        icon = QgsApplication.getThemeIcon(name)
        return fallback if icon.isNull() else icon

    def _set_running(self, running):
        """Abilita/disabilita le azioni in base alla presenza di un'estrazione attiva."""
        self.action_select_aoi.setEnabled(not running)
        self.action_run_batch.setEnabled(not running and self.last_aoi_extent is not None)
        self.action_cancel.setEnabled(running)
        self._refresh_panel()

    def _refresh_panel(self, *_):
        """Aggiorna stato e azioni dipendenti dai risultati nel pannello di controllo."""
        if self.control_panel is None:
            return

        layer = self.iface.activeLayer()
        if layer is None:
            layer_text = "nessuno"
        elif wms_layer_utils.is_wms_layer(layer):
            layer_text = layer.name()
        else:
            layer_text = f"{layer.name()} (non è un layer WMS)"

        if self.last_aoi_extent is None:
            area_text = "nessuna selezionata"
        else:
            area_text = f"{self.last_aoi_extent.width():.0f} × {self.last_aoi_extent.height():.0f} unità CRS"

        if self.output_gpkg_path:
            output_text = f"{self.output_layer_name} in {os.path.basename(self.output_gpkg_path)}"
        else:
            output_text = "da scegliere alla selezione dell'area"

        if self.task is not None:
            task_text = "Estrazione in corso..."
        elif self.calibration_task is not None:
            task_text = "Ricerca della modalità migliore in corso..."
        else:
            task_text = ""
        self.control_panel.set_status(layer_text, area_text, output_text, task_text)
        self.action_show_results.setEnabled(self.results_dock is not None)

        idle = self.task is None and self.calibration_task is None
        can_tune = idle and self.last_aoi_extent is not None and self._preview_ok
        self.action_more_tiles.setEnabled(can_tune)
        self.action_fewer_tiles.setEnabled(can_tune and self.tile_budget_multiplier > 1)

    def _change_tile_budget(self, factor):
        """Raddoppia o dimezza i quadranti concessi all'area corrente e
        ridisegna l'anteprima."""
        layer = QgsProject.instance().mapLayer(self.last_layer_id) if self.last_layer_id else None
        if not wms_layer_utils.is_wms_layer(layer) or self.task is not None or self.calibration_task is not None:
            return

        previous = self.tile_budget_multiplier
        self.tile_budget_multiplier = max(1, int(previous * factor))
        summary = self._show_grid_preview(layer)
        if not self._preview_ok:
            # Troppi quadranti: si torna alla griglia precedente (l'avviso è già stato mostrato).
            self.tile_budget_multiplier = previous
            summary = self._show_grid_preview(layer)
        self._refresh_panel()
        self.iface.messageBar().pushInfo("Toponimi Extractor", summary)

    def show_results_dock(self):
        if self.results_dock is not None:
            self.results_dock.show()
            self.results_dock.raise_()

    def _check_dependencies(self):
        from .core import dependency_check

        missing = dependency_check.missing_packages()
        if not missing:
            return

        choice = QMessageBox.question(
            self.iface.mainWindow(),
            "Toponimi Extractor",
            f"Alcune librerie Python opzionali non sono installate nell'ambiente di QGIS:\n\n"
            f"{', '.join(missing)}\n\n"
            f"Sono necessarie per l'OCR locale (Tesseract) e/o i motori cloud (Claude/GPT).\n"
            f"Vuoi installarle ora? (richiede connessione internet)",
            buttons=QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            defaultButton=QMessageBox.StandardButton.No,
        )
        if choice != QMessageBox.StandardButton.Yes:
            return

        all_ok, output = dependency_check.install_packages(missing)
        if all_ok:
            QMessageBox.information(
                self.iface.mainWindow(),
                "Toponimi Extractor",
                f"Librerie installate correttamente:\n\n{output}\n\n"
                f"Riavvia QGIS perché le modifiche abbiano effetto.",
            )
        else:
            QMessageBox.warning(
                self.iface.mainWindow(),
                "Toponimi Extractor",
                f"Installazione completata con alcuni errori:\n\n{output}\n\n"
                f"Se un pacchetto è indicato come 'FALLITO' perché già in uso da QGIS, "
                f"prova a chiudere e riaprire QGIS e ripetere: spesso il pacchetto risulta "
                f"comunque già disponibile.",
            )

    def unload(self):
        try:
            self.iface.currentLayerChanged.disconnect(self._refresh_panel)
        except (TypeError, RuntimeError):
            pass

        for action in self.actions:
            self.iface.removePluginMenu(self.menu, action)
        self.actions = []

        if self.toolbar is not None:
            self.iface.mainWindow().removeToolBar(self.toolbar)
            self.toolbar.deleteLater()
            self.toolbar = None

        if self.control_panel is not None:
            self.iface.removeDockWidget(self.control_panel)
            self.control_panel.deleteLater()
            self.control_panel = None

        self._deactivate_aoi_tool()
        self._remove_grid_preview()

        for running_task in (self.task, self.calibration_task):
            if running_task is not None:
                # Il task non deve richiamare un plugin ormai smontato.
                running_task.on_finished_callback = None
        self._cancel_extraction()
        self.task = None
        self.calibration_task = None

        if self.progress_dialog is not None:
            self.progress_dialog.close()
            self.progress_dialog = None

        self._close_results_dock()

    def _close_results_dock(self):
        """Chiude il dock dei risultati (se aperto) e libera la cache delle
        immagini dei tile usata per l'eventuale rilancio manuale di Claude."""
        if self.results_dock is None:
            return
        self.results_dock.cleanup_tile_cache()
        self.iface.removeDockWidget(self.results_dock)
        self.results_dock = None
        self._refresh_panel()

    def _deactivate_aoi_tool(self):
        """Stacca lo strumento di selezione dal canvas e ne libera il rubber
        band, cosi' dopo una selezione (o alla disattivazione del plugin) non
        resta ne' un rettangolo disegnato ne' un map tool agganciato in
        ascolto di eventi mouse."""
        if self.map_tool is None:
            return

        canvas = self.iface.mapCanvas()
        if canvas.mapTool() is self.map_tool:
            canvas.unsetMapTool(self.map_tool)
        self.map_tool.deactivate()
        self.map_tool = None

    def activate_aoi_tool(self):
        layer = self.iface.activeLayer()
        if not wms_layer_utils.is_wms_layer(layer):
            QMessageBox.warning(
                self.iface.mainWindow(),
                "Toponimi Extractor",
                "Seleziona prima, nel pannello Layer, un layer raster WMS attivo.",
            )
            return

        from .ui.output_target_dialog import OutputTargetDialog

        target_dialog = OutputTargetDialog(
            self.iface.mainWindow(),
            current_gpkg_path=self.output_gpkg_path,
            current_layer_name=self.output_layer_name,
        )
        if target_dialog.exec() != OutputTargetDialog.DialogCode.Accepted:
            return

        self.output_layer_name = target_dialog.layer_name
        self.output_gpkg_path = target_dialog.gpkg_path
        self.output_append = target_dialog.append_to_existing
        self._refresh_panel()

        self._deactivate_aoi_tool()
        canvas = self.iface.mapCanvas()
        self.map_tool = RectangleMapTool(canvas)
        self.map_tool.rectangleCreated.connect(self._on_rectangle_created)
        self.map_tool.canceled.connect(self._on_selection_canceled)
        canvas.setMapTool(self.map_tool)

        self.iface.messageBar().pushInfo(
            f"Toponimi Extractor v{get_plugin_version()}",
            "Disegna un rettangolo sulla mappa per selezionare l'area di interesse (Esc per annullare).",
        )

    def _on_selection_canceled(self):
        self._deactivate_aoi_tool()
        self.iface.messageBar().pushInfo("Toponimi Extractor", "Selezione dell'area annullata.")

    def _on_rectangle_created(self, rect):
        self._deactivate_aoi_tool()

        layer = self.iface.activeLayer()
        if not wms_layer_utils.is_wms_layer(layer):
            QMessageBox.warning(
                self.iface.mainWindow(),
                "Toponimi Extractor",
                "Il layer attivo non è più un layer raster WMS: selezionalo e ridisegna l'area.",
            )
            return

        # Il rettangolo è nel CRS della mappa; rendering, griglia e punti
        # salvati lavorano nel CRS del layer.
        canvas = self.iface.mapCanvas()
        canvas_crs = canvas.mapSettings().destinationCrs()
        layer_rect = rect
        if canvas_crs != layer.crs():
            transform = QgsCoordinateTransform(canvas_crs, layer.crs(), QgsProject.instance())
            try:
                layer_rect = transform.transformBoundingBox(rect)
            except QgsCsException as exc:
                QMessageBox.critical(
                    self.iface.mainWindow(),
                    "Toponimi Extractor",
                    f"Impossibile convertire l'area nel sistema di riferimento del layer "
                    f"({layer.crs().authid()}): {exc}",
                )
                return

        self.last_aoi_extent = layer_rect
        self.last_layer_id = layer.id()
        self.last_canvas_units_per_pixel = canvas.mapUnitsPerPixel() * layer_rect.width() / rect.width()
        self.calibration_overrides = None
        self.tile_budget_multiplier = 1
        self.action_run_batch.setEnabled(True)
        self._refresh_panel()

        choice = QMessageBox.question(
            self.iface.mainWindow(),
            "Toponimi Extractor",
            "Area selezionata.\n\nVuoi che trovi la modalità migliore per quest'area "
            "(risoluzione, quadranti e motore OCR)?\n\n"
            "Sì: alcune letture di prova con il modello AI, poi i parametri trovati vengono "
            "usati per l'estrazione.\nNo: l'estrazione userà le impostazioni correnti.",
            buttons=QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            defaultButton=QMessageBox.StandardButton.Yes,
        )
        if choice == QMessageBox.StandardButton.Yes:
            # In background: il flusso riprende in _on_calibration_finished.
            self._start_best_mode_search(layer)
        else:
            self._finish_selection(layer)

    def _finish_selection(self, layer):
        """Ultimo passo dopo la selezione: anteprima dei quadranti e invito
        a lanciare l'estrazione."""
        summary = self._show_grid_preview(layer)
        self.iface.messageBar().pushSuccess(
            "Toponimi Extractor",
            f"{summary} Controlla l'anteprima sulla mappa, poi premi 'Estrai toponimi'. "
            f"Nel dubbio su numeri o scritte piccole, 'Raddoppia quadranti' aumenta il dettaglio.",
        )
        self._refresh_panel()

    def _start_best_mode_search(self, layer):
        """Avvia in background la ricerca automatica dei parametri, con una
        finestra che mostra i passi e il tempo trascorso."""
        settings = load_ocr_settings()
        try:
            settings["wms_params"] = wms_layer_utils.get_wms_params(layer)
        except wms_layer_utils.NotAWmsLayerError as exc:
            QMessageBox.critical(self.iface.mainWindow(), "Toponimi Extractor", str(exc))
            self._finish_selection(layer)
            return

        from .tasks.calibration_task import CalibrationTask
        from .ui.progress_dialog import ProgressDialog

        self.progress_dialog = ProgressDialog(
            self.iface.mainWindow(), title="Ricerca della modalità migliore", indeterminate=True
        )
        self.progress_dialog.cancel_btn.clicked.connect(self._cancel_extraction)

        self.calibration_task = CalibrationTask(
            layer, self.last_aoi_extent, settings, on_finished=self._on_calibration_finished
        )
        self.calibration_task.stepChanged.connect(self.progress_dialog.add_step)

        self.progress_dialog.show()
        QgsApplication.taskManager().addTask(self.calibration_task)
        self._set_running(True)

    def _on_calibration_finished(self, task):
        from .core import ai_agent_calibrator

        self.calibration_task = None
        self._set_running(False)
        if self.progress_dialog is not None:
            self.progress_dialog.close()
            self.progress_dialog = None

        if task.exception is not None:
            if isinstance(task.exception, ai_agent_calibrator.AiCalibrationError):
                provider = task.settings.get("ai_agent_provider", "claude")
                message = (
                    f"Ricerca della modalità migliore non avviabile: {task.exception}\n\n"
                    f"Verifica di aver configurato una API key valida per il provider scelto "
                    f"({provider}) nella sezione 'Fallback cloud' delle impostazioni."
                )
            else:
                message = f"Ricerca della modalità migliore fallita: {task.exception}"
            QMessageBox.critical(
                self.iface.mainWindow(),
                "Toponimi Extractor",
                message + "\n\nL'estrazione userà le impostazioni correnti.",
            )
        elif task.result is None:
            self.iface.messageBar().pushInfo(
                "Toponimi Extractor", "Ricerca annullata: l'estrazione userà le impostazioni correnti."
            )
        else:
            self._show_calibration_report(task.result)
            self.calibration_overrides = self._overrides_from_calibration(task.result)

        layer = QgsProject.instance().mapLayer(self.last_layer_id) if self.last_layer_id else None
        if wms_layer_utils.is_wms_layer(layer):
            self._finish_selection(layer)

    @staticmethod
    def _overrides_from_calibration(result):
        """Parametri da applicare all'estrazione. Il motore scelto dall'agente
        diventa il motore primario per tutta l'estrazione (è quello validato
        sul tile di prova); con Tesseract il fallback cloud resta quello
        delle impostazioni, come rete di sicurezza."""
        if result.engine == "tesseract":
            ocr = {"use_tesseract": True}
        else:
            ocr = {"use_tesseract": False, "use_cloud_fallback": True, "cloud_provider": result.engine}
        return {
            "meters_per_pixel_override": result.meters_per_pixel,
            "max_tile_px_override": result.tile_size_px,
            "overlap_px": result.overlap_px,
            "ocr": ocr,
        }

    def _build_extraction_settings(self, layer):
        """Impostazioni complete per estrarre dall'area corrente: quelle
        salvate, più i parametri del layer e quelli eventualmente trovati
        dalla ricerca automatica. Ritorna None (dopo aver avvisato) se il
        layer non è utilizzabile."""
        try:
            wms_params = wms_layer_utils.get_wms_params(layer)
        except wms_layer_utils.NotAWmsLayerError as exc:
            QMessageBox.critical(self.iface.mainWindow(), "Toponimi Extractor", str(exc))
            return None

        settings = load_ocr_settings()
        settings["wms_params"] = wms_params
        settings["canvas_units_per_pixel"] = self.last_canvas_units_per_pixel
        settings["crs_is_metric"] = layer.crs().mapUnits() == QgsUnitTypes.DistanceMeters
        settings["tile_budget_multiplier"] = self.tile_budget_multiplier

        if self.calibration_overrides is not None:
            # Parametri già trovati per quest'area dopo la selezione.
            overrides = dict(self.calibration_overrides)
            settings["ocr"].update(overrides.pop("ocr"))
            settings.update(overrides)
        # "ai_driven" senza parametri trovati: la ricerca è stata rifiutata o è
        # fallita dopo la selezione, quindi vale la risoluzione automatica.
        return settings

    def _remove_grid_preview(self):
        if self.preview_layer_id is not None:
            if QgsProject.instance().mapLayer(self.preview_layer_id) is not None:
                QgsProject.instance().removeMapLayer(self.preview_layer_id)
            self.preview_layer_id = None

    def _show_grid_preview(self, layer):
        """Disegna sulla mappa l'area e i quadranti che l'estrazione userà.
        Ritorna il testo di riepilogo da mostrare all'utente."""
        from .qgis_integration import grid_preview
        from .tasks.extraction_task import plan_grid

        self._remove_grid_preview()
        self._preview_ok = False

        settings = self._build_extraction_settings(layer)
        if settings is None:
            return "Area pronta."

        tiles = []
        if self.calibration_overrides is None and settings.get("resolution_mode") == "calibrate":
            summary = "Area pronta: i quadranti saranno definiti dalla calibrazione della scala, all'avvio."
        else:
            try:
                tiles, meters_per_pixel, max_tile_px = plan_grid(self.last_aoi_extent, settings)
                summary = (
                    f"Area pronta: {len(tiles)} quadranti, risoluzione {meters_per_pixel:.4g} unità CRS/px, "
                    f"lato massimo {max_tile_px} px."
                )
                if self.tile_budget_multiplier > 1:
                    summary += f" Quadranti ×{self.tile_budget_multiplier} rispetto alla griglia di base."
                self._preview_ok = True
            except ValueError as exc:
                QMessageBox.warning(self.iface.mainWindow(), "Toponimi Extractor", str(exc))
                summary = "Area selezionata, ma con le impostazioni correnti l'estrazione non può partire."

        preview = grid_preview.build_preview_layer(self.last_aoi_extent, tiles, layer.crs())
        QgsProject.instance().addMapLayer(preview)
        self.preview_layer_id = preview.id()
        # Aggiungere un layer lo rende attivo: il layer attivo deve restare
        # quello WMS, altrimenti la prossima "Seleziona area" verrebbe rifiutata.
        self.iface.setActiveLayer(layer)
        return summary

    def _start_extraction(self):
        if self.last_aoi_extent is None:
            QMessageBox.warning(
                self.iface.mainWindow(),
                "Toponimi Extractor",
                "Seleziona prima un'area con lo strumento 'Seleziona area'.",
            )
            return

        # Sempre il layer su cui è stata disegnata l'area (l'AOI è nel suo
        # CRS), non quello attivo in questo momento.
        layer = QgsProject.instance().mapLayer(self.last_layer_id)
        if not wms_layer_utils.is_wms_layer(layer):
            QMessageBox.warning(
                self.iface.mainWindow(),
                "Toponimi Extractor",
                "Il layer WMS su cui era stata disegnata l'area non è più nel progetto: "
                "seleziona di nuovo l'area con 'Seleziona area'.",
            )
            return

        settings = self._build_extraction_settings(layer)
        if settings is None:
            return

        if self.calibration_overrides is None and settings.get("resolution_mode") == "calibrate":
            calibrated_mpp = self._run_scale_calibration(layer, settings)
            if calibrated_mpp is None:
                return
            settings["meters_per_pixel_override"] = calibrated_mpp

        from .tasks.extraction_task import ExtractionTask
        from .ui.progress_dialog import ProgressDialog

        self.progress_dialog = ProgressDialog(self.iface.mainWindow(), title="Estrazione in corso")
        self.progress_dialog.cancel_btn.clicked.connect(self._cancel_extraction)

        self.task = ExtractionTask(layer, self.last_aoi_extent, settings, on_finished=self._on_task_finished)
        self.task.output_target = (self.output_gpkg_path, self.output_layer_name, self.output_append)
        self.task.progressChanged.connect(self.progress_dialog.set_progress)
        self.task.stepChanged.connect(self.progress_dialog.add_step)

        self.progress_dialog.show()
        QgsApplication.taskManager().addTask(self.task)
        self._set_running(True)

    def _run_scale_calibration(self, layer, settings):
        """Esegue la calibrazione automatica della scala (sincrona: pochi tile
        piccoli, solo Tesseract locale, dura pochi secondi). Ritorna il valore
        di meters_per_pixel scelto, o None se annullata/fallita."""
        import tempfile
        import shutil
        from .core import scale_calibrator

        temp_dir = tempfile.mkdtemp(prefix="toponimi_extractor_calib_")
        try:
            try:
                accepted, all_results = scale_calibrator.calibrate_scale(
                    layer,
                    self.last_aoi_extent,
                    temp_dir,
                    tesseract_lang=settings["ocr"].get("tesseract_lang", "ita"),
                    tesseract_cmd=settings["ocr"].get("tesseract_cmd"),
                    min_confidence=settings.get("calibrate_min_confidence", 50.0),
                    min_words=settings.get("calibrate_min_words", 1),
                )
            except Exception as exc:
                QMessageBox.critical(
                    self.iface.mainWindow(),
                    "Toponimi Extractor",
                    f"Calibrazione scala fallita: {exc}",
                )
                return None
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

        report = "\n".join(str(r) for r in all_results)

        if accepted is None:
            best = max(all_results, key=lambda r: r.avg_confidence) if all_results else None
            if best is None:
                QMessageBox.warning(
                    self.iface.mainWindow(),
                    "Toponimi Extractor",
                    "Calibrazione fallita: nessuna scala testata.",
                )
                return None
            choice = QMessageBox.warning(
                self.iface.mainWindow(),
                "Toponimi Extractor",
                f"Nessuna scala testata soddisfa i criteri richiesti:\n\n{report}\n\n"
                f"Vuoi usare comunque la migliore trovata ({best}) o annullare?",
                buttons=QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                defaultButton=QMessageBox.StandardButton.Cancel,
            )
            if choice != QMessageBox.StandardButton.Yes:
                return None
            return best.meters_per_pixel

        QMessageBox.information(
            self.iface.mainWindow(),
            "Toponimi Extractor",
            f"Calibrazione completata. Scala scelta: 1:{accepted.scale_denominator} "
            f"({accepted.meters_per_pixel:.4f} m/px)\n\nDettaglio test:\n{report}",
        )
        return accepted.meters_per_pixel

    def _show_calibration_report(self, result):
        from .core import ai_agent_calibrator
        from .ui.report_dialog import ReportDialog

        estimated_tiles = grid_mod.estimate_tile_count(
            self.last_aoi_extent, result.meters_per_pixel, result.tile_size_px, result.overlap_px
        )
        summary = (
            f"L'agente AI ha scelto la configurazione per l'estrazione:\n"
            f"• Risoluzione: {result.meters_per_pixel:.4f} unità CRS/px\n"
            f"• Dimensione tile: {result.tile_size_px} px — Overlap: {result.overlap_px} px\n"
            f"• Motore OCR: {result.engine}\n"
            f"• Quadranti stimati sull'area: {estimated_tiles}\n"
            f"• Tentativi effettuati: {len(result.attempts)}/{ai_agent_calibrator.MAX_ITERATIONS}"
        )
        details = (
            "RAGIONAMENTO DELL'AGENTE\n\n" + "\n\n".join(result.reasoning_log)
            + "\n\n\nDETTAGLIO TENTATIVI\n\n" + result.report_text()
        )
        ReportDialog(
            "Toponimi Extractor — modalità migliore trovata",
            summary,
            details,
            parent=self.iface.mainWindow(),
            default_filename="toponimi_extractor_ragionamento_agente.txt",
        ).exec()

    def _cancel_extraction(self):
        """Annulla l'operazione in corso, estrazione o ricerca della modalità."""
        for running_task in (self.task, self.calibration_task):
            if running_task is None:
                continue
            try:
                if not running_task.isCanceled():
                    running_task.cancel()
            except RuntimeError:
                pass
        if self.progress_dialog is not None and (self.task is not None or self.calibration_task is not None):
            self.progress_dialog.add_step("Annullamento richiesto: attendo la fine del passo in corso...")

    def _on_task_finished(self, success, task):
        self.task = None
        self._set_running(False)

        if self.progress_dialog is not None:
            self.progress_dialog.close()
            self.progress_dialog = None

        import shutil

        if task.exception is not None:
            QMessageBox.critical(self.iface.mainWindow(), "Toponimi Extractor", str(task.exception))
            return

        if not success:
            # Annullata dall'utente: non è un errore.
            if not task.all_results:
                self.iface.messageBar().pushInfo("Toponimi Extractor", "Estrazione annullata.")
                return
            choice = QMessageBox.question(
                self.iface.mainWindow(),
                "Toponimi Extractor",
                f"Estrazione annullata dopo {task.tiles_processed} quadranti su {task.tiles_total}.\n\n"
                f"Vuoi rivedere i {len(task.all_results)} toponimi trovati finora?",
                buttons=QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                defaultButton=QMessageBox.StandardButton.Yes,
            )
            if choice != QMessageBox.StandardButton.Yes:
                shutil.rmtree(task.tile_cache_dir, ignore_errors=True)
                return

        if not task.all_results:
            QMessageBox.information(
                self.iface.mainWindow(),
                "Toponimi Extractor",
                "Nessun toponimo trovato nell'area selezionata.",
            )
            shutil.rmtree(task.tile_cache_dir, ignore_errors=True)
            return

        from .ui.results_dock import ResultsDock

        self._close_results_dock()

        self.results_dock = ResultsDock(self.iface.mainWindow())
        self.results_dock.confirmed.connect(self._on_results_confirmed)
        self.iface.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self.results_dock)
        self.results_dock.set_ocr_context(task.settings.get("ocr", {}), task.usage_tracker, task.tile_cache_dir)
        self.results_dock.set_crs(task.layer.crs())
        self.results_dock.load_results(task.all_results, task.usage_tracker.summary_text())
        self.results_dock.show()
        self._pending_layer = task.layer
        self._pending_output = task.output_target
        self._pending_aoi = task.aoi_extent
        self._refresh_panel()

    def _on_results_confirmed(self, confirmed_results):
        if not confirmed_results:
            QMessageBox.information(
                self.iface.mainWindow(),
                "Toponimi Extractor",
                "Nessun toponimo selezionato: nessun punto creato.",
            )
            return

        output_path, layer_name, append_chosen = self._pending_output
        aoi_extent = self._pending_aoi

        from .core import geopackage_writer
        from qgis.core import QgsVectorLayer

        overwrite = False
        if geopackage_writer.layer_exists(output_path, layer_name):
            uri = f"{output_path}|layername={layer_name}"
            existing_layer = QgsVectorLayer(uri, layer_name, "ogr")
            compatible = existing_layer.isValid() and geopackage_writer.schema_is_compatible(existing_layer)

            if not compatible:
                choice = self._ask_choice(
                    f"Il layer \"{layer_name}\" esiste già in:\n{output_path}\n\n"
                    f"ma non ha i campi di un layer di toponimi di questo plugin, quindi non ci si "
                    f"possono aggiungere punti.\n\nSovrascrivendolo, TUTTI i suoi dati attuali vanno persi.",
                    ["Sovrascrivi e perdi i dati attuali"],
                )
                if choice is None:
                    return
                overwrite = True
            elif not append_chosen:
                # Alla scelta della destinazione il layer non c'era: è comparso dopo.
                choice = self._ask_choice(
                    f"Il layer \"{layer_name}\" ora esiste già in:\n{output_path}\n"
                    f"con {existing_layer.featureCount()} toponimi.\n\nCosa vuoi fare dei nuovi toponimi?",
                    ["Aggiungi a quelli presenti", "Sovrascrivi e perdi i dati attuali"],
                )
                if choice is None:
                    return
                overwrite = choice != "Aggiungi a quelli presenti"
            # altrimenti: l'utente ha scelto esplicitamente di aggiungere a questo layer

        layer = self._pending_layer
        source_crs = layer.crs() if layer is not None else None
        crs_authid = source_crs.authid() if source_crs is not None else "EPSG:4326"
        first = confirmed_results[0]
        aoi_name = geopackage_writer.aoi_layer_name(layer_name)
        try:
            vector_layer = geopackage_writer.open_or_create_layer(
                output_path, crs_authid, layer_name=layer_name, overwrite=overwrite
            )
            count = geopackage_writer.append_results(vector_layer, confirmed_results, source_crs=source_crs)

            # Se va creato, il layer delle aree nasce nello stesso sistema di
            # riferimento del layer dei punti a cui si accompagna.
            aoi_layer = geopackage_writer.open_or_create_aoi_layer(
                output_path, vector_layer.crs().authid() or crs_authid, aoi_name, overwrite=overwrite
            )
            geopackage_writer.append_aoi_extent(
                aoi_layer,
                aoi_extent,
                fonte_nome_wms=first.get("fonte_nome_wms", ""),
                fonte_indirizzo_wms=first.get("fonte_indirizzo_wms", ""),
                data_estrazione=first.get("data_estrazione", ""),
                toponimi_trovati=count,
                source_crs=source_crs,
            )
        except IOError as exc:
            # Il dock dei risultati resta aperto: l'utente può riprovare.
            QMessageBox.critical(
                self.iface.mainWindow(),
                "Toponimi Extractor",
                f"Salvataggio non riuscito:\n{exc}\n\n"
                f"Verifica che il GeoPackage non sia aperto in modifica o bloccato da un altro programma.",
            )
            return
        geopackage_writer.apply_aoi_style(aoi_layer)
        # Ordine di caricamento: prima il poligono, poi i punti, così i punti
        # finiscono sopra nel pannello dei livelli.
        geopackage_writer.add_layer_to_project(aoi_layer)
        geopackage_writer.add_layer_to_project(vector_layer)

        self._remove_grid_preview()
        self._close_results_dock()

        QMessageBox.information(
            self.iface.mainWindow(),
            "Toponimi Extractor",
            f"{count} punti salvati in:\n{output_path} (layer '{layer_name}', "
            f"ora con {vector_layer.featureCount()} toponimi in totale)\n\n"
            f"Area di estrazione salvata nel layer '{aoi_name}'.",
        )

    def _ask_choice(self, text, labels):
        """Domanda con pulsanti dal testo esplicito (mai Sì/No su un'azione
        che cancella dati) più "Annulla", che è il predefinito. Ritorna
        l'etichetta scelta, o None se annullato."""
        box = QMessageBox(self.iface.mainWindow())
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle("Toponimi Extractor")
        box.setText(text)
        added = [(box.addButton(label, QMessageBox.ButtonRole.ActionRole), label) for label in labels]
        cancel_button = box.addButton(QMessageBox.StandardButton.Cancel)
        box.setDefaultButton(cancel_button)
        box.setEscapeButton(cancel_button)
        box.exec()
        clicked = box.clickedButton()
        for button, label in added:
            if clicked is button or clicked == button:
                return label
        return None

    def open_settings_dialog(self):
        from .ui.settings_dialog import SettingsDialog

        dialog = SettingsDialog(self.iface.mainWindow())
        dialog.exec()

    def export_text_for_rag(self):
        """Esporta il GeoPackage di output corrente (o l'ultimo usato) chiedendo
        solo dove salvare il testo. Il GeoPackage viene chiesto soltanto se non
        ce n'è nessuno noto."""
        from .core import rag_export
        from .ui.settings_dialog import SETTINGS_GROUP

        s = QSettings()
        s.beginGroup(SETTINGS_GROUP)
        last_dir = s.value("last_output_dir", os.path.expanduser("~"), type=str)
        last_gpkg = s.value("last_output_gpkg", "", type=str)
        s.endGroup()

        gpkg_path = next(
            (path for path in (self.output_gpkg_path, last_gpkg) if path and os.path.isfile(path)), None
        )
        if not gpkg_path:
            gpkg_path, _ = QFileDialog.getOpenFileName(
                self.iface.mainWindow(),
                "Nessun GeoPackage di toponimi noto: scegli quello da esportare",
                last_dir,
                "GeoPackage (*.gpkg)",
            )
            if not gpkg_path:
                return

        output_path, _ = QFileDialog.getSaveFileName(
            self.iface.mainWindow(),
            "Salva il testo per la RAG",
            os.path.splitext(gpkg_path)[0] + "_toponimi.md",
            "Markdown (*.md);;Testo (*.txt)",
        )
        if not output_path:
            return

        title = f"Toponimi estratti — {os.path.splitext(os.path.basename(gpkg_path))[0]}"
        try:
            layer_count, total = rag_export.export_geopackage_to_text(gpkg_path, output_path, title)
        except (ValueError, OSError) as exc:
            QMessageBox.warning(self.iface.mainWindow(), "Toponimi Extractor", f"Esportazione non riuscita:\n{exc}")
            return

        QMessageBox.information(
            self.iface.mainWindow(),
            "Toponimi Extractor",
            f"Esportati {total} toponimi da {layer_count} layer in:\n{output_path}",
        )

    def open_help(self):
        """Apre nel browser la guida HTML distribuita con il plugin."""
        help_path = os.path.join(self.plugin_dir, "docs", "guida.html")
        if not os.path.isfile(help_path) or not QDesktopServices.openUrl(QUrl.fromLocalFile(help_path)):
            QMessageBox.warning(
                self.iface.mainWindow(),
                "Toponimi Extractor",
                f"Impossibile aprire la guida:\n{help_path}",
            )

    def show_version_info(self):
        QMessageBox.information(
            self.iface.mainWindow(),
            "Toponimi Extractor",
            f"Toponimi Extractor\nVersione: {get_plugin_version()}",
        )
