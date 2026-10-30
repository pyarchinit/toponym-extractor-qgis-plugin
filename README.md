# Toponimi Extractor — QGIS plugin

Automatic extraction of place names (toponyms) and text labels from a raster
WMS layer. You select an area, the plugin splits it into tiles at the
service's optimal resolution, and recognizes the text with a cascading OCR
pipeline (local Tesseract + fallback to Claude/GPT vision models). The output
is a GeoPackage point layer with one point for every recognized toponym,
centered on the text itself (not on the tile that contains it).

> The plugin user interface is currently in Italian. Italian UI labels are
> quoted in this documentation, with the English meaning in parentheses.

## Features

- **Seleziona area (Select area)**: interactive tool to draw a rectangle
  (AOI) on the map, over an active raster WMS layer.
- **Estrai toponimi (Extract toponyms)**: splits the area into tiles sized
  according to the limits declared by the WMS service (`GetCapabilities`),
  runs OCR on each tile in a background task (the QGIS UI is not blocked) and
  writes the results to a GeoPackage point layer that is automatically added
  to the project.
- **Settings**: choice of the primary OCR engine (local Tesseract or cloud
  only), Tesseract language, confidence threshold below which the cloud
  fallback is triggered, cloud provider (Claude, GPT, or both), optional
  overrides for tile size and resolution, and an AI-driven calibration mode.

See [SETTINGS_GUIDE.md](SETTINGS_GUIDE.md) for a complete guide to the
settings, resolution, tiling and OCR engine choices.

## Cascading OCR pipeline

1. **Local Tesseract** (if enabled): word-by-word recognition with bounding
   boxes, so that each toponym is georeferenced separately even when a tile
   contains many of them.
2. **Cloud fallback** (Claude and/or GPT vision): triggered automatically if
   Tesseract finds no text, or if the average confidence of the words found
   is below the configured threshold. API keys are read from the
   `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` environment variables, with the
   option to override them in the plugin settings.

Every result saved in the GeoPackage includes: text, the OCR engine that
produced it, confidence, row/column of the source tile, and the estimated
text rotation.

## Installation in QGIS

The plugin folder must be named with a valid Python module name (no hyphens),
for example `toponimi_extractor`.

1. Clone or download this repository and place its contents in a folder named
   `toponimi_extractor` inside your QGIS plugins directory:
   - Windows: `%APPDATA%\QGIS\QGIS3\profiles\default\python\plugins\`
     (use `QGIS4` instead of `QGIS3` for QGIS 4)
   - Linux: `~/.local/share/QGIS/QGIS3/profiles/default/python/plugins/`
   - macOS: `~/Library/Application Support/QGIS/QGIS3/profiles/default/python/plugins/`
2. Restart QGIS, then enable the plugin in **Plugins → Manage and Install
   Plugins → Installed**.
3. Two toolbar icons will appear ("Seleziona area", "Estrai toponimi") and a
   "Toponimi Extractor" menu entry with the settings.

Alternatively, zip the `toponimi_extractor` folder (excluding `__pycache__`)
and use **Install from ZIP** in the plugin manager.

## Python dependencies

On Windows, the "embedded" Python of QGIS is separate from any system Python.
Install the dependencies in that environment, typically through the OSGeo4W
Shell:

```
py3_env
python -m pip install -r requirements.txt
```

or individually:

```
python -m pip install pytesseract Pillow anthropic openai
```

- **Tesseract OCR** (local engine): must be installed separately as a system
  binary, then set in the plugin settings if it is not in the system PATH. See
  the [installation instructions](https://github.com/tesseract-ocr/tesseract#installing-tesseract)
  and the [ready-made installers](https://tesseract-ocr.github.io/tessdoc/Installation.html).
  For Italian maps make sure the Tesseract `ita` language pack is installed.
- **ANTHROPIC_API_KEY** / **OPENAI_API_KEY**: environment variables for the
  cloud fallback engines. You can also enter them as a direct override in the
  plugin settings (stored locally through `QSettings`).

## Usage

1. Load a raster WMS layer in the project and select it in the Layers panel.
2. Click **Seleziona area** (Select area) and draw a rectangle on the map.
3. Click **Estrai toponimi** (Extract toponyms), choose the output GeoPackage
   path and start the extraction.
4. When it finishes, the point layer with the toponyms found is added to the
   project.

## Development

For iterative development the **Plugin Reloader** plugin is recommended (it
reloads the code without restarting QGIS after every change). Tests of the
pure logic (grid, georeferencing) do not require QGIS and are run with:

```
python test/test_core.py -v
```
