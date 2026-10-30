# Complete guide — Settings, cartography, resolution, tiling and OCR

This guide covers **all the practical cases** you may meet when using Toponimi Extractor: how to read a WMS cartography, how to choose resolution and tile size, when to use Tesseract or the cloud, and how the new **🤖 AI DRIVEN** mode works, which automates all these choices with an agent that reasons and decides on its own.

> The plugin interface is currently in Italian. Italian UI labels are quoted below, with the English meaning in parentheses where it helps.

---

## Part 1 — The Settings dialog, field by field

### 1.1 Primary OCR engine

| Option | Meaning |
|---|---|
| **Tesseract locale** (local Tesseract, default) | Each tile is read first with Tesseract (free, offline). The cloud only steps in as a fallback if confidence is low. |
| **Solo cloud (Claude/GPT)** (cloud only) | Tesseract is skipped: every tile goes straight to the chosen cloud provider. The cloud fallback is always forced on in this case (if `use_tesseract=False`, `load_ocr_settings` in [ui/settings_dialog.py](ui/settings_dialog.py) enforces `use_cloud_fallback=True` regardless of the checkbox, otherwise no engine would be active). |

### 1.2 Tesseract

| Field | Meaning |
|---|---|
| **Executable path** | Path to `tesseract.exe`. Empty = system PATH. |
| **Language (Tesseract code)** | `ita` for Italian, `eng` for English, `lat` if available for Latin/ancient toponyms. |

### 1.3 Cloud fallback

| Field | Meaning |
|---|---|
| **Enable cloud fallback** | If off (with Tesseract as primary engine), the cloud is never called. |
| **Provider** | Claude, GPT or Both. |
| **Minimum confidence threshold** (default 60%) | Below this average Tesseract confidence, the cloud fallback is triggered on the tile. |
| **Environment variable / API key override** | Always prefer the environment variable: the override saved in the dialog ends up in plain text in `QSettings`. |
| **Claude / GPT model** | Cost/accuracy trade-off; price per 1M tokens shown in the interface. |

### 1.4 Tiles — size, overlap, resolution

| Field | Meaning |
|---|---|
| **Max tile size (px)** | `0` = automatic (max 1400px, a threshold chosen because Claude/GPT downscale images larger than ~1568px on the long side, losing detail on small text — see `OCR_FRIENDLY_TILE_PX` in [tasks/extraction_task.py](tasks/extraction_task.py)). |
| **Tile overlap (px)** (default 200) | Avoids cutting a toponym in half on the border between two tiles. |
| **Automatica** (Automatic) | Uses the resolution you were looking at the map with when you drew the area: what you see on screen is what gets read. If the area fits in a single tile it is still rendered at the maximum resolution that tile allows, so drawing with the map zoomed out does not worsen the reading of small areas. |
| **Scala QGIS** (QGIS scale) | You set a scale denominator (1:N); valid only for CRSs in meters. |
| **Risoluzione manuale** (Manual resolution) | Direct value in CRS units/px. |
| **Calibrazione automatica** (Automatic calibration) | Tests fixed scales (10000→250) with a Tesseract-only heuristic (confidence + word count), no AI involved — see [core/scale_calibrator.py](core/scale_calibrator.py). |
| **🤖 AI DRIVEN** (new) | An AI agent (Claude or GPT) looks at the test tile, reads the OCR output, and decides on its own resolution/tile/overlap/engine in at most 5 attempts. See Part 3. |

---

## Part 2 — Cartography, resolution and tiling cases

This is the mental map the AI DRIVEN agent uses internally (in its system prompt), and it is also the practical guide for manual choices.

### 2.1 Types of cartographic source and typical problems

| Map type | Typical problem | OCR symptom | Recommended solution |
|---|---|---|---|
| **Scanned historical map** (e.g. cadastre, old IGM) | Low native WMS resolution, yellowed paper, scan noise | Tesseract finds little or nothing even at "average" confidence | Increase resolution (lower meters_per_pixel); if that is not enough, switch to a cloud engine |
| **WMS with unreliable GetCapabilities** | The service declares a `ScaleHint`/resolution that does not reflect real text legibility | "Automatica" mode produces tiles where the text is too small, or huge and slow ones | Do not trust "Automatica": use QGIS scale, Calibration or AI DRIVEN |
| **Map with curved text along rivers/borders** | Letters not horizontally aligned, spaced along a curve | Tesseract often ignores the word completely or reads it as noise | A cloud vision engine (Claude/GPT) is needed: it reads the geometry of the lettering, not just text lines |
| **Decorative/historical italic font** | Ornate serifs, cartographic italics for rivers/mountains | Tesseract confuses similar letters (e.g. long "s"/"f") | Cloud engine; possibly increase resolution to give more stroke detail |
| **Elevation numbers over contour lines** | Small numbers, often overlapping black lines | OCR confuses digits with contour segments | Increase resolution; if it persists, cloud with a prompt that emphasizes numbers (the current prompt already explicitly asks for elevations/numbers) |
| **Repeated toponyms on tile borders** | Same name split between two adjacent tiles | Duplicates or words cut in half | Increase overlap_px (e.g. from 200 to 300+) |
| **Noisy background** (texture, print halftone, scan grain) | "Dotted" pattern or texture behind the text | OCR false positives (noise read as letters), generally low confidence | Cloud engine (more robust to visual noise); possibly smaller tiles to better isolate the context |
| **Areas without toponyms** (open sea, empty rural area) | No real reading problem: there is simply no text | 0 words found even with correct parameters | Not a calibration problem — it is normal; do not raise resolution endlessly chasing a result that does not exist |
| **Multilingual map / dialect or Latin toponyms** | Tesseract language does not match | Words read but transcribed incorrectly (not a bbox confidence problem, but a language dictionary one) | Change `tesseract_lang` (e.g. `ita+lat`) before touching resolution/engine |

### 2.2 How to choose the resolution (meters_per_pixel)

General rule: **the resolution must be fine enough for the smallest text on the map to be legible to the eye in the tile screenshot**, not fine enough to maximize detail in absolute terms (it costs more, tiles are heavier, and there is no benefit beyond a certain point).

- If a test tile shows the text as indistinct "blobs" even when zoomed in → resolution too low (meters_per_pixel too high): halve it and retry.
- If the text is legible to the eye but OCR still gets it wrong → the problem is NOT the resolution, it is the engine: switch to a cloud engine instead of continuing to raise the resolution (it would only waste time/cost).
- There is no universal value: it depends on the source (a rasterized-vector CTR 1:5000 needs different resolutions from a cadastre scanned at 300dpi).

### 2.3 How to choose the tile size

- **Tiles too large** (>1400-1568px on the long side): cloud vision engines internally downscale images above this threshold, so small text loses detail even BEFORE being read. Local rendering also becomes slower and more tiles fail by timeout on slow WMS services.
- **Tiles too small**: they multiply the number of calls (especially expensive in cloud mode) and increase the risk of cutting toponyms in half, even with overlap.
- Reasonable starting point: 768-1400px, then adjust according to how dense with text the tile is (maps very dense with small labels → prefer smaller tiles but more resolution, not the other way round).

### 2.4 How to choose the overlap

- Insufficient overlap → toponyms cut in half on the border between tiles, read as two different words or lost.
- Excessive overlap → more area reprocessed several times, risk of duplicates in the results table (manageable in manual review anyway, but better avoided).
- 150-250px is a good compromise for most scales; increase it if toponyms tend to be long (compound names, "San Giovanni in..." etc.).

### 2.5 How to choose the OCR engine

| Situation | Recommended engine |
|---|---|
| Printed, horizontal text, good contrast | Tesseract (free) |
| Curved/rotated/italic text | Cloud (Claude or GPT) |
| Noisy background, low-quality scan | Cloud |
| Huge number of tiles, limited budget | Tesseract + selective cloud fallback (confidence threshold) |
| A few very difficult tiles identified after review | Manual re-run of Claude from the results table, rather than changing the global strategy |

---

## Part 3 — 🤖 AI DRIVEN mode: how the agent works

### 3.1 Goal

To spare the user from manually choosing resolution, tile size, overlap and OCR engine. An AI agent (Claude or GPT, selectable in the settings) **looks at the image** of the test tile, **reads the OCR result** obtained on it, and **decides on its own** what to try, up to a maximum of **5 attempts**.

Implemented in [core/ai_agent_calibrator.py](core/ai_agent_calibrator.py), integrated in [toponimi_extractor.py](toponimi_extractor.py) through `_run_ai_driven_calibration`, and enabled by the "🤖 AI DRIVEN" radio button in [ui/settings_dialog.py](ui/settings_dialog.py).

### 3.2 Difference from the existing "Calibrazione automatica"

| | Automatic calibration (`scale_calibrator.py`) | AI DRIVEN (`ai_agent_calibrator.py`) |
|---|---|---|
| What it tries | Only predefined fixed scales (10000→250) | Any combination of resolution/tile/overlap/engine, decided dynamically |
| Who judges | Fixed numeric threshold (average confidence + word count) | The AI, looking at the image and the text read |
| Engine tested | Tesseract only | Tesseract, Claude or GPT, chosen by the AI turn by turn |
| "Continue or stop" decision | Automatic (first scale that passes the threshold) | The AI's, with explicit reasoning saved in the report |
| Cost | Free (Tesseract only) | Requires API calls (both for the cloud OCR test and for the agent's "brain") |

### 3.3 The agentic loop (chain of thought + tool use)

```
Turn 1:
  → render a test tile at the CENTER of the AOI (heuristic starting parameters)
  → OCR with the starting engine
  → the agent receives: tile image + text read + confidence + score
  → the agent reasons ("reasoning" field) and calls ONE of the two tools:
      • try_parameters(meters_per_pixel, tile_size_px, overlap_px, engine, reasoning)
      • accept_configuration(reasoning)

If try_parameters → Turn 2 with the new parameters requested by the AI, repeat.
If accept_configuration → loop ends, the LAST evaluated attempt is used.

After 5 attempts anyway: acceptance of the last one is forced (the system
prompt explicitly tells the AI so on the last turn).
```

The two **tools** exposed to the agent (full schema in `_TOOLS_ANTHROPIC`/`_TOOLS_OPENAI`):

- **`try_parameters`** — requests a new attempt with new parameters: `meters_per_pixel`, `tile_size_px`, `overlap_px`, `engine` (`tesseract`/`claude`/`gpt`), plus a mandatory `reasoning` field explaining why.
- **`accept_configuration`** — declares the last attempt good and stops the loop, with a `reasoning` explaining why it is adequate for the whole extraction.

The agent's **system prompt** (see `_SYSTEM_PROMPT` in the module) explicitly encodes the same domain guidelines as Part 2: empty image → lower meters_per_pixel; text legible to the eye but OCR wrong → change engine, not resolution; curved/italic text → prefer cloud; few words but high confidence → maybe it is an area without toponyms, consider accepting; look for the cheapest configuration that still reads well, not absolute perfection.

### 3.4 Automatic score (support only, not decisional)

Every attempt also receives a heuristic score (`AttemptResult.score`, 0-100) based on average confidence and number of words found, shown to the AI in the prompt as an objective reference. **The decision to continue or stop always remains the AI's**: the score is only a number the agent can use in its reasoning, not an automatic threshold that blocks or forces the loop.

### 3.5 What happens after calibration

At the end (explicit acceptance or 5th attempt reached), the plugin:

1. Shows the user a summary with the chosen resolution/tile/overlap/engine, the agent's full turn-by-turn reasoning, and the details of all attempts.
2. Automatically applies the chosen parameters to `settings` for the full extraction over the whole AOI:
   - `meters_per_pixel_override` and `max_tile_px_override` → used by `grid.build_grid` to generate the real tile grid.
   - `overlap_px` → the same overlap validated in the test.
   - The chosen engine becomes the primary engine for the whole extraction (`use_tesseract` set accordingly); if the agent chose a cloud engine, the cloud fallback stays active anyway as a safety net on individual tiles where that specific engine might fail.
3. Proceeds with `ExtractionTask` exactly as in the other modes — no difference in the rest of the pipeline (georeferencing, GeoPackage writing, manual review in `ResultsDock`).

### 3.6 If no attempt is "good"

If after 5 attempts the quality remains poor (objectively very difficult map, or an area without toponyms), the plugin proceeds anyway with the best configuration among those tried (the one accepted by the AI, or the last one if the model never explicitly called `accept_configuration`), and clearly flags it in the summary shown to the user — no silent block, no "surprise" extraction with poor parameters without warning.

### 3.7 Prerequisites and costs

- Requires a valid API key (Claude or GPT, according to the "AI DRIVEN: decision agent engine" choice) already configured in the **Cloud fallback** section of the settings.
- Each calibration costs up to 5 calls to the "brain" model (text + image) plus, if the agent chooses to test a cloud engine as OCR, additional calls to that engine on the test tile — all MUCH smaller and more circumscribed (a single tile, max 5 times) than the cost of the whole extraction, which may involve dozens of tiles.
- The recommended model for the agent's "brain" is the same one configured as the Claude/GPT model in the cloud settings (it reuses `claude_model`/`gpt_model`).

---

## Practical summary — which mode to choose

| Scenario | Recommended mode |
|---|---|
| You have no idea about the map, you want minimum effort | 🤖 **AI DRIVEN** |
| Map already known, parameters already tested in the past | Direct **QGIS scale** or **Manual resolution** |
| You want to stay free (no cloud calls) and the map is "normal" | **Automatic calibration** (Tesseract only) |
| WMS with already good native resolution and simple text | **Automatica** |
| Map almost entirely unreadable by Tesseract | **Cloud only** primary engine, possibly after a first AI DRIVEN run to also choose optimal resolution/tile |
| Only a few problematic tiles identified after extraction | Do not change global settings: use **"Rilancia con Claude sul quadrante"** (Re-run with Claude on the tile) in the results table |

---

## Part 4 — Cleaning duplicates caused by tile overlap

### The problem

Tiles overlap slightly through the **Tile overlap (px)** parameter (see 1.4), precisely so as not to cut a toponym in half on the border between adjacent tiles. The side effect is that a toponym falling in the overlap strip is almost always **read twice**: once as part of the left/upper tile, once as part of the right/lower tile — resulting in two almost coincident points with the same text in the results table.

### The solution: the "🧹 Rimuovi duplicati" button in the results table

In the results panel (`ResultsDock`), before the final "OK - Crea punti nel GeoPackage" (OK - Create points in the GeoPackage) button, there is a group **"Pulizia duplicati (overlap tra quadranti)"** (Duplicate cleanup, tile overlap) with the button **"🧹 Rimuovi duplicati (stesso testo, punti vicini)"** (Remove duplicates: same text, nearby points).

**Deduplication criterion** (implemented in `core/deduplicator.py`): two rows are considered the same duplicated toponym if:

1. **The text is the same**, compared tolerantly: case-insensitive, multiple spaces collapsed, accents/hyphens normalized (so "Póggio" and "Poggio", or "POGGIO BERNI" and "Poggio Berni", are correctly recognized as the same toponym even if read with different capitalization or accents by the two OCR passes).
2. **The position is close**, with a threshold computed automatically from the real resolution at which that point was rendered (not an arbitrary fixed value in meters, which would be wrong both for CRSs in degrees and for maps at very different scales): the threshold equals 3 times the size of one pixel at render time. A true overlap duplicate typically falls a few pixels apart between the two readings.

Among the duplicates found, **the one with the highest confidence is kept** (the more reliable of the readings), and the others are discarded.

**Important — what is NOT merged**: two toponyms with the same name but really far apart on the map (e.g. two "San Marino" in different places of the area) both remain, because the distance far exceeds the overlap threshold. Likewise, two different toponyms that happen to fall close together (e.g. "Monte Alto" and "Monte Basso" a few meters apart) are never merged, because the text is different.

**"Solo righe selezionate" (Selected rows only) option**: if you want to apply the cleanup only to a subset of the table (e.g. after re-running Claude on some tiles), select the rows of interest and tick this box before pressing the button.

**When to use it**: always, as the last step before confirming the creation of the points — especially with high overlap (>150px) or on maps where many toponyms fall near tile borders. The button can be pressed several times without risk: if it finds no duplicates, it simply says so without changing anything.
