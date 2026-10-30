"""Conversione dei bounding box (in pixel, relativi a un tile) in coordinate
mappa, prendendo il centro del bbox di ciascuna singola parola/etichetta
riconosciuta — mai il centro dell'intero tile.
"""


def word_center_to_map_xy(word, tile):
    """word: dict con left/top/width/height in pixel relativi all'immagine del tile.
    tile: oggetto Tile (core.grid.Tile) con xmin/ymin/xmax/ymax e width_px/height_px.

    Ritorna (x, y) nel CRS del tile, corrispondenti al centro del bbox della
    singola parola — cioè al centro geometrico della scritta stessa.
    """
    if tile.width_px <= 0 or tile.height_px <= 0:
        raise ValueError("Tile con dimensioni pixel non valide")

    px_center_x = word["left"] + word["width"] / 2.0
    px_center_y = word["top"] + word["height"] / 2.0

    map_units_per_px_x = (tile.xmax - tile.xmin) / tile.width_px
    map_units_per_px_y = (tile.ymax - tile.ymin) / tile.height_px

    x = tile.xmin + px_center_x * map_units_per_px_x
    # L'asse Y dell'immagine cresce verso il basso, quello della mappa verso l'alto.
    y = tile.ymax - px_center_y * map_units_per_px_y

    return x, y
