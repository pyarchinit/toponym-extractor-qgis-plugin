"""Interrogazione GetCapabilities WMS per stimare la risoluzione ottimale del servizio."""

from xml.etree import ElementTree

from qgis.PyQt.QtCore import QUrl, QUrlQuery
from qgis.PyQt.QtNetwork import QNetworkRequest
from qgis.core import QgsBlockingNetworkRequest

DEFAULT_MAX_PIXEL_SIZE = 4096

# Standard OGC: 1 pixel = 0.28 mm. Per un CRS in metri, la risoluzione a terra
# (m/px) a una scala 1:N è N * 0.00028.
OGC_METERS_PER_PIXEL = 0.00028

# Ultimo GetCapabilities scaricato, (url, root XML): la stessa estrazione lo
# legge più volte (dimensione tile, range di scala) e può pesare diversi MB.
_last_capabilities = (None, None)


def scale_to_meters_per_pixel(scale_denominator):
    return scale_denominator * OGC_METERS_PER_PIXEL


def _strip_ns(tag):
    return tag.split("}", 1)[-1] if "}" in tag else tag


def get_capabilities_url(base_url):
    """Costruisce l'URL GetCapabilities a partire dall'URL base del servizio WMS."""
    url = QUrl(base_url)
    query = QUrlQuery(url)
    existing = {key.lower() for key, _ in query.queryItems()}

    for key, value in (("SERVICE", "WMS"), ("REQUEST", "GetCapabilities"), ("VERSION", "1.3.0")):
        if key.lower() not in existing:
            query.addQueryItem(key, value)
    url.setQuery(query)
    return url


def _fetch_capabilities_root(base_url, timeout_ms=10000):
    """Scarica e parsa il GetCapabilities. Ritorna la root XML, o None se la
    richiesta o il parsing falliscono."""
    global _last_capabilities

    url = get_capabilities_url(base_url)
    url_key = url.toString()
    if _last_capabilities[0] == url_key:
        return _last_capabilities[1]

    try:
        request = QNetworkRequest(url)
        request.setTransferTimeout(timeout_ms)
        blocking = QgsBlockingNetworkRequest()
        err = blocking.get(request, forceRefresh=False)
        if err != QgsBlockingNetworkRequest.ErrorCode.NoError:
            return None
        root = ElementTree.fromstring(bytes(blocking.reply().content()))
    except Exception:
        return None

    _last_capabilities = (url_key, root)
    return root


def _find_layer_element(root, layer_name):
    """Elemento <Layer> il cui <Name> corrisponde a `layer_name`, o None.
    Se il layer QGIS combina più layer WMS ("a,b"), usa il primo."""
    if not layer_name:
        return None
    wanted = layer_name.split(",")[0]
    for layer_elem in root.iter():
        if _strip_ns(layer_elem.tag) != "Layer":
            continue
        for child in layer_elem:
            if _strip_ns(child.tag) == "Name":
                if child.text == wanted:
                    return layer_elem
                break
    return None


def _child_float(elem, tag_name):
    for child in elem:
        if _strip_ns(child.tag) == tag_name and child.text:
            try:
                return float(child.text.strip())
            except ValueError:
                return None
    return None


def fetch_max_tile_pixel_size(base_url, layer_name=None, timeout_ms=10000):
    """Ritorna (max_tile_px, metri_per_pixel) letti dal GetCapabilities:
    - max_tile_px: lato massimo dei tile GetMap (MaxWidth/MaxHeight), o
      DEFAULT_MAX_PIXEL_SIZE se il servizio non dichiara limiti;
    - metri_per_pixel: risoluzione più dettagliata dichiarata per il layer,
      valida solo per CRS in metri, o None se il servizio non la dichiara.
    """
    root = _fetch_capabilities_root(base_url, timeout_ms)
    if root is None:
        return DEFAULT_MAX_PIXEL_SIZE, None
    return _parse_capabilities(root, layer_name)


def _parse_capabilities(root, layer_name=None):
    limits = []
    for elem in root.iter():
        if _strip_ns(elem.tag) in ("MaxWidth", "MaxHeight") and elem.text:
            try:
                limits.append(int(elem.text.strip()))
            except ValueError:
                pass
    limits = [v for v in limits if v > 0]
    max_pixel = min(limits) if limits else DEFAULT_MAX_PIXEL_SIZE

    return max_pixel, _find_layer_resolution(root, layer_name)


def _find_layer_resolution(root, layer_name):
    """Risoluzione a terra più dettagliata (m/px) dichiarata per il layer:
    MinScaleDenominator (WMS 1.3.0) oppure ScaleHint min (WMS 1.1.1).
    Ritorna None se non determinabile.
    """
    layer_elem = _find_layer_element(root, layer_name)
    if layer_elem is None:
        return None

    min_scale = _child_float(layer_elem, "MinScaleDenominator")
    if min_scale and min_scale > 0:
        return scale_to_meters_per_pixel(min_scale)

    for child in layer_elem:
        if _strip_ns(child.tag) == "ScaleHint":
            try:
                # ScaleHint è la diagonale di un pixel in unità di mappa.
                hint = float(child.attrib.get("min", ""))
            except ValueError:
                return None
            return hint / (2 ** 0.5) if hint > 0 else None
    return None


def fetch_layer_scale_range(base_url, layer_name, timeout_ms=10000):
    """Legge MinScaleDenominator/MaxScaleDenominator (WMS 1.3) dichiarati dal
    servizio per `layer_name`: fuori da questo intervallo di scala, il server
    restituisce tipicamente un'immagine bianca/vuota per policy — A PRESCINDERE
    dalla risoluzione o dal livello di zoom richiesti. Un tile sempre vuoto a
    qualunque risoluzione provata è il sintomo tipico di una richiesta fuori
    da questo range, non di una risoluzione sbagliata.

    Ritorna (min_scale_denominator, max_scale_denominator), ciascuno None se
    il servizio non dichiara quel limite o se la richiesta/parsing fallisce
    (nessun vincolo noto: il chiamante non dovrebbe scartare nulla in base a
    questa informazione se è None).
    """
    if not layer_name:
        return None, None
    root = _fetch_capabilities_root(base_url, timeout_ms)
    if root is None:
        return None, None

    layer_elem = _find_layer_element(root, layer_name)
    if layer_elem is None:
        return None, None
    return (
        _child_float(layer_elem, "MinScaleDenominator"),
        _child_float(layer_elem, "MaxScaleDenominator"),
    )
