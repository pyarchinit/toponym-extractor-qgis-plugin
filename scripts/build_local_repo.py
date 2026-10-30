"""Crea lo ZIP del plugin e un repository locale QGIS (dist/plugins.xml).

Uso:  python scripts/build_local_repo.py

In QGIS: Plugin -> Gestisci e installa plugin -> Impostazioni -> Aggiungi,
URL = l'indirizzo file:/// stampato alla fine. Dopo ogni modifica alza
`version=` in metadata.txt e rilancia lo script: QGIS mostra l'aggiornamento.
"""

import configparser
import datetime
import os
import pathlib
import zipfile
from xml.sax.saxutils import escape

ROOT = pathlib.Path(__file__).resolve().parent.parent
DIST = ROOT / "dist"
PLUGIN_DIR_NAME = "toponimi_extractor"  # nome della cartella nello ZIP: deve essere un modulo Python valido

INCLUDE_FILES = ["__init__.py", "metadata.txt", "toponimi_extractor.py", "requirements.txt", "README.md", "SETTINGS_GUIDE.md"]
INCLUDE_DIRS = ["core", "ocr", "qgis_integration", "tasks", "ui", "docs", "icons", "i18n"]


def read_metadata():
    parser = configparser.ConfigParser(interpolation=None)
    parser.read(ROOT / "metadata.txt", encoding="utf-8")
    return parser["general"]


def build_zip(version):
    DIST.mkdir(exist_ok=True)
    zip_name = f"{PLUGIN_DIR_NAME}-{version}.zip"
    zip_path = DIST / zip_name
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        for name in INCLUDE_FILES:
            z.write(ROOT / name, f"{PLUGIN_DIR_NAME}/{name}")
        for directory in INCLUDE_DIRS:
            for path in sorted((ROOT / directory).rglob("*")):
                if path.is_dir() or "__pycache__" in path.parts or path.suffix == ".pyc":
                    continue
                z.write(path, f"{PLUGIN_DIR_NAME}/{path.relative_to(ROOT).as_posix()}")
    return zip_path


def build_plugins_xml(meta, zip_path):
    def field(key, default=""):
        return escape(meta.get(key, default).strip())

    now = datetime.datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    zip_url = zip_path.resolve().as_uri()
    icon = field("icon")
    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<plugins>
  <pyqgis_plugin name="{field('name')}" version="{field('version')}">
    <description><![CDATA[{meta.get('description', '').strip()}]]></description>
    <about><![CDATA[{meta.get('about', '').strip()}]]></about>
    <version>{field('version')}</version>
    <qgis_minimum_version>{field('qgisMinimumVersion', '3.40')}</qgis_minimum_version>
    <qgis_maximum_version>{field('qgisMaximumVersion', '4.99')}</qgis_maximum_version>
    <supports_qt6>{field('supportsQt6', 'False')}</supports_qt6>
    <homepage>{field('homepage')}</homepage>
    <file_name>{escape(zip_path.name)}</file_name>
    <icon>{icon}</icon>
    <author_name><![CDATA[{meta.get('author', '').strip()}]]></author_name>
    <download_url>{escape(zip_url)}</download_url>
    <uploaded_by><![CDATA[{meta.get('author', '').strip()}]]></uploaded_by>
    <create_date>{now}</create_date>
    <update_date>{now}</update_date>
    <experimental>{field('experimental', 'False')}</experimental>
    <deprecated>{field('deprecated', 'False')}</deprecated>
    <tracker>{field('tracker')}</tracker>
    <repository>{field('repository')}</repository>
    <tags>{field('tags')}</tags>
    <server>False</server>
  </pyqgis_plugin>
</plugins>
"""
    xml_path = DIST / "plugins.xml"
    xml_path.write_text(xml, encoding="utf-8")
    return xml_path


def main():
    meta = read_metadata()
    zip_path = build_zip(meta["version"].strip())
    xml_path = build_plugins_xml(meta, zip_path)
    print(f"ZIP:          {zip_path}")
    print(f"plugins.xml:  {xml_path}")
    print(f"URL repository per QGIS:  {xml_path.resolve().as_uri()}")


if __name__ == "__main__":
    main()
