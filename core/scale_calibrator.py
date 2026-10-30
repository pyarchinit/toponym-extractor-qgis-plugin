"""Calibrazione automatica della scala/risoluzione ottimale per un layer WMS.

Molti servizi WMS dichiarano una risoluzione massima "ideale" che in pratica
non basta a rendere leggibile il testo (etichette piccole, font decorativo,
sovrapposizioni). Invece di fidarsi del GetCapabilities, questo modulo
renderizza un tile di prova al CENTRO dell'area scelta dall'utente, a diverse
scale candidate (dalla più economica/larga alla più dettagliata), esegue
Tesseract su ciascuna e sceglie la scala PIÙ LARGA possibile (meno tile, meno
costo) che supera una soglia di confidenza media e un numero minimo di parole
trovate — cioè la scala più economica che "si legge ancora bene".
"""

from qgis.core import QgsRectangle, QgsMessageLog, Qgis

from ..qgis_integration import tile_renderer
from ..ocr import tesseract_engine
from .grid import Tile
from .wms_capabilities import scale_to_meters_per_pixel

LOG_TAG = "Toponimi Extractor"

# Scale candidate da provare, dalla più larga (economica) alla più dettagliata.
# L'algoritmo si ferma alla PRIMA che soddisfa i criteri, quindi l'ordine conta.
DEFAULT_CANDIDATE_SCALES = [10000, 5000, 2000, 1000, 500, 250]

TEST_TILE_SIZE_PX = 512


class ScaleCalibrationResult:
    def __init__(self, scale_denominator, meters_per_pixel, avg_confidence, words_found, accepted):
        self.scale_denominator = scale_denominator
        self.meters_per_pixel = meters_per_pixel
        self.avg_confidence = avg_confidence
        self.words_found = words_found
        self.accepted = accepted

    def __repr__(self):
        return (
            f"1:{self.scale_denominator} ({self.meters_per_pixel:.4f} m/px) — "
            f"{self.words_found} parole, confidenza media {self.avg_confidence:.0f}%"
            f"{' [OK]' if self.accepted else ''}"
        )


def _center_test_extent(aoi_extent, meters_per_pixel, tile_size_px):
    """QgsRectangle centrato sull'AOI, di dimensione fissa in pixel alla
    risoluzione data — indipendente dalla dimensione dell'AOI stessa."""
    half_width_map = (tile_size_px * meters_per_pixel) / 2.0
    center_x = aoi_extent.center().x()
    center_y = aoi_extent.center().y()
    return QgsRectangle(
        center_x - half_width_map,
        center_y - half_width_map,
        center_x + half_width_map,
        center_y + half_width_map,
    )


def calibrate_scale(
    layer,
    aoi_extent,
    temp_dir,
    tesseract_lang="ita",
    tesseract_cmd=None,
    candidate_scales=None,
    min_confidence=50.0,
    min_words=1,
    progress_callback=None,
):
    """Prova le scale candidate (dalla più larga alla più dettagliata) su un
    tile di prova al centro di `aoi_extent`, e ritorna la prima
    ScaleCalibrationResult accettata (la più economica che si legge bene).

    Se nessuna scala soddisfa i criteri, ritorna comunque tutti i risultati
    provati: il chiamante può scegliere la migliore disponibile (es. quella
    con confidenza più alta) o avvisare l'utente.

    Ritorna (accepted_result_or_None, all_results).
    """
    if candidate_scales is None:
        candidate_scales = DEFAULT_CANDIDATE_SCALES

    if tesseract_cmd:
        tesseract_engine.configure_tesseract_cmd(tesseract_cmd)

    if not tesseract_engine.is_available():
        raise tesseract_engine.TesseractNotAvailable(
            "Tesseract non disponibile: la calibrazione automatica della scala richiede Tesseract locale.\n\n"
            + tesseract_engine.INSTALL_HINT
        )

    results = []

    for scale_denominator in candidate_scales:
        meters_per_pixel = scale_to_meters_per_pixel(scale_denominator)
        test_extent = _center_test_extent(aoi_extent, meters_per_pixel, TEST_TILE_SIZE_PX)

        if progress_callback:
            progress_callback(f"Test scala 1:{scale_denominator}...")

        test_tile = Tile(
            row=0, col=0,
            xmin=test_extent.xMinimum(), ymin=test_extent.yMinimum(),
            xmax=test_extent.xMaximum(), ymax=test_extent.yMaximum(),
            width_px=TEST_TILE_SIZE_PX, height_px=TEST_TILE_SIZE_PX,
        )

        image_path = tile_renderer.render_tile_to_file(
            layer, test_tile, temp_dir, filename=f"calib_{scale_denominator}.png"
        )

        words = tesseract_engine.recognize_words(image_path, lang=tesseract_lang, min_confidence=0)

        if words:
            avg_confidence = sum(w["confidence"] for w in words) / len(words)
        else:
            avg_confidence = 0.0

        accepted = len(words) >= min_words and avg_confidence >= min_confidence

        result = ScaleCalibrationResult(scale_denominator, meters_per_pixel, avg_confidence, len(words), accepted)
        results.append(result)

        QgsMessageLog.logMessage(f"Calibrazione scala: {result}", LOG_TAG, Qgis.MessageLevel.Info)
        if progress_callback:
            progress_callback(str(result))

        if accepted:
            return result, results

    return None, results
