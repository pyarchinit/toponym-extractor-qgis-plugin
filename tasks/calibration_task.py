"""QgsTask che esegue in background la ricerca della modalità migliore
(agente AI): rendering, letture di prova e chiamate al modello possono
richiedere minuti, e nel thread principale bloccherebbero tutta QGIS."""

from qgis.core import QgsTask, QgsMessageLog, Qgis
from qgis.PyQt.QtCore import pyqtSignal

from ..core import ai_agent_calibrator
from ..core.i18n import tr

LOG_TAG = "Toponimi Extractor"


class CalibrationTask(QgsTask):
    stepChanged = pyqtSignal(str)

    def __init__(self, layer, aoi_extent, settings, on_finished=None):
        super().__init__(tr("Ricerca della modalità migliore per l'area"), QgsTask.Flag.CanCancel)
        # Copia privata del layer, creata nel thread principale (vedi ExtractionTask).
        self.layer = layer.clone()
        self.aoi_extent = aoi_extent
        self.settings = settings
        self.on_finished_callback = on_finished

        self.result = None
        self.exception = None

    def run(self):
        try:
            self.result = ai_agent_calibrator.run_ai_driven_calibration(
                self.layer,
                self.aoi_extent,
                self.settings["ocr"],
                agent_provider=self.settings.get("ai_agent_provider", "claude"),
                progress_callback=self.stepChanged.emit,
                wms_params=self.settings.get("wms_params"),
                overlap_px=self.settings.get("overlap_px", 200),
                is_canceled=self.isCanceled,
            )
        except ai_agent_calibrator.AiCalibrationCanceled:
            return False
        except Exception as exc:
            self.exception = exc
            QgsMessageLog.logMessage(f"Ricerca modalità migliore fallita: {exc}", LOG_TAG, Qgis.MessageLevel.Critical)
            return False
        return not self.isCanceled()

    def finished(self, result):
        if self.on_finished_callback:
            self.on_finished_callback(self)
