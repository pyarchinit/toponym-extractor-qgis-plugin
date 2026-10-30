"""Verifica e installazione (su richiesta) delle librerie Python opzionali
usate dai motori OCR: pytesseract, Pillow, anthropic, openai.

QGIS ha un proprio interprete Python; queste librerie potrebbero non essere
disponibili anche se pip le vede installate a livello di sistema (es. quando
il sys.path del processo QGIS non include lo user site-packages)."""

import importlib.util
import os
import subprocess
import sys

OPTIONAL_PACKAGES = [
    ("pytesseract", "pytesseract"),
    ("PIL", "Pillow"),
    ("anthropic", "anthropic"),
    ("openai", "openai"),
]


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
