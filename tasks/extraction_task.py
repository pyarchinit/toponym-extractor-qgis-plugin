"""QgsTask che esegue l'estrazione completa in background, senza bloccare la UI."""

import os
import tempfile
from datetime import datetime, timezone

from qgis.core import QgsTask, QgsMessageLog, Qgis
from qgis.PyQt.QtCore import pyqtSignal

from ..core import grid as grid_mod
from ..core import wms_capabilities
from ..core import georeference
from ..core.usage_tracker import UsageTracker
from ..qgis_integration import tile_renderer
from ..ocr import cascade

LOG_TAG = "Toponimi Extractor"

# Lato massimo (px, overlap incluso) di un tile in modalità automatica. Claude
# rimpicciolisce le immagini oltre ~1568 px sul lato lungo, quindi restiamo sotto.
OCR_FRIENDLY_TILE_PX = 1400

# Oltre questo numero di quadranti l'estrazione non parte: quasi sempre è una
# risoluzione troppo fine per l'area scelta (ogni quadrante è un rendering più
# una chiamata OCR, eventualmente a pagamento).
MAX_TILES_PER_EXTRACTION = 2000

# Quadranti consecutivi falliti per errore del motore OCR (API key errata,
# modello inesistente, Tesseract assente...) dopo i quali ci si ferma, invece
# di ripetere lo stesso errore su tutta l'area.
MAX_CONSECUTIVE_FAILED_TILES = 5

BLANK_TILE_ENGINE = "vuoto"


def resolve_resolution(aoi_extent, settings, capabilities_resolution, max_tile_px):
    """Risoluzione (unità CRS/px) da usare: quella impostata dall'utente o
    da una calibrazione; altrimenti, in automatico, quella a cui l'utente
    stava guardando la mappa quando ha disegnato l'area, resa più fine se
    l'area starebbe comunque in un solo tile (un tile costa uguale pieno
    o mezzo vuoto, tanto vale usarne tutti i pixel). La risoluzione
    dichiarata dal servizio è spesso molto più fine del necessario e
    serve solo come ripiego quando quella a video non è nota."""
    override = settings.get("meters_per_pixel_override")
    if override:
        return override

    canvas_resolution = settings.get("canvas_units_per_pixel")
    if canvas_resolution:
        longest_side = max(aoi_extent.width(), aoi_extent.height())
        return min(canvas_resolution, longest_side / max_tile_px)

    if capabilities_resolution and settings.get("crs_is_metric"):
        return capabilities_resolution
    raise ValueError(
        "Impossibile determinare una risoluzione automatica per questo layer: "
        "imposta una scala o una risoluzione manuale nelle impostazioni."
    )


def plan_grid(aoi_extent, settings):
    """Decide risoluzione e dimensione dei quadranti per `aoi_extent` e
    costruisce la griglia. Usata sia dall'estrazione sia dall'anteprima sulla
    mappa, così quello che si vede prima è esattamente quello che verrà letto.

    Ritorna (tiles, meters_per_pixel, max_tile_px). Solleva ValueError se la
    risoluzione non è determinabile o i quadranti sarebbero troppi.
    """
    wms_params = settings["wms_params"]
    max_tile_px, resolution = wms_capabilities.fetch_max_tile_pixel_size(
        wms_params["base_url"], wms_params.get("layer_name")
    )

    # In automatico il limite è il minore tra quello del servizio WMS e la
    # dimensione oltre la quale i modelli vision rimpiccioliscono l'immagine
    # (il testo piccolo diventa illeggibile): un tile deve arrivare all'OCR
    # alla stessa scala a cui è stato renderizzato.
    max_tile_px = settings.get("max_tile_px_override") or min(max_tile_px, OCR_FRIENDLY_TILE_PX)
    overlap_px = settings.get("overlap_px", 200)
    meters_per_pixel = resolve_resolution(aoi_extent, settings, resolution, max_tile_px)

    # Qualunque sia la provenienza della risoluzione (impostazioni, scala,
    # calibrazione, agente AI), mai più grossolana di un tile pieno.
    full_tile_resolution = grid_mod.coarsest_useful_resolution(aoi_extent, max_tile_px)
    if 0 < full_tile_resolution < meters_per_pixel:
        QgsMessageLog.logMessage(
            f"Risoluzione richiesta {meters_per_pixel:.4g} unità CRS/px troppo grossolana per l'area "
            f"(un solo quadrante semivuoto): uso {full_tile_resolution:.4g}, che riempie il quadrante.",
            LOG_TAG,
            Qgis.Warning,
        )
        meters_per_pixel = full_tile_resolution

    # "Raddoppia quadranti": l'utente concede N volte i quadranti di base, e
    # si usa tutto il dettaglio che quel numero consente.
    budget_multiplier = settings.get("tile_budget_multiplier", 1)
    if budget_multiplier > 1:
        base_tiles = grid_mod.estimate_tile_count(aoi_extent, meters_per_pixel, max_tile_px, overlap_px)
        if base_tiles:
            meters_per_pixel = grid_mod.finest_resolution_for_tile_budget(
                aoi_extent, max_tile_px, overlap_px, base_tiles * budget_multiplier, meters_per_pixel
            )

    estimated_tiles = grid_mod.estimate_tile_count(aoi_extent, meters_per_pixel, max_tile_px, overlap_px)
    if estimated_tiles is not None and estimated_tiles > MAX_TILES_PER_EXTRACTION:
        raise ValueError(
            f"Con risoluzione {meters_per_pixel:.4g} unità CRS/px l'area selezionata produrrebbe "
            f"{estimated_tiles} quadranti (limite: {MAX_TILES_PER_EXTRACTION}). "
            f"Seleziona un'area più piccola o imposta una risoluzione meno dettagliata."
        )

    tiles = grid_mod.build_grid(aoi_extent, meters_per_pixel, max_tile_px, overlap_px=overlap_px)
    return tiles, meters_per_pixel, max_tile_px


class ExtractionTask(QgsTask):
    stepChanged = pyqtSignal(str)

    def __init__(self, layer, aoi_extent, settings, on_finished=None):
        super().__init__("Estrazione toponimi da raster WMS", QgsTask.Flag.CanCancel)
        # Copia privata del layer, creata qui nel thread principale: il task
        # non deve dipendere da un layer che l'utente può rimuovere dal
        # progetto mentre l'estrazione è in corso.
        self.layer = layer.clone()
        self.aoi_extent = aoi_extent
        self.settings = settings
        self.on_finished_callback = on_finished

        self.exception = None
        self.total_words_found = 0
        self.tiles_processed = 0
        self.tiles_total = 0
        self.all_results = []
        self.usage_tracker = UsageTracker()
        # Cartella con le immagini renderizzate dei tile: NON viene ripulita a
        # fine task (a differenza delle versioni precedenti), perché il dock
        # dei risultati la usa per poter rilanciare l'OCR cloud su un tile
        # specifico dopo la revisione manuale. La pulizia è responsabilità di
        # chi consuma i risultati (vedi ResultsDock.cleanup_tile_cache).
        self.tile_cache_dir = tempfile.mkdtemp(prefix="toponimi_extractor_")

    def run(self):
        try:
            self._run_impl()
        except Exception as exc:
            self.exception = exc
            QgsMessageLog.logMessage(str(exc), LOG_TAG, Qgis.Critical)
            self._cleanup_tile_cache_dir()
            return False

        if self.isCanceled():
            # I risultati parziali restano disponibili al chiamante, che può
            # proporli all'utente: in quel caso le immagini dei tile servono ancora.
            if not self.all_results:
                self._cleanup_tile_cache_dir()
            return False

        return True

    def _cleanup_tile_cache_dir(self):
        """Ripulisce le immagini dei tile quando non verranno mai mostrate nel
        dock dei risultati (errore, annullamento, o nessun quadrante generato).
        In caso di successo la pulizia spetta invece al chiamante (ResultsDock),
        che ne ha ancora bisogno per il rilancio manuale dell'OCR cloud."""
        import shutil

        shutil.rmtree(self.tile_cache_dir, ignore_errors=True)

    def _run_impl(self):
        wms_params = self.settings["wms_params"]
        fonte_nome_wms = wms_params.get("layer_name", "")
        fonte_indirizzo_wms = wms_params.get("base_url", "")
        data_estrazione = datetime.now(timezone.utc).isoformat(timespec="seconds")

        self.stepChanged.emit("Interrogazione del servizio WMS e suddivisione dell'area in quadranti...")
        tiles, meters_per_pixel, max_tile_px = plan_grid(self.aoi_extent, self.settings)
        self.tiles_total = len(tiles)

        QgsMessageLog.logMessage(
            f"Griglia: {self.tiles_total} quadranti, "
            f"risoluzione={meters_per_pixel} unità CRS/px, max_tile_px={max_tile_px}",
            LOG_TAG,
            Qgis.Info,
        )

        # Riferimento diretto alla lista: se l'utente annulla, i toponimi già
        # trovati restano leggibili dal chiamante.
        all_results = self.all_results
        consecutive_failed_tiles = 0

        for tile in tiles:
            if self.isCanceled():
                break

            self.stepChanged.emit(
                f"Quadrante {self.tiles_processed + 1}/{self.tiles_total}: rendering immagine..."
            )
            image_path, is_blank = tile_renderer.render_tile(self.layer, tile, self.tile_cache_dir)

            if is_blank:
                self.usage_tracker.record_tile_engine(BLANK_TILE_ENGINE)
                self.stepChanged.emit(
                    f"Quadrante {self.tiles_processed + 1}/{self.tiles_total}: immagine vuota, OCR saltato."
                )
                self.tiles_processed += 1
                self.setProgress(100.0 * self.tiles_processed / self.tiles_total)
                continue

            self.stepChanged.emit(
                f"Quadrante {self.tiles_processed + 1}/{self.tiles_total}: riconoscimento testo (OCR)..."
            )
            errors_before = self.usage_tracker.error_count()
            words, engine_used = cascade.run_cascade(
                image_path, tile.width_px, tile.height_px, self.settings["ocr"], self.usage_tracker,
                is_canceled=self.isCanceled,
            )
            self.usage_tracker.record_tile_engine(engine_used)

            if engine_used == "none" and self.usage_tracker.error_count() > errors_before:
                consecutive_failed_tiles += 1
            else:
                consecutive_failed_tiles = 0
            if consecutive_failed_tiles >= MAX_CONSECUTIVE_FAILED_TILES and not self.isCanceled():
                raise RuntimeError(
                    f"Estrazione interrotta: {consecutive_failed_tiles} quadranti consecutivi falliti "
                    f"per errore del motore OCR.\n\n{self.usage_tracker.summary_text()}"
                )

            QgsMessageLog.logMessage(
                f"Tile [{tile.row},{tile.col}] ({tile.width_px}x{tile.height_px}px): "
                f"{len(words)} parole trovate (motore: {engine_used})",
                LOG_TAG,
                Qgis.Info,
            )

            self.stepChanged.emit(
                f"Quadrante {self.tiles_processed + 1}/{self.tiles_total}: "
                f"{len(words)} toponimi trovati (motore: {engine_used})."
            )

            for word in words:
                x, y = georeference.word_center_to_map_xy(word, tile)
                all_results.append(
                    {
                        "toponimo": word["text"],
                        "x": x,
                        "y": y,
                        "fonte_nome_wms": fonte_nome_wms,
                        "fonte_indirizzo_wms": fonte_indirizzo_wms,
                        "data_estrazione": data_estrazione,
                        "source_engine": engine_used,
                        "confidence": word.get("confidence", 0.0),
                        "tile_row": tile.row,
                        "tile_col": tile.col,
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

            self.tiles_processed += 1
            self.setProgress(100.0 * self.tiles_processed / self.tiles_total)

        self.total_words_found = len(all_results)

        QgsMessageLog.logMessage(
            f"Totale parole trovate: {self.total_words_found} su {self.tiles_processed} quadranti processati.",
            LOG_TAG,
            Qgis.Info,
        )
        usage_summary = self.usage_tracker.summary_text()
        QgsMessageLog.logMessage(f"Utilizzo motori OCR:\n{usage_summary}", LOG_TAG, Qgis.Info)

        self.stepChanged.emit(
            f"Completato: {self.total_words_found} toponimi trovati in {self.tiles_processed} quadranti."
        )
        for line in self.usage_tracker.summary_lines():
            self.stepChanged.emit(f"  {line}")

    def finished(self, result):
        if self.on_finished_callback:
            self.on_finished_callback(result, self)
        else:
            # Nessuno consumerà i risultati (plugin disattivato durante l'estrazione).
            self._cleanup_tile_cache_dir()
