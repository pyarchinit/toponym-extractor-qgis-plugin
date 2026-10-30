"""Scomposizione di un'area di interesse (AOI) in quadranti (tile) per richieste GetMap."""

import math
from collections import namedtuple

Tile = namedtuple("Tile", ["row", "col", "xmin", "ymin", "xmax", "ymax", "width_px", "height_px"])


def clamp_overlap(overlap_px, max_tile_pixel_size):
    """Limita l'overlap a un quarto del lato del tile: oltre, la fascia
    sovrapposta (su entrambi i lati) supererebbe l'area utile e il tile reale
    uscirebbe più grande di max_tile_pixel_size."""
    return max(0, min(overlap_px, max_tile_pixel_size // 4))


def coarsest_useful_resolution(extent, max_tile_pixel_size):
    """Risoluzione (unità CRS/px) alla quale l'intera area occupa esattamente
    un tile pieno. Andare più grossolani non fa risparmiare nulla (il tile
    resta uno) e toglie solo pixel al testo: un'area resa in 200 px quando
    ne potrebbe usare 1400 è illeggibile senza motivo."""
    return max(extent.width(), extent.height()) / max_tile_pixel_size


def _grid_shape(extent, crs_units_per_pixel, max_tile_pixel_size, overlap_px):
    total_width_px = extent.width() / crs_units_per_pixel
    total_height_px = extent.height() / crs_units_per_pixel

    # L'overlap si somma su entrambi i lati del tile: il lato reale in pixel
    # non deve superare max_tile_pixel_size, quindi il passo utile è più piccolo.
    usable_px = max(max_tile_pixel_size - 2 * overlap_px, 1)

    def count(total_px):
        # Se il lato sta in un solo tile non ci sono vicini, quindi nessun
        # overlap da riservare: un tile solo, a piena dimensione.
        if total_px <= max_tile_pixel_size + 1e-6:
            return 1
        return math.ceil(total_px / usable_px)

    return count(total_width_px), count(total_height_px)


def estimate_tile_count(extent, crs_units_per_pixel, max_tile_pixel_size, overlap_px=0):
    """Numero di tile che build_grid produrrebbe, senza costruirli. Ritorna
    None se risoluzione o dimensione tile non sono valide."""
    if crs_units_per_pixel <= 0 or max_tile_pixel_size <= 0:
        return None
    overlap_px = clamp_overlap(overlap_px, max_tile_pixel_size)
    n_cols, n_rows = _grid_shape(extent, crs_units_per_pixel, max_tile_pixel_size, overlap_px)
    return n_cols * n_rows


def finest_resolution_for_tile_budget(extent, max_tile_pixel_size, overlap_px, max_tiles, start_resolution):
    """La risoluzione più dettagliata (unità CRS/px più basse) che non supera
    `max_tiles` quadranti, cercata a partire da `start_resolution` verso il
    dettaglio. Serve a "spendere" un numero di quadranti deciso dall'utente
    nel modo più utile: tutto il dettaglio che quel numero consente."""
    valid = start_resolution   # nota per rispettare il budget (o comunque punto di partenza)
    too_fine = start_resolution / 64.0
    count = estimate_tile_count(extent, too_fine, max_tile_pixel_size, overlap_px)
    if count is not None and count <= max_tiles:
        return too_fine
    for _ in range(50):
        middle = (valid + too_fine) / 2.0
        count = estimate_tile_count(extent, middle, max_tile_pixel_size, overlap_px)
        if count is not None and count <= max_tiles:
            valid = middle
        else:
            too_fine = middle
    return valid


def build_grid(extent, crs_units_per_pixel, max_tile_pixel_size, overlap_px=0):
    """Suddivide `extent` (QgsRectangle) in una griglia di tile.

    - crs_units_per_pixel: risoluzione a terra desiderata (unità del CRS per pixel).
    - max_tile_pixel_size: lato massimo in pixel di ciascun tile (limite del servizio WMS).
    - overlap_px: sovrapposizione in pixel tra tile adiacenti, per evitare di tagliare
      testo a cavallo di un bordo.

    Ritorna una lista di oggetti Tile.
    """
    if crs_units_per_pixel <= 0:
        raise ValueError("crs_units_per_pixel deve essere positivo")
    if max_tile_pixel_size <= 0:
        raise ValueError("max_tile_pixel_size deve essere positivo")

    overlap_px = clamp_overlap(overlap_px, max_tile_pixel_size)
    n_cols, n_rows = _grid_shape(extent, crs_units_per_pixel, max_tile_pixel_size, overlap_px)

    tile_width_map = extent.width() / n_cols
    tile_height_map = extent.height() / n_rows
    overlap_map = overlap_px * crs_units_per_pixel

    tiles = []
    for row in range(n_rows):
        for col in range(n_cols):
            xmin = extent.xMinimum() + col * tile_width_map - (overlap_map if col > 0 else 0)
            xmax = extent.xMinimum() + (col + 1) * tile_width_map + (overlap_map if col < n_cols - 1 else 0)
            ymin = extent.yMinimum() + row * tile_height_map - (overlap_map if row > 0 else 0)
            ymax = extent.yMinimum() + (row + 1) * tile_height_map + (overlap_map if row < n_rows - 1 else 0)

            width_px = max(1, int(round((xmax - xmin) / crs_units_per_pixel)))
            height_px = max(1, int(round((ymax - ymin) / crs_units_per_pixel)))

            tiles.append(Tile(row, col, xmin, ymin, xmax, ymax, width_px, height_px))

    return tiles
