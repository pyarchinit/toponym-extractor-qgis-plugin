"""Verifica e installazione (su richiesta) delle librerie Python opzionali
usate dai motori OCR: pytesseract, Pillow, anthropic, openai.

QGIS ha un proprio interprete Python; queste librerie potrebbero non essere
disponibili anche se pip le vede installate a livello di sistema (es. quando
il sys.path del processo QGIS non include lo user site-packages)."""

import importlib.metadata
import importlib.util
import os
import subprocess
import sys

from .i18n import tr

OPTIONAL_PACKAGES = [
    ("pytesseract", "pytesseract"),
    ("PIL", "Pillow"),
    ("anthropic", "anthropic"),
    ("openai", "openai"),
]


PYPI_URL = "https://pypi.org/project/{}/"
ANTHROPIC_KEYS_URL = "https://console.anthropic.com/settings/keys"
OPENAI_KEYS_URL = "https://platform.openai.com/api-keys"
OPENAI_ENV_VARS = ("OPENAI_API_KEY",)
ANTHROPIC_ENV_VARS = ("ANTHROPIC_API_KEY", "CLAUDE_API_KEY")

OK, WARN, MISSING = "ok", "warn", "missing"


def _item(key, label, status, detail, url=None, url_label=None, pip_name=None):
    return {"key": key, "label": label, "status": status, "detail": detail,
            "url": url, "url_label": url_label, "pip_name": pip_name}


def _package_item(module_name, pip_name):
    if importlib.util.find_spec(module_name) is None:
        return _item(module_name, pip_name, MISSING, tr("non installato"),
                     PYPI_URL.format(pip_name), "PyPI", pip_name)
    try:
        version = importlib.metadata.version(pip_name)
    except importlib.metadata.PackageNotFoundError:
        version = ""
    return _item(module_name, pip_name, OK, tr("installato") + (f" ({version})" if version else ""))


def _tesseract_item(cmd, lang):
    from ..ocr import tesseract_engine

    guide = tesseract_engine.PREBUILT_INSTALLERS_URL
    try:
        info = tesseract_engine.probe(cmd or tesseract_engine.find_tesseract())
    except tesseract_engine.TesseractNotAvailable as exc:
        return _item("tesseract", tr("Programma Tesseract"), MISSING,
                     tr("non trovato o non utilizzabile: {error}").format(error=exc),
                     guide, tr("Scarica Tesseract"))
    missing = [c for c in (lang or "").split("+") if c and c not in info["languages"]]
    if missing:
        return _item("tesseract", tr("Programma Tesseract"), WARN,
                     tr("{version}; lingua non installata: {languages}").format(
                         version=info["version"], languages=", ".join(missing)),
                     tesseract_engine.TESSDATA_URL, tr("Scarica i dati lingua"))
    return _item("tesseract", tr("Programma Tesseract"), OK,
                 tr("{version} — lingue: {languages} ({count} installate)").format(
                     version=info["version"], languages=(lang or "-"), count=len(info["languages"])))


def _key_item(key, label, explicit_key, env_names, url):
    """Stato di una API key: da campo manuale o da variabile d'ambiente.
    Il valore della chiave non viene mai restituito."""
    if explicit_key:
        return _item(key, label, OK, tr("impostata nel pannello Impostazioni"))
    for name in env_names:
        if os.environ.get(name):
            return _item(key, label, OK, tr("trovata nella variabile d'ambiente {name}").format(name=name))
    return _item(key, label, WARN,
                 tr("nessuna chiave: imposta la variabile {names} oppure inseriscila in Impostazioni → Cloud e chiavi").format(
                     names=" / ".join(env_names)),
                 url, tr("Crea una API key"))


def requirements_report(tesseract_cmd=None, tesseract_lang="ita", claude_key=None,
                        claude_env_var=None, openai_key=None):
    """Elenco dei requisiti con stato OK/WARN/MISSING, dettaglio e link.
    Non richiede QGIS e non solleva eccezioni."""
    env_names = ((claude_env_var,) if claude_env_var else ()) + ANTHROPIC_ENV_VARS
    items = [_tesseract_item(tesseract_cmd, tesseract_lang)]
    items += [_package_item(m, p) for m, p in OPTIONAL_PACKAGES]
    items.append(_key_item("claude_key", tr("API key Claude (Anthropic)"), claude_key,
                           tuple(dict.fromkeys(env_names)), ANTHROPIC_KEYS_URL))
    items.append(_key_item("openai_key", tr("API key GPT (OpenAI)"), openai_key,
                           OPENAI_ENV_VARS, OPENAI_KEYS_URL))
    return items


def missing_packages():
    """Ritorna la lista dei nomi pip (es. 'Pillow') non importabili in questo momento."""
    from qgis.core import QgsMessageLog, Qgis

    # find_spec individua il pacchetto senza importarlo: questo controllo gira
    # a ogni avvio di QGIS e importare davvero anthropic/openai costa secondi.
    missing = []
    for module_name, pip_name in OPTIONAL_PACKAGES:
        if importlib.util.find_spec(module_name) is None:
            QgsMessageLog.logMessage(
                f"Dependency check: {module_name} non trovato",
                "Toponimi Extractor",
                Qgis.MessageLevel.Warning,
            )
            missing.append(pip_name)
    return missing


def _find_python_executable():
    """All'interno del processo QGIS, sys.executable punta a qgis-bin.exe
    (non un vero interprete Python), quindi non è utilizzabile per pip.
    Cerca il python.exe reale accanto all'installazione di QGIS.
    """
    candidates = []

    # sys.prefix in QGIS punta di solito a .../apps/PythonXYZ
    prefix_candidate = os.path.join(sys.prefix, "python.exe")
    candidates.append(prefix_candidate)

    # Risale da questo file: .../apps/PythonXYZ/Lib/site-packages/.../core/dependency_check.py
    # cerca un python.exe nei genitori.
    current = os.path.dirname(os.path.abspath(__file__))
    for _ in range(8):
        current = os.path.dirname(current)
        candidate = os.path.join(current, "python.exe")
        candidates.append(candidate)

    for path in candidates:
        if os.path.isfile(path):
            return path
    return None


def _user_site_packages_dir():
    """Cartella site-packages "utente" standard di Windows per questa versione
    di Python (es. %APPDATA%\\Python\\Python312\\site-packages). Usata come
    --target esplicito perché il Python di QGIS ha ENABLE_USER_SITE=False e
    quindi 'pip install --user' è bloccato (\"User site-packages are disabled
    for this Python\").
    """
    appdata = os.environ.get("APPDATA")
    if not appdata:
        return None
    version_tag = f"Python{sys.version_info.major}{sys.version_info.minor}"
    return os.path.join(appdata, "Python", version_tag, "site-packages")


def _install_one(python_exe, target_dir, pip_name, log_callback=None):
    """Installa un singolo pacchetto. Ritorna (success, output_text).

    Usa --ignore-installed per evitare che pip provi a RIMUOVERE una versione
    già presente in target_dir prima di reinstallarla: se quel pacchetto è una
    DLL/.pyd già caricata in memoria da QGIS (es. dipendenza transitiva come
    pydantic_core), la cancellazione fallisce con PermissionError e fa fallire
    l'intera installazione anche per i pacchetti successivi.
    """
    cmd = [
        python_exe, "-m", "pip", "install",
        "--target", target_dir,
        "--ignore-installed",
        "--no-warn-script-location",
        pip_name,
    ]
    if log_callback:
        log_callback(f"Esecuzione: {' '.join(cmd)}")

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    except Exception as exc:
        return False, f"{pip_name}: eccezione durante l'esecuzione di pip: {exc}"

    output = (result.stdout or "") + (result.stderr or "")
    if result.returncode != 0:
        # Estrae solo l'ultima riga utile (es. "ERROR: ...") invece del traceback completo.
        error_lines = [line for line in output.splitlines() if line.strip()]
        short_error = error_lines[-1] if error_lines else "errore sconosciuto"
        return False, f"{pip_name}: FALLITO — {short_error}"

    return True, f"{pip_name}: installato correttamente."


def install_packages(pip_names, log_callback=None):
    """Installa i pacchetti con pip nel vero interprete Python di QGIS, uno
    alla volta: se un pacchetto fallisce (es. un file già in uso da QGIS non
    cancellabile), l'errore viene catturato e si prosegue con i successivi
    invece di interrompere l'intero processo.

    Il Python di QGIS ha lo user-site disabilitato, quindi 'pip install --user'
    fallisce sempre con "User site-packages are disabled for this Python".
    Si usa invece '--target' puntato alla cartella user site-packages standard
    (già inclusa in PYTHONPATH), che non passa dal meccanismo "user site" e
    quindi non richiede privilegi di amministratore.

    Ritorna (all_succeeded, output_text) dove output_text riassume l'esito di
    ciascun pacchetto, uno per riga.
    """
    if not pip_names:
        return True, ""

    python_exe = _find_python_executable()
    if python_exe is None:
        return False, (
            "Impossibile individuare l'interprete Python di QGIS (python.exe). "
            "Installa manualmente i pacchetti eseguendo, dal prompt dei comandi:\n"
            f"\"<cartella QGIS>\\apps\\Python3XX\\python.exe\" -m pip install --target "
            f"\"%APPDATA%\\Python\\Python3XX\\site-packages\" {' '.join(pip_names)}"
        )

    target_dir = _user_site_packages_dir()
    if target_dir is None:
        return False, "Impossibile determinare la cartella site-packages utente (variabile APPDATA assente)."

    os.makedirs(target_dir, exist_ok=True)

    results = []
    all_ok = True
    for pip_name in pip_names:
        try:
            ok, message = _install_one(python_exe, target_dir, pip_name, log_callback)
        except Exception as exc:
            ok, message = False, f"{pip_name}: errore inatteso — {exc}"
        results.append(message)
        all_ok = all_ok and ok

    return all_ok, "\n".join(results)
