"""Esportazione dei toponimi salvati in un GeoPackage come testo leggero,
pensato per essere indicizzato in una RAG.

Scelte di formato:
- una riga per toponimo, completa in sé (nome, coordinate, fonte): chi
  spezza il testo in frammenti non deve risalire a un'intestazione lontana
  per capire di che luogo e di che carta si parla;
- coordinate in gradi WGS84 (le più riconoscibili fuori da un GIS) più
  quelle originali del layer, per poter ritrovare il punto esatto;
- i dati uguali per tutto il layer (indirizzo del servizio, sistema di
  riferimento) stanno una volta sola nell'intestazione del layer.
"""

from qgis.core import (
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsProject,
    QgsProviderRegistry,
    QgsVectorLayer,
    QgsWkbTypes,
)

from .geopackage_writer import AOI_LAYER_NAME_SUFFIX

NAME_FIELD = "toponimo"


def _point_layers(gpkg_path):
    """Layer puntuali del GeoPackage che contengono toponimi estratti dal plugin."""
    metadata = QgsProviderRegistry.instance().providerMetadata("ogr")
    layers = []
    for item in metadata.querySublayers(gpkg_path):
        name = item.name()
        if name.endswith(AOI_LAYER_NAME_SUFFIX):
            continue
        layer = QgsVectorLayer(f"{gpkg_path}|layername={name}", name, "ogr")
        if not layer.isValid() or layer.geometryType() != QgsWkbTypes.GeometryType.PointGeometry:
            continue
        if layer.fields().indexOf(NAME_FIELD) < 0:
            continue
        layers.append(layer)
    return layers


def _value(feature, field_names, name, default=""):
    if name not in field_names:
        return default
    value = feature[name]
    # I NULL di QGIS non sono None: si riconoscono perché la loro stringa è "NULL".
    if value is None or str(value) == "NULL":
        return default
    return value


def _format_number(value, suffix=""):
    try:
        return f"{float(value):.0f}{suffix}"
    except (TypeError, ValueError):
        return ""


def _layer_lines(layer):
    """Righe di testo per un layer: intestazione + una riga per toponimo.
    Ritorna (lines, count)."""
    field_names = set(layer.fields().names())
    crs = layer.crs()
    wgs84 = QgsCoordinateReferenceSystem("EPSG:4326")
    to_wgs84 = None
    if crs.isValid() and crs != wgs84:
        to_wgs84 = QgsCoordinateTransform(crs, wgs84, QgsProject.instance())

    rows = []
    sources = {}
    dates = set()
    for feature in layer.getFeatures():
        name = str(_value(feature, field_names, NAME_FIELD)).strip()
        geometry = feature.geometry()
        if not name or geometry is None or geometry.isEmpty():
            continue

        point = geometry.asPoint()
        try:
            lonlat = to_wgs84.transform(point) if to_wgs84 is not None else point
            position = f"{lonlat.y():.5f} N, {lonlat.x():.5f} E (WGS84)"
        except Exception:
            position = "coordinate WGS84 non calcolabili"

        source_name = str(_value(feature, field_names, "fonte_nome_wms"))
        source_url = str(_value(feature, field_names, "fonte_indirizzo_wms"))
        if source_name or source_url:
            sources[source_name] = source_url
        date = str(_value(feature, field_names, "data_estrazione"))[:10]
        if date:
            dates.add(date)

        parts = [name, position, f"{crs.authid() or 'CRS originale'} x {point.x():.1f}, y {point.y():.1f}"]
        if source_name:
            parts.append(f"fonte: {source_name}")
        engine = str(_value(feature, field_names, "source_engine"))
        confidence = _format_number(_value(feature, field_names, "confidence", None), "%")
        if engine:
            parts.append(f"letto da {engine}" + (f", confidenza {confidence}" if confidence else ""))
        if date:
            parts.append(f"estratto il {date}")
        rows.append((name.lower(), "- " + " — ".join(parts)))

    rows.sort(key=lambda row: row[0])

    lines = [f"## Layer \"{layer.name()}\"", ""]
    for source_name, source_url in sorted(sources.items()):
        lines.append(f"Fonte cartografica: {source_name or '(senza nome)'}" + (f" — {source_url}" if source_url else ""))
    lines.append(f"Sistema di riferimento originale: {crs.authid() or crs.description() or 'sconosciuto'}")
    if dates:
        lines.append(f"Date di estrazione: {', '.join(sorted(dates))}")
    lines.append(f"Toponimi: {len(rows)}")
    lines.append("")
    lines.extend(text for _, text in rows)
    lines.append("")
    return lines, len(rows)


def build_text(gpkg_path, title=None):
    """Testo completo per il GeoPackage. Ritorna (text, layer_count, toponym_count)."""
    layers = _point_layers(gpkg_path)
    body = []
    total = 0
    for layer in sorted(layers, key=lambda l: l.name().lower()):
        lines, count = _layer_lines(layer)
        if count:
            body.extend(lines)
            total += count

    layer_count = sum(1 for line in body if line.startswith("## Layer "))
    header = [
        f"# {title or 'Toponimi estratti da cartografia'}",
        "",
        "Elenco di toponimi (nomi di luoghi, corsi d'acqua, rilievi, località) letti automaticamente "
        "da carte raster e georeferenziati. Ogni riga è un toponimo con la sua posizione e la carta "
        "da cui proviene. Le letture sono automatiche: grafie e posizioni possono contenere errori.",
        "",
        f"Layer: {layer_count} — Toponimi: {total}",
        "",
    ]
    return "\n".join(header + body), layer_count, total


def export_geopackage_to_text(gpkg_path, output_path, title=None):
    """Scrive il testo su `output_path`. Ritorna (layer_count, toponym_count).
    Solleva ValueError se il GeoPackage non contiene toponimi."""
    text, layer_count, total = build_text(gpkg_path, title)
    if total == 0:
        raise ValueError("Il GeoPackage non contiene layer di toponimi creati dal plugin.")
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(text)
    return layer_count, total
