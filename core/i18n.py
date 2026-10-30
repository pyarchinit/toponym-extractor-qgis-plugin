"""Traduzione dei testi dell'interfaccia.

I testi sorgente nel codice sono in italiano e passano da `tr()`. Le traduzioni
stanno in `i18n/toponimi_extractor_<lingua>.qm` (compilati da
`i18n/toponimi_extractor_<lingua>.ts`); `install_translator` carica quella
della lingua di QGIS se esiste, altrimenti i testi restano in italiano.
"""

import os

try:
    from qgis.PyQt.QtCore import QCoreApplication, QLocale, QSettings, QTranslator
except ImportError:  # test della logica pura fuori da QGIS: i testi restano in italiano
    QCoreApplication = None

TRANSLATION_CONTEXT = "ToponimiExtractor"
I18N_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "i18n")

_translator = None


def tr(text):
    if QCoreApplication is None:
        return text
    return QCoreApplication.translate(TRANSLATION_CONTEXT, text)


def _qgis_language():
    settings = QSettings()
    if settings.value("locale/overrideFlag", False, type=bool):
        code = settings.value("locale/userLocale", "", type=str)
    else:
        code = QLocale().name()
    return (code or "it")[:2].lower()


def install_translator():
    """Carica la traduzione per la lingua di QGIS; no-op per l'italiano o se
    manca il file. Restituisce True se una traduzione è stata installata."""
    global _translator
    if QCoreApplication is None:
        return False
    lang = _qgis_language()
    if lang == "it":
        return False
    path = os.path.join(I18N_DIR, f"toponimi_extractor_{lang}.qm")
    if not os.path.isfile(path):
        # Qualunque lingua senza traduzione dedicata ricade sull'inglese.
        path = os.path.join(I18N_DIR, "toponimi_extractor_en.qm")
    if not os.path.isfile(path):
        return False
    translator = QTranslator()
    if translator.load(path):
        QCoreApplication.installTranslator(translator)
        _translator = translator
        return True
    return False


def remove_translator():
    global _translator
    if _translator is not None:
        QCoreApplication.removeTranslator(_translator)
        _translator = None
