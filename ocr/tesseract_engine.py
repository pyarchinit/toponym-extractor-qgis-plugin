"""Motore OCR locale basato su Tesseract (via pytesseract).

Restituisce, per ogni tile, una lista di parole/etichette riconosciute con il
bounding box IN PIXEL della singola parola (non del tile), così da poter
calcolare un punto geografico distinto per ciascun toponimo presente nel
quadrante.
"""

ENGINE_NAME = "tesseract"

# Dove procurarsi il programma Tesseract (il plugin installa solo la libreria
# Python pytesseract, non il motore OCR vero e proprio).
INSTALL_GUIDE_URL = "https://github.com/tesseract-ocr/tesseract#installing-tesseract"
PREBUILT_INSTALLERS_URL = "https://tesseract-ocr.github.io/tessdoc/Installation.html"
INSTALL_HINT = (
    f"Istruzioni di installazione: {INSTALL_GUIDE_URL} — "
    f"installer già pronti: {PREBUILT_INSTALLERS_URL}"
)


class TesseractNotAvailable(Exception):
    pass


def _import_deps():
    try:
        import pytesseract
        from PIL import Image
    except ImportError as exc:
        raise TesseractNotAvailable(
            "pytesseract/Pillow non installati nell'ambiente Python di QGIS."
        ) from exc
    return pytesseract, Image


def configure_tesseract_cmd(path):
    """Imposta il percorso del binario tesseract.exe, se fornito dall'utente."""
    if not path:
        return
    pytesseract, _ = _import_deps()
    pytesseract.pytesseract.tesseract_cmd = path


def recognize_words(image_path, lang="ita", min_confidence=0):
    """Esegue OCR sull'immagine del tile e ritorna una lista di dict:
    {text, left, top, width, height, confidence}
    con bounding box in pixel relativi all'immagine del tile.

    Le righe vuote o con confidenza -1 (spazi/righe non testuali secondo
    Tesseract) vengono scartate.
    """
    pytesseract, Image = _import_deps()

    try:
        image = Image.open(image_path)
    except Exception as exc:
        raise TesseractNotAvailable(f"Impossibile aprire l'immagine del tile: {exc}") from exc

    # Binario assente dal PATH o lingua non installata: per il chiamante è lo
    # stesso caso di "Tesseract non disponibile" (si passa al fallback cloud).
    try:
        with image:
            data = pytesseract.image_to_data(
                image, lang=lang, output_type=pytesseract.Output.DICT
            )
    except (pytesseract.TesseractNotFoundError, pytesseract.TesseractError) as exc:
        raise TesseractNotAvailable(f"Tesseract non utilizzabile: {exc}\n{INSTALL_HINT}") from exc

    words = []
    n = len(data.get("text", []))
    for i in range(n):
        text = (data["text"][i] or "").strip()
        if not text:
            continue
        try:
            conf = float(data["conf"][i])
        except (ValueError, TypeError):
            conf = -1.0
        if conf < 0 or conf < min_confidence:
            continue

        words.append(
            {
                "text": text,
                "left": int(data["left"][i]),
                "top": int(data["top"][i]),
                "width": int(data["width"][i]),
                "height": int(data["height"][i]),
                "confidence": conf,
            }
        )

    return words


def is_available():
    try:
        pytesseract, _ = _import_deps()
        pytesseract.get_tesseract_version()
        return True
    except Exception:
        return False
