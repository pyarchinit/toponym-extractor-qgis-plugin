"""Test unitari per i moduli che non dipendono da QGIS."""

import sys
import os
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from core.georeference import word_center_to_map_xy


class FakeExtent:
    def __init__(self, xmin, ymin, xmax, ymax):
        self._xmin, self._ymin, self._xmax, self._ymax = xmin, ymin, xmax, ymax

    def xMinimum(self):
        return self._xmin

    def yMinimum(self):
        return self._ymin

    def width(self):
        return self._xmax - self._xmin

    def height(self):
        return self._ymax - self._ymin


class GridTile:
    def __init__(self, row, col, xmin, ymin, xmax, ymax, width_px, height_px):
        self.row, self.col = row, col
        self.xmin, self.ymin, self.xmax, self.ymax = xmin, ymin, xmax, ymax
        self.width_px, self.height_px = width_px, height_px


class TestGeoreference(unittest.TestCase):
    def test_word_center_maps_to_tile_center_when_bbox_is_full_tile(self):
        tile = GridTile(0, 0, xmin=100.0, ymin=200.0, xmax=200.0, ymax=300.0, width_px=1000, height_px=1000)
        word = {"left": 0, "top": 0, "width": 1000, "height": 1000}
        x, y = word_center_to_map_xy(word, tile)
        self.assertAlmostEqual(x, 150.0)
        self.assertAlmostEqual(y, 250.0)

    def test_distinct_words_in_same_tile_get_distinct_points(self):
        tile = GridTile(0, 0, xmin=0.0, ymin=0.0, xmax=1000.0, ymax=1000.0, width_px=1000, height_px=1000)
        word_a = {"left": 0, "top": 0, "width": 100, "height": 20}
        word_b = {"left": 800, "top": 800, "width": 100, "height": 20}

        xa, ya = word_center_to_map_xy(word_a, tile)
        xb, yb = word_center_to_map_xy(word_b, tile)

        self.assertNotAlmostEqual(xa, xb)
        self.assertNotAlmostEqual(ya, yb)

    def test_y_axis_is_flipped_between_image_and_map(self):
        tile = GridTile(0, 0, xmin=0.0, ymin=0.0, xmax=100.0, ymax=100.0, width_px=100, height_px=100)
        word_top = {"left": 0, "top": 0, "width": 10, "height": 10}
        word_bottom = {"left": 0, "top": 90, "width": 10, "height": 10}

        _, y_top = word_center_to_map_xy(word_top, tile)
        _, y_bottom = word_center_to_map_xy(word_bottom, tile)

        # Un pixel in alto nell'immagine deve corrispondere a una Y maggiore in mappa.
        self.assertGreater(y_top, y_bottom)


from core.grid import build_grid, estimate_tile_count, finest_resolution_for_tile_budget
from core import deduplicator
from core.usage_tracker import UsageTracker
from ocr.claude_engine import _parse_response


class FakeQgsRectangle(FakeExtent):
    def xMaximum(self):
        return self._xmax

    def yMaximum(self):
        return self._ymax


class TestGrid(unittest.TestCase):
    def test_single_tile_when_area_smaller_than_max(self):
        extent = FakeQgsRectangle(0, 0, 100, 100)
        tiles = build_grid(extent, crs_units_per_pixel=1.0, max_tile_pixel_size=200)
        self.assertEqual(len(tiles), 1)

    def test_splits_into_multiple_tiles_when_area_exceeds_max(self):
        extent = FakeQgsRectangle(0, 0, 1000, 1000)
        tiles = build_grid(extent, crs_units_per_pixel=1.0, max_tile_pixel_size=400)
        self.assertGreater(len(tiles), 1)
        # Copertura totale: righe*colonne dovrebbe combaciare col numero di tile.
        rows = {t.row for t in tiles}
        cols = {t.col for t in tiles}
        self.assertEqual(len(tiles), len(rows) * len(cols))

    def test_tiles_never_exceed_max_size_even_with_huge_overlap(self):
        extent = FakeQgsRectangle(0, 0, 5000, 5000)
        tiles = build_grid(extent, crs_units_per_pixel=1.0, max_tile_pixel_size=256, overlap_px=300)
        for t in tiles:
            self.assertLessEqual(t.width_px, 256)
            self.assertLessEqual(t.height_px, 256)

    def test_estimate_matches_built_grid(self):
        extent = FakeQgsRectangle(0, 0, 7300, 4100)
        for max_px, overlap in ((1400, 200), (768, 192), (256, 300), (400, 0)):
            tiles = build_grid(extent, 1.0, max_px, overlap_px=overlap)
            self.assertEqual(estimate_tile_count(extent, 1.0, max_px, overlap), len(tiles))

    def test_area_that_fits_one_tile_is_not_split_by_overlap(self):
        # 2346 x 1909 m resi in modo che il lato lungo sia esattamente 1400 px.
        extent = FakeQgsRectangle(0, 0, 2346, 1909)
        tiles = build_grid(extent, crs_units_per_pixel=2346 / 1400, max_tile_pixel_size=1400, overlap_px=150)
        self.assertEqual(len(tiles), 1)
        self.assertEqual(tiles[0].width_px, 1400)

    def test_doubling_the_tile_budget_spends_it_on_detail(self):
        extent = FakeQgsRectangle(0, 0, 2346, 1909)
        base = 2346 / 1400
        self.assertEqual(estimate_tile_count(extent, base, 1400, 150), 1)

        doubled = finest_resolution_for_tile_budget(extent, 1400, 150, 2, base)
        self.assertEqual(estimate_tile_count(extent, doubled, 1400, 150), 2)
        # il lato corto riempie un tile: è il massimo dettaglio possibile con 2 quadranti
        self.assertAlmostEqual(doubled, 1909 / 1400, places=3)

        x4 = finest_resolution_for_tile_budget(extent, 1400, 150, 4, base)
        self.assertLess(x4, doubled)
        self.assertLessEqual(estimate_tile_count(extent, x4, 1400, 150), 4)

    def test_estimate_rejects_invalid_resolution(self):
        extent = FakeQgsRectangle(0, 0, 100, 100)
        self.assertIsNone(estimate_tile_count(extent, 0, 1400, 200))
        self.assertIsNone(estimate_tile_count(extent, -1.0, 1400, 200))

    def test_tiny_extent_still_gives_a_renderable_tile(self):
        # CRS in gradi con risoluzione grossolana: mai tile da 0 pixel.
        extent = FakeQgsRectangle(12.0, 44.0, 12.1, 44.1)
        tiles = build_grid(extent, crs_units_per_pixel=1.0, max_tile_pixel_size=1400, overlap_px=200)
        self.assertEqual(len(tiles), 1)
        self.assertGreaterEqual(tiles[0].width_px, 1)
        self.assertGreaterEqual(tiles[0].height_px, 1)


def _result(text, x, y, confidence, tile=(0, 0)):
    """Risultato letto dal quadrante `tile` di una griglia con quadranti da
    1000 m e 200 m di sovrapposizione tra colonne adiacenti."""
    row, col = tile
    xmin = col * 800.0
    return {
        "toponimo": text, "x": x, "y": y, "confidence": confidence,
        "tile_row": row, "tile_col": col,
        "tile_xmin": xmin, "tile_ymin": 0.0, "tile_xmax": xmin + 1000.0, "tile_ymax": 1000.0,
        "tile_width_px": 1000, "tile_height_px": 1000,
    }


class TestDeduplicator(unittest.TestCase):
    def test_same_name_read_by_two_tiles_in_their_overlap_is_a_duplicate(self):
        results = [
            _result("Póggio Berni", 900.0, 500.0, 60, tile=(0, 0)),
            _result("POGGIO BERNI", 908.0, 503.0, 90, tile=(0, 1)),
        ]
        kept, removed, report = deduplicator.deduplicate(results, max_distance=10)
        self.assertEqual(removed, 1)
        self.assertEqual(kept[0]["confidence"], 90)
        self.assertEqual(len(report), 1)

    def test_same_name_twice_in_the_same_tile_is_two_real_places(self):
        results = [
            _result("Ca' Nova", 900.0, 500.0, 80, tile=(0, 0)),
            _result("Ca' Nova", 905.0, 500.0, 80, tile=(0, 0)),
        ]
        _, removed, _ = deduplicator.deduplicate(results, max_distance=70)
        self.assertEqual(removed, 0)

    def test_same_name_in_different_tiles_but_outside_their_overlap_is_kept(self):
        # il quadrante (0,1) inizia a x=800
        results = [
            _result("Molino", 780.0, 500.0, 80, tile=(0, 0)),
            _result("Molino", 810.0, 500.0, 80, tile=(0, 1)),
        ]
        _, removed, _ = deduplicator.deduplicate(results, max_distance=70)
        self.assertEqual(removed, 1)   # a 30 m l'uno dall'altro, a cavallo del bordo (800): plausibile doppione
        # entrambi ben dentro la parte che solo il quadrante (0,0) copre: (0,1) non può averli letti
        results[0]["x"] = 700.0
        results[1]["x"] = 760.0
        _, removed, _ = deduplicator.deduplicate(results, max_distance=70)
        self.assertEqual(removed, 0)

    def test_distance_threshold_is_respected(self):
        results = [
            _result("San Marino", 880.0, 500.0, 80, tile=(0, 0)),
            _result("San Marino", 915.0, 500.0, 80, tile=(0, 1)),
        ]
        self.assertEqual(deduplicator.deduplicate(results, max_distance=20)[1], 0)
        self.assertEqual(deduplicator.deduplicate(results, max_distance=70)[1], 1)

    def test_different_names_are_never_merged(self):
        results = [
            _result("Monte Alto", 900.0, 500.0, 80, tile=(0, 0)),
            _result("Monte Basso", 901.0, 500.0, 80, tile=(0, 1)),
        ]
        self.assertEqual(deduplicator.deduplicate(results, max_distance=70)[1], 0)

    def test_name_truncated_at_tile_border_keeps_the_complete_reading(self):
        results = [
            _result("Petrella", 985.0, 500.0, 95, tile=(0, 0)),
            _result("Petrella Massana", 990.0, 500.0, 70, tile=(0, 1)),
        ]
        kept, removed, _ = deduplicator.deduplicate(results, max_distance=10)
        self.assertEqual(removed, 1)
        self.assertEqual(kept[0]["toponimo"], "Petrella Massana")

    def test_one_misread_letter_is_still_the_same_name(self):
        self.assertTrue(deduplicator.texts_match("Petrella Massana", "Petrella Masana"))
        self.assertTrue(deduplicator.texts_match("Valdiceci", "VALDICECI"))
        self.assertFalse(deduplicator.texts_match("Monte Alto", "Monte Basso"))
        self.assertFalse(deduplicator.texts_match("Ca", "Casa Nova"))

    def test_custom_distance_function_is_used(self):
        results = [
            _result("Rimini", 900.0, 500.0, 80, tile=(0, 0)),
            _result("Rimini", 900.0001, 500.0, 80, tile=(0, 1)),
        ]
        far = lambda a, b: 500.0
        self.assertEqual(deduplicator.deduplicate(results, max_distance=20, distance_fn=far)[1], 0)


class TestCloudResponseParsing(unittest.TestCase):
    def test_parses_array_embedded_in_text(self):
        words = _parse_response('Ecco: [{"text": "Rimini", "x": 100, "y": 50, "confidence": 90}] fine')
        self.assertEqual(len(words), 1)
        self.assertEqual(words[0]["text"], "Rimini")
        self.assertEqual(words[0]["top"] + words[0]["height"] / 2, 50)

    def test_malformed_items_are_skipped_not_raised(self):
        raw = '["solo testo", {"text": "A", "x": null, "y": 1}, {"text": "B", "x": 5, "y": 6}, {"x": 1}]'
        words = _parse_response(raw)
        self.assertEqual([w["text"] for w in words], ["B"])

    def test_non_json_or_non_list_gives_empty(self):
        self.assertEqual(_parse_response("nessuna etichetta"), [])
        self.assertEqual(_parse_response("[troncato"), [])


class TestUsageTracker(unittest.TestCase):
    def test_cost_uses_model_price(self):
        tracker = UsageTracker()
        tracker.record_call("claude", "claude-opus-5-5", input_tokens=1_000_000, output_tokens=1_000_000)
        self.assertAlmostEqual(tracker.total_estimated_cost_usd(), 24.0)

    def test_error_count_sums_repeated_errors(self):
        tracker = UsageTracker()
        tracker.record_error("claude", "401")
        tracker.record_error("claude", "401")
        tracker.record_error("gpt", "timeout")
        self.assertEqual(tracker.error_count(), 3)


if __name__ == "__main__":
    unittest.main()
