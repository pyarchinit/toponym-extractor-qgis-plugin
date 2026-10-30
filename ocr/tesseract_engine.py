"""Motore OCR locale basato su Tesseract (via pytesseract).

Restituisce, per ogni tile, una lista di parole/etichette riconosciute con il
bounding box IN PIXEL della singola parola (non del tile), così da poter
calcolare un punto geografico distinto per ciascun toponimo presente nel
quadrante.
"""

import os
import shutil
import subprocess

ENGINE_NAME = "tesseract"

# Dove procurarsi il programma Tesseract (il plugin installa solo la libreria
# Python pytesseract, non il motore OCR vero e proprio).
INSTALL_GUIDE_URL = "https://github.com/tesseract-ocr/tesseract#installing-tesseract"
PREBUILT_INSTALLERS_URL = "https://tesseract-ocr.github.io/tessdoc/Installation.html"
TESSDATA_URL = "https://github.com/tesseract-ocr/tessdata"
INSTALL_HINT = (
    f"Istruzioni di installazione: {INSTALL_GUIDE_URL} — "
    f"installer già pronti: {PREBUILT_INSTALLERS_URL}"
)


class TesseractNotAvailable(Exception):
    pass


def _candidate_paths():
    """Posizioni tipiche del binario, oltre al PATH di sistema."""
    local_appdata = os.environ.get("LOCALAPPDATA", "")
    return [
        r"C:\Program Files\Tesseract-OCR\tesseract.exe",
        r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
        os.path.join(local_appdata, "Programs", "Tesseract-OCR", "tesseract.exe") if local_appdata else "",
        r"C:\OSGeo4W\bin\tesseract.exe",
        "/opt/homebrew/bin/tesseract",
        "/usr/local/bin/tesseract",
        "/usr/bin/tesseract",
    ]


def find_tesseract():
    """Percorso del binario Tesseract trovato nel PATH o nelle posizioni
    tipiche, oppure None."""
    found = shutil.which("tesseract")
    if found:
        return found
    for path in _candidate_paths():
        if path and os.path.isfile(path):
            return path
    return None


def probe(cmd=None, timeout=20):
    """Interroga il binario (`cmd`, o quello nel PATH se vuoto) e ritorna
    {"version": str, "languages": [codici]}. Non richiede pytesseract.
    Solleva TesseractNotAvailable se il binario non parte."""
    exe = cmd or "tesseract"
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)

    def run(arg):
        try:
            done = subprocess.run(
                [exe, arg], capture_output=True, text=True, timeout=timeout, creationflags=flags
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise TesseractNotAvailable(str(exc)) from exc
        return (done.stdout or "") + (done.stderr or "")

    version_output = run("--version").strip()
    if not version_output:
        raise TesseractNotAvailable("Nessuna risposta dal programma.")
    version = version_output.splitlines()[0].strip()

    languages = [
        line.strip()
        for line in run("--list-langs").splitlines()
        if line.strip() and not line.lower().startswith("list of available")
    ]
    return {"version": version, "languages": languages}


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
