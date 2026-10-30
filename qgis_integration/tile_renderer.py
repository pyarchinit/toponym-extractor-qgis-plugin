"""Rendering di un singolo tile (quadrante) del layer WMS su file immagine.

Usa il motore di rendering di QGIS (QgsMapRendererCustomPainterJob) invece di
costruire manualmente richieste GetMap, così da riutilizzare automaticamente
autenticazione, referer, stile e parametri già configurati sul layer in
progetto.
"""

import os
import time

from qgis.PyQt.QtCore import QSize
from qgis.PyQt.QtGui import QImage, QPainter, QColor
from qgis.core import QgsMapSettings, QgsMapRendererCustomPainterJob, QgsRectangle, QgsMessageLog, Qgis

LOG_TAG = "Toponimi Extractor"

# Bug osservato: con layer WMS, QgsMapRendererCustomPainterJob.waitForFinished()
# può ritornare PRIMA che il provider abbia effettivamente ricevuto e decodificato
# la risposta di rete per quella precisa combinazione extent/risoluzione (specie
# la prima volta che si richiede una nuova area/scala, non ancora in cache) — il
# job di "pittura" si considera concluso, ma l'immagine risultante è
# completamente bianca anche se il server avrebbe davvero contenuto da mostrare
# (confermato: la stessa identica richiesta GetMap fatta direttamente via HTTP
# torna un'immagine piena di contenuto cartografico leggibile). Sintomo
# osservato: 0 parole trovate su OGNI tile di un'intera estrazione, qualunque
# risoluzione/tile size si provi, mentre lo stesso identico file scaricato a
# parte via HTTP viene letto correttamente da Claude con confidenza alta.
# Fix: se il rendering risulta sospettosamente "vuoto" (quasi tutto un unico
# colore, tipicamente lo sfondo bianco), si lascia respirare l'event loop Qt
# (necessario perché le risposte di rete asincrone del provider WMS vengano
# processate) e si ri-renderizza, fino a RENDER_MAX_RETRIES tentativi extra.
RENDER_MAX_RETRIES = 2
BLANK_FRACTION_THRESHOLD = 0.995  # oltre il 99.5% di pixel del colore di sfondo = sospetto
RENDER_RETRY_WAIT_SECONDS = 0.5


def _is_suspiciously_blank(image, background_color):
    """Euristica economica: campiona una griglia di pixel sull'immagine
    (non tutti, per restare veloce anche su tile da megapixel) e stima la
    frazione che corrisponde al colore di sfondo. Non è una detection
    perfetta (una mappa realmente vuota in quel punto esiste, vedi le note
    nel prompt dell'agente AI DRIVEN), ma è sufficiente per distinguere "quasi
    certamente un rendering fallito" da "contenuto reale, anche se rado".
    """
    width = image.width()
    height = image.height()
    if width <= 0 or height <= 0:
        return True

    sample_step_x = max(1, width // 64)
    sample_step_y = max(1, height // 64)
    bg_rgb = (background_color.red(), background_color.green(), background_color.blue())

    total = 0
    background_count = 0
    for y in range(0, height, sample_step_y):
        for x in range(0, width, sample_step_x):
            pixel = image.pixelColor(x, y)
            total += 1
            if (pixel.red(), pixel.green(), pixel.blue()) == bg_rgb:
                background_count += 1

    if total == 0:
        return True
    return (background_count / total) >= BLANK_FRACTION_THRESHOLD


def render_tile_to_file(layer, tile, output_dir, filename=None):
    """Come render_tile, ma ritorna solo il path del file creato."""
    output_path, _ = render_tile(layer, tile, output_dir, filename)
    return output_path


def render_tile(layer, tile, output_dir, filename=None):
    """Renderizza l'estensione del tile usando il layer WMS fornito e salva
    il risultato come PNG in output_dir. Ritorna (path, is_blank), dove
    is_blank è True solo se OGNI pixel è del colore di sfondo: un tile così
    non contiene testo e non ha senso passarlo all'OCR.

    Rilancia automaticamente il rendering (fino a RENDER_MAX_RETRIES volte)
    se il risultato appare sospettosamente vuoto, dando tempo al provider WMS
    di completare la richiesta di rete asincrona — vedi nota sopra.
    """
    extent = QgsRectangle(tile.xmin, tile.ymin, tile.xmax, tile.ymax)
    size = QSize(max(1, tile.width_px), max(1, tile.height_px))
    background_color = QColor(255, 255, 255)

    image = None
    for attempt in range(RENDER_MAX_RETRIES + 1):
        settings = QgsMapSettings()
        settings.setLayers([layer])
        settings.setBackgroundColor(background_color)
        settings.setOutputSize(size)
        settings.setExtent(extent)
        settings.setDestinationCrs(layer.crs())

        image = QImage(size, QImage.Format.Format_RGB32)
        image.fill(background_color)

        painter = QPainter(image)
        job = QgsMapRendererCustomPainterJob(settings, painter)
        job.start()
        job.waitForFinished()
        painter.end()

        if not _is_suspiciously_blank(image, background_color):
            break

        if attempt < RENDER_MAX_RETRIES:
            QgsMessageLog.logMessage(
                f"Rendering tile [{tile.row},{tile.col}] risultato vuoto al tentativo "
                f"{attempt + 1}/{RENDER_MAX_RETRIES + 1}: ri-renderizzo tra "
                f"{RENDER_RETRY_WAIT_SECONDS}s (possibile richiesta WMS non ancora completata).",
                LOG_TAG, Qgis.Warning,
            )
            # NOTA: qui serve un'attesa "passiva" (time.sleep), non
            # QCoreApplication.processEvents() — questa funzione può girare
            # anche nel worker thread di un QgsTask (ExtractionTask.run()),
            # dove processEvents() non è sicuro: solo il thread principale
            # possiede l'event loop applicativo. Un semplice sleep dà comunque
            # tempo al provider WMS di completare la richiesta di rete prima
            # del prossimo job.start()/waitForFinished(), che è la parte che
            # conta per il retry.
            time.sleep(RENDER_RETRY_WAIT_SECONDS)

    if filename is None:
        filename = f"tile_{tile.row}_{tile.col}.png"
    output_path = os.path.join(output_dir, filename)
    image.save(output_path, "PNG")

    # Confronto esatto (non a campione come _is_suspiciously_blank): una sola
    # etichetta su sfondo bianco deve comunque arrivare all'OCR.
    blank_reference = QImage(size, QImage.Format.Format_RGB32)
    blank_reference.fill(background_color)
    return output_path, image == blank_reference
