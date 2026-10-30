"""Utility per estrarre parametri (URL, layer, CRS) da un QgsRasterLayer WMS."""

from qgis.core import QgsRasterLayer, Qgis


class NotAWmsLayerError(Exception):
    pass


def is_wms_layer(layer):
    return (
        layer is not None
        and hasattr(layer, "providerType")
        and layer.providerType() == "wms"
    )


def get_wms_params(layer):
    """Estrae dall'URI del provider WMS: base url, nome del layer remoto, formato immagine, crs."""
    if not is_wms_layer(layer):
        raise NotAWmsLayerError(f"Il layer '{layer.name() if layer else None}' non è un layer WMS.")

    uri = layer.dataProvider().dataSourceUri()
    params = _parse_wms_uri(uri)

    return {
        "base_url": params.get("url", ""),
        "layer_name": params.get("layers", ""),
        "image_format": params.get("format", "image/png"),
        "crs": params.get("crs", layer.crs().authid()),
    }


def _parse_wms_uri(uri):
    """Il datasourceUri di un provider WMS QGIS è nel formato
    'key1=value1&key2=value2&...' con url-encoding sui singoli valori.
    """
    from urllib.parse import unquote

    params = {}
    for part in uri.split("&"):
        if "=" not in part:
            continue
        key, _, value = part.partition("=")
        params[key] = unquote(value)
    return params


def get_map_units_per_pixel(map_canvas):
    """Ritorna la risoluzione corrente della mappa in unità CRS per pixel."""
    extent = map_canvas.extent()
    width_px = map_canvas.mapSettings().outputSize().width()
    if width_px <= 0:
        return 1.0
    return extent.width() / width_px
