"""Scrittura dei risultati OCR in un layer puntuale GeoPackage."""

import os

from qgis.core import (
    QgsField,
    QgsFields,
    QgsFeature,
    QgsGeometry,
    QgsPointXY,
    QgsWkbTypes,
    QgsVectorFileWriter,
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsCoordinateTransformContext,
    QgsCsException,
    QgsVectorLayer,
    QgsProject,
    QgsFillSymbol,
    QgsSingleSymbolRenderer,
)
from qgis.PyQt.QtCore import QMetaType


FIELDS_SPEC = [
    ("toponimo", QMetaType.Type.QString),
    ("fonte_nome_wms", QMetaType.Type.QString),
    ("fonte_indirizzo_wms", QMetaType.Type.QString),
    ("data_estrazione", QMetaType.Type.QString),
    ("source_engine", QMetaType.Type.QString),
    ("confidence", QMetaType.Type.Double),
    ("tile_row", QMetaType.Type.Int),
    ("tile_col", QMetaType.Type.Int),
    ("rotation_deg", QMetaType.Type.Double),
]

AOI_FIELDS_SPEC = [
    ("fonte_nome_wms", QMetaType.Type.QString),
    ("fonte_indirizzo_wms", QMetaType.Type.QString),
    ("data_estrazione", QMetaType.Type.QString),
    ("toponimi_trovati", QMetaType.Type.Int),
]

AOI_LAYER_NAME_SUFFIX = "_aree_estrazione"


def _build_fields(fields_spec):
    fields = QgsFields()
    for name, qtype in fields_spec:
        fields.append(QgsField(name, qtype))
    return fields


def _create_layer(path, crs_authid, layer_name, geometry_type, fields_spec):
    """Crea (o sovrascrive) un layer nel GeoPackage a `path`. Ritorna il path
    del layer nel formato `path|layername=...` utilizzabile da QgsVectorLayer.
    """
    fields = _build_fields(fields_spec)
    crs = QgsCoordinateReferenceSystem(crs_authid)

    options = QgsVectorFileWriter.SaveVectorOptions()
    options.driverName = "GPKG"
    options.layerName = layer_name
    options.fileEncoding = "UTF-8"
    if os.path.exists(path):
        options.actionOnExistingFile = QgsVectorFileWriter.CreateOrOverwriteLayer

    writer = QgsVectorFileWriter.create(
        path,
        fields,
        geometry_type,
        crs,
        QgsCoordinateTransformContext(),
        options,
    )
    if writer.hasError() != QgsVectorFileWriter.NoError:
        raise IOError(f"Errore creazione GeoPackage: {writer.errorMessage()}")
    del writer

    return f"{path}|layername={layer_name}"


def create_geopackage(path, crs_authid, layer_name="toponimi"):
    """Crea (o sovrascrive) un GeoPackage con un layer puntuale vuoto pronto a
    ricevere le feature estratte. Ritorna il path del layer nel formato
    `path|layername=...` utilizzabile da QgsVectorLayer.
    """
    return _create_layer(path, crs_authid, layer_name, QgsWkbTypes.Point, FIELDS_SPEC)


def layer_exists(path, layer_name):
    """True se nel GeoPackage a `path` esiste già un layer chiamato `layer_name`."""
    if not os.path.exists(path):
        return False
    uri = f"{path}|layername={layer_name}"
    layer = QgsVectorLayer(uri, layer_name, "ogr")
    return layer.isValid()


def schema_is_compatible(layer, fields_spec=FIELDS_SPEC):
    """True se `layer` ha già tutti i campi attesi da `fields_spec` (append sicuro)."""
    existing_names = {f.name() for f in layer.fields()}
    expected_names = {name for name, _ in fields_spec}
    return expected_names.issubset(existing_names)


def open_or_create_layer(path, crs_authid, layer_name="toponimi", overwrite=False):
    """Apre il layer se il GeoPackage/layer esiste già (per appendere), oppure
    lo crea. Se `overwrite` è True e il layer esiste, viene ricreato vuoto.
    """
    if not overwrite and os.path.exists(path):
        uri = f"{path}|layername={layer_name}"
        layer = QgsVectorLayer(uri, layer_name, "ogr")
        if layer.isValid():
            return layer
    uri = create_geopackage(path, crs_authid, layer_name)
    return QgsVectorLayer(uri, layer_name, "ogr")


def aoi_layer_name(points_layer_name):
    """Nome convenzionale del layer poligonale delle aree di estrazione,
    derivato dal nome del layer puntuale dei toponimi."""
    return f"{points_layer_name}{AOI_LAYER_NAME_SUFFIX}"


def open_or_create_aoi_layer(path, crs_authid, layer_name, overwrite=False):
    """Come open_or_create_layer, ma per il layer poligonale delle AOI."""
    if not overwrite and os.path.exists(path):
        uri = f"{path}|layername={layer_name}"
        layer = QgsVectorLayer(uri, layer_name, "ogr")
        if layer.isValid():
            return layer
    uri = _create_layer(path, crs_authid, layer_name, QgsWkbTypes.Polygon, AOI_FIELDS_SPEC)
    return QgsVectorLayer(uri, layer_name, "ogr")


def append_aoi_extent(
    layer, rect, fonte_nome_wms, fonte_indirizzo_wms, data_estrazione, toponimi_trovati, source_crs=None
):
    """Aggiunge un poligono rettangolare (l'AOI disegnata) al layer aree_estrazione.

    `rect` è un QgsRectangle nel sistema di riferimento `source_crs` (se
    omesso, si assume quello del layer di destinazione).
    """
    _require_valid(layer)
    feat = QgsFeature(layer.fields())
    feat.setGeometry(_to_layer_crs(QgsGeometry.fromRect(rect), layer, source_crs))
    feat.setAttribute("fonte_nome_wms", fonte_nome_wms)
    feat.setAttribute("fonte_indirizzo_wms", fonte_indirizzo_wms)
    feat.setAttribute("data_estrazione", data_estrazione)
    feat.setAttribute("toponimi_trovati", int(toponimi_trovati))

    _write_features(layer, [feat])


def _to_layer_crs(geometry, layer, source_crs):
    """Porta `geometry` da `source_crs` al sistema di riferimento del layer
    di destinazione. Aggiungendo a un layer esistente i due possono differire
    (es. layer creato da una carta UTM 32, nuova estrazione da una UTM 33):
    senza conversione i punti finirebbero in un altro posto."""
    if source_crs is None or not source_crs.isValid() or source_crs == layer.crs():
        return geometry
    try:
        geometry.transform(QgsCoordinateTransform(source_crs, layer.crs(), QgsProject.instance()))
    except QgsCsException as exc:
        raise IOError(
            f"Impossibile convertire le coordinate da {source_crs.authid()} a {layer.crs().authid()}: {exc}"
        ) from exc
    return geometry


def _require_valid(layer):
    if not layer.isValid():
        raise IOError(f"Layer '{layer.name()}' non valido: impossibile scrivere nel GeoPackage.")


def _write_features(layer, features):
    """Aggiunge e salva le feature, sollevando IOError se il salvataggio non
    riesce (file bloccato, layer già in modifica altrove, disco pieno...)."""
    if not layer.startEditing():
        raise IOError(f"Impossibile aprire in modifica il layer '{layer.name()}'.")
    if not layer.addFeatures(features) or not layer.commitChanges():
        errors = "; ".join(layer.commitErrors()) or "errore sconosciuto"
        layer.rollBack()
        raise IOError(f"Salvataggio nel layer '{layer.name()}' non riuscito: {errors}")


def append_results(layer, results, source_crs=None):
    """Aggiunge una lista di risultati OCR al layer puntuale.

    `results` è una lista di dict con chiavi:
    toponimo, x, y, fonte_nome_wms, fonte_indirizzo_wms, data_estrazione,
    source_engine, confidence, tile_row, tile_col, rotation_deg
    dove (x, y) sono nel sistema di riferimento `source_crs` (se omesso, si
    assume quello del layer di destinazione).
    """
    if not results:
        return 0
    _require_valid(layer)

    features = []
    for r in results:
        feat = QgsFeature(layer.fields())
        feat.setGeometry(_to_layer_crs(QgsGeometry.fromPointXY(QgsPointXY(r["x"], r["y"])), layer, source_crs))
        feat.setAttribute("toponimo", r.get("toponimo", ""))
        feat.setAttribute("fonte_nome_wms", r.get("fonte_nome_wms", ""))
        feat.setAttribute("fonte_indirizzo_wms", r.get("fonte_indirizzo_wms", ""))
        feat.setAttribute("data_estrazione", r.get("data_estrazione", ""))
        feat.setAttribute("source_engine", r.get("source_engine", ""))
        feat.setAttribute("confidence", float(r.get("confidence", 0.0)))
        feat.setAttribute("tile_row", int(r.get("tile_row", -1)))
        feat.setAttribute("tile_col", int(r.get("tile_col", -1)))
        feat.setAttribute("rotation_deg", float(r.get("rotation_deg", 0.0)))
        features.append(feat)

    _write_features(layer, features)
    return len(features)


def add_layer_to_project(layer):
    """Aggiunge il layer al progetto; se la stessa sorgente è già caricata
    (estrazione precedente sullo stesso GeoPackage) aggiorna quello esistente
    invece di creare un doppione in legenda."""
    for existing in QgsProject.instance().mapLayers().values():
        if existing.source() == layer.source():
            existing.reload()
            existing.triggerRepaint()
            return
    QgsProject.instance().addMapLayer(layer)


def apply_aoi_style(layer):
    """Poligono senza riempimento, contorno rosso tratteggiato."""
    symbol = QgsFillSymbol.createSimple({
        "color": "0,0,0,0",
        "style": "no",
        "outline_color": "255,0,0,255",
        "outline_style": "dash",
        "outline_width": "0.6",
    })
    layer.setRenderer(QgsSingleSymbolRenderer(symbol))
    layer.triggerRepaint()
