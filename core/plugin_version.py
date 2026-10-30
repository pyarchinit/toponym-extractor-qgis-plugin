"""Lettura della versione del plugin da metadata.txt, per mostrarla in GUI
e verificare a colpo d'occhio se QGIS sta usando l'ultima versione dei file."""

import configparser
import os


def get_plugin_version():
    """Ritorna la stringa di versione dichiarata in metadata.txt (es. "0.2.2"),
    oppure "?" se non determinabile.
    """
    plugin_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    metadata_path = os.path.join(plugin_dir, "metadata.txt")

    parser = configparser.ConfigParser()
    try:
        parser.read(metadata_path, encoding="utf-8")
        return parser.get("general", "version", fallback="?")
    except (OSError, configparser.Error):
        return "?"
