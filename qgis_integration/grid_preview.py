"""Layer temporaneo che mostra sulla mappa l'area selezionata e i quadranti
in cui verrà scomposta, per un controllo a vista prima di lanciare l'estrazione."""

from qgis.core import (
    QgsCategorizedSymbolRenderer,
    QgsFeature,
    QgsFillSymbol,
    QgsGeometry,
    QgsRectangle,
    QgsRendererCategory,
    QgsVectorLayer,
)

PREVIEW_LAYER_NAME = "Toponimi Extractor - anteprima quadranti"

KIND_AOI = "area"
KIND_TILE = "quadrante"


def build_preview_layer(aoi_extent, tiles, crs):
    """Crea il layer in memoria con un poligono per l'area selezionata e uno
    per ogni quadrante (overlap incluso: dove due quadranti si sovrappongono
    il riempimento risulta più scuro)."""
    layer = QgsVectorLayer(
        f"Polygon?crs={crs.authid()}"
        "&field=tipo:string&field=riga:integer&field=colonna:integer"
        "&field=larghezza_px:integer&field=altezza_px:integer",
        PREVIEW_LAYER_NAME,
        "memory",
    )
    if not crs.authid():
        layer.setCrs(crs)

    features = []
    for tile in tiles:
        feat = QgsFeature(layer.fields())
        feat.setGeometry(QgsGeometry.fromRect(QgsRectangle(tile.xmin, tile.ymin, tile.xmax, tile.ymax)))
        feat.setAttributes([KIND_TILE, tile.row, tile.col, tile.width_px, tile.height_px])
        features.append(feat)

    aoi_feat = QgsFeature(layer.fields())
    aoi_feat.setGeometry(QgsGeometry.fromRect(aoi_extent))
    aoi_feat.setAttributes([KIND_AOI, None, None, None, None])
    features.append(aoi_feat)

    layer.dataProvider().addFeatures(features)
    layer.updateExtents()

    tile_symbol = QgsFillSymbol.createSimple({
        "color": "0,90,255,30",
        "outline_color": "0,90,255,255",
        "outline_width": "0.4",
    })
    aoi_symbol = QgsFillSymbol.createSimple({
        "color": "0,0,0,0",
        "style": "no",
        "outline_color": "255,0,0,255",
        "outline_style": "dash",
        "outline_width": "0.8",
    })
    layer.setRenderer(QgsCategorizedSymbolRenderer("tipo", [
        QgsRendererCategory(KIND_TILE, tile_symbol, f"Quadranti ({len(tiles)})"),
        QgsRendererCategory(KIND_AOI, aoi_symbol, "Area selezionata"),
    ]))

    # Layer di servizio: QGIS non deve chiedere di salvarlo alla chiusura.
    layer.setCustomProperty("skipMemoryLayersCheck", 1)
    return layer
