# Checklist di revisione — Toponimi Extractor v0.7.1

Revisione statica completa del 2026-10-05 (tutti i 22 file `.py`, metadata, README, guida, zip).
Aggiornata dopo il primo giro di correzioni (v0.8.0): le voci spuntate sono state corrette e verificate con 17 test unitari e 14 verifiche headless nel Python di QGIS 4.2.2. Nessuna è stata ancora provata a mano in QGIS su un WMS reale.

**Attenzione**: i riferimenti di riga sono quelli della v0.7.1 revisionata, che è ancora la copia installata nel profilo `QGIS4/default`. La v0.8.0 corretta è solo nel sorgente e va copiata nel profilo per essere caricata. La copia nel profilo `QGIS3/default` è invece una v0.6.1 obsoleta: i riferimenti di riga di questa checklist non valgono per quella.

Legenda: **[C]** confermato leggendo il codice · **[V]** da verificare in QGIS (ipotesi fondata, non riprodotta)

---

## P0 — Bloccanti: risultati sbagliati o crash

- [x] **[C] La modalità "Automatica" (default) usa quasi sempre 1.0 unità CRS/px.**
  `get_capabilities_url` forza `VERSION=1.3.0` ([wms_capabilities.py:29](core/wms_capabilities.py:29)), ma `_find_layer_resolution` cerca `ScaleHint` ([wms_capabilities.py:116](core/wms_capabilities.py:116)), che esiste solo in WMS 1.1.1. Risultato: si ricade sempre su `DEFAULT_METERS_PER_PIXEL = 1.0`. Su una CTR il testo è illeggibile; su un CRS in gradi 1.0 = 1°/px e i tile escono da 0–1 px.
  → Leggere `MinScaleDenominator` (1.3.0) o partire dalla scala corrente del canvas; gestire esplicitamente i CRS geografici.

- [x] **[C] CRS del progetto ≠ CRS del layer → area e punti sbagliati.**
  Il rettangolo è in CRS progetto ([map_tool.py:27](qgis_integration/map_tool.py:27)), ma viene renderizzato come se fosse nel CRS del layer ([tile_renderer.py:88](qgis_integration/tile_renderer.py:88)) e salvato con `layer.crs()` ([toponimi_extractor.py:616](toponimi_extractor.py:616)). Nessuna trasformazione.
  → Trasformare l'AOI nel CRS del layer (o lavorare tutto in CRS progetto) e impostare il transform context.

- [x] **[C] Annullare l'estrazione mostra "Errore sconosciuto durante l'estrazione".**
  `run()` ritorna `False` su cancel ([extraction_task.py:56](tasks/extraction_task.py:56)) e `_on_task_finished` lo tratta come errore ([toponimi_extractor.py:535](toponimi_extractor.py:535)). Inoltre i risultati parziali (già pagati) vengono buttati.
  → Distinguere `isCanceled()`; offrire i risultati parziali.

- [x] **[C] `unload()` non annulla il task in corso.**
  Ricaricando il plugin o chiudendo QGIS durante un'estrazione, il task continua e richiama `_on_task_finished` su un plugin già smontato ([toponimi_extractor.py:240](toponimi_extractor.py:240)).
  → Cancellare il task e scollegare il callback in `unload()`.

- [ ] **[V] (mitigato) Il layer del thread principale viene usato nel worker thread.** *Ora il task lavora su un clone del layer, quindi rimuovere il layer dal progetto non causa più crash; il rendering dal worker thread resta da validare su un WMS reale.*
  `ExtractionTask` tiene `self.layer` e lo passa a `QgsMapRendererCustomPainterJob` da `run()` ([extraction_task.py:122](tasks/extraction_task.py:122)). Non è thread-safe; se l'utente rimuove il layer durante l'estrazione, crash. È una causa plausibile del bug "tile bianco" aggirato con retry + sleep in [tile_renderer.py:18](qgis_integration/tile_renderer.py:18).
  → Clonare il layer nel thread principale e passare il clone al task.

- [x] **[C] "Rilancia con Claude" cancella le correzioni manuali.**
  `_on_rerun_claude` non chiama `_sync_results_from_table()` prima di `load_results` ([results_dock.py:358](ui/results_dock.py:358)–403): testo corretto a mano e spunte "Includi" di tutte le altre righe vanno persi.

- [x] **[C] Tesseract non installato fa fallire tutta l'estrazione al primo tile.**
  La cascata cattura solo `TesseractNotAvailable` ([cascade.py:93](ocr/cascade.py:93)), ma `image_to_data` solleva `pytesseract.TesseractNotFoundError` / `TesseractError` (binario assente, lingua `ita` mancante) ([tesseract_engine.py:50](ocr/tesseract_engine.py:50)). Niente fallback cloud.

- [x] **[C] Scrittura GeoPackage senza controllo errori.**
  I valori di ritorno di `addFeatures` / `commitChanges` sono ignorati ([geopackage_writer.py:147](core/geopackage_writer.py:147), [:179](core/geopackage_writer.py:179)): il messaggio "N punti creati" appare anche se il commit fallisce (file bloccato, layer già aperto). `IOError` di `_create_layer` non è catturato. Ad ogni conferma lo stesso layer viene riaggiunto al progetto (duplicati in legenda).

---

## P1 — Lentezza e costi (il "perché è lento")

- [x] **[C] Retry cieco su qualunque eccezione: 14 s persi per tile per ogni errore permanente.**
  `_call_with_retry` ([cascade.py:35](ocr/cascade.py:35)) ritenta anche su 401 (chiave errata), 404 (modello inesistente), 400, e perfino su errori di parsing. Backoff 2+4+8 s, moltiplicato per ogni tile: 100 tile con chiave sbagliata = ~23 minuti di `sleep`. In più gli SDK ritentano già da soli (2 retry di default) → fino a 12 tentativi reali. Lo `sleep` non controlla `isCanceled()`.
  → Ritentare solo 429/5xx/rete; interrompere tutto dopo N fallimenti consecutivi.

- [ ] **[C] Tile elaborati uno alla volta.**
  Loop strettamente sequenziale render → OCR ([extraction_task.py:115](tasks/extraction_task.py:115)). Le chiamate cloud sono I/O puro: 4–8 chiamate in parallelo (pool di thread, rendering serializzato) è il guadagno maggiore disponibile.

- [x] **[C] Tile bianchi: 2 render extra + 1 s di attesa, poi inviati comunque all'OCR a pagamento.**
  Il retry "immagine vuota" ([tile_renderer.py:99](qgis_integration/tile_renderer.py:99)) scatta anche per tile legittimamente vuoti (mare, fuori copertura), e il tile vuoto va comunque al cloud ([extraction_task.py:127](tasks/extraction_task.py:127)).
  → Saltare l'OCR sui tile vuoti.

- [x] **[C] Nessun limite al numero di tile fuori dalla modalità AI DRIVEN.**
  Il tetto di 500 tile esiste solo in `ai_agent_calibrator`. In scala/manuale/automatica `build_grid` può generare migliaia di tile senza conferma né stima di costo ([extraction_task.py:90](tasks/extraction_task.py:90)).
  → Stima tile + costo e conferma prima di partire, in tutte le modalità.

- [x] **[C] Nessun controllo preliminare dei motori.**
  Con "Solo cloud" e chiave assente (o Tesseract mancante e fallback spento) l'estrazione renderizza tutta l'area per trovare zero toponimi con motore `none`.
  → Verificare binario/lingua Tesseract e chiavi API prima di avviare.

- [ ] **[C] Calibrazione AI e calibrazione scala bloccano l'interfaccia.**
  Entrambe girano nel thread principale ([toponimi_extractor.py:385](toponimi_extractor.py:385), [:446](toponimi_extractor.py:446)): fino a 6 render + 5 OCR + 5 chiamate all'agente con QGIS congelato. Il `progress_callback` riempie una lista mai mostrata ([toponimi_extractor.py:456](toponimi_extractor.py:456)).
  → Spostare in un `QgsTask` con dialogo di avanzamento.

- [ ] **[C] "Rilancia con Claude" è sincrono nel thread principale** ([results_dock.py:398](ui/results_dock.py:398)): UI bloccata per ogni quadrante, senza annulla né retry.

- [ ] **[C] Provider "Entrambi" raddoppia costi e risultati.**
  I risultati di Claude e GPT vengono concatenati ([cascade.py:125](ocr/cascade.py:125), [:163](ocr/cascade.py:163)) senza fusione: ogni toponimo compare due volte.

- [x] **[C] Proposta dell'agente rifiutata = iterazione sprecata.**
  Se la proposta supera il limite tile, il ciclo rifà render + OCR + chiamata con gli **stessi** parametri ([ai_agent_calibrator.py:723](core/ai_agent_calibrator.py:723)), e all'agente non viene mai detto del rifiuto (il commento a riga 735 afferma il contrario).

- [x] **[C] Client API ricreato ad ogni tile** ([claude_engine.py:120](ocr/claude_engine.py:120), [gpt_engine.py:84](ocr/gpt_engine.py:84)): nuovo handshake TLS per ogni chiamata. Riusare un client per estrazione.

- [x] **[C] GetCapabilities scaricato e parsato due volte** in modalità AI ([wms_capabilities.py:47](core/wms_capabilities.py:47), [:128](core/wms_capabilities.py:128)); il parametro `timeout_ms` non è usato da nessuna delle due funzioni.

- [x] **[C] Avvio di QGIS rallentato dal controllo dipendenze.**
  `initGui` importa davvero `anthropic`, `openai`, `pytesseract`, `PIL` e logga tutto `sys.path` ad ogni avvio ([dependency_check.py:21](core/dependency_check.py:21)), poi apre un popup modale se manca anche un solo pacchetto opzionale ([toponimi_extractor.py:145](toponimi_extractor.py:145)).
  → Usare `importlib.util.find_spec`, controllare solo i motori selezionati, e solo al primo uso.

- [ ] **[C] `pip install` blocca la UI** fino a 300 s per pacchetto ([dependency_check.py:105](core/dependency_check.py:105)).

- [ ] **[C] Tabella risultati lenta con molti toponimi.**
  Un `QCheckBox` come cell widget per riga ([results_dock.py:215](ui/results_dock.py:215)), ricostruito da zero ad ogni dedup/rilancio. Usare item checkable e sospendere gli aggiornamenti durante il caricamento.

- [ ] **[C] Cache tile mai limitata.**
  Tutti i PNG restano in `%TEMP%` finché il dock non viene chiuso; se QGIS va in crash non vengono mai rimossi. Nessuna pulizia delle cartelle `toponimi_extractor_*` orfane all'avvio.

- [ ] **[C] Dialogo destinazione: apre ogni sublayer del GeoPackage ad ogni tasto premuto** ([output_target_dialog.py:123](ui/output_target_dialog.py:123)).

---

## P1 — Errori di logica

- [x] **[C] Prezzi dei modelli Claude sbagliati → stima costi falsata** ([usage_tracker.py:12](core/usage_tracker.py:12)).
  Sonnet 5 è a $2/$10 (nel codice $3/$15); Opus 5.5 è a $4/$20 (nel codice $15/$75, quasi 4×). Haiku 4.5 è corretto. Il Sonnet corrente è `claude-sonnet-5-5`; i modelli GPT in lista (`gpt-4o`) vanno ricontrollati sul listino OpenAI.

- [x] **[C] Risposte troncate scartate in silenzio.**
  `stop_reason` non viene mai controllato. Sui modelli con thinking sempre attivo (Opus 5.5, Sonnet 5) il thinking consuma `max_tokens`: 1024 per l'agente ([ai_agent_calibrator.py:497](core/ai_agent_calibrator.py:497)) e 4096 per l'OCR ([claude_engine.py:123](ocr/claude_engine.py:123)). JSON troncato → `[]` → il tile perde tutti i toponimi senza alcun avviso.
  → Alzare `max_tokens`, gestire `max_tokens` e `refusal`.

- [ ] **[C] Agente che non chiama un tool = "accettazione implicita"** ([ai_agent_calibrator.py:692](core/ai_agent_calibrator.py:692)). Una risposta troncata o testuale fa accettare l'ultimo tentativo. Nota: su Opus 5.5 / Sonnet 5.5 `tool_choice` forzato dà 400, quindi serve `auto` + `strict: true` + un nuovo giro esplicito.

- [x] **[C] Parametri proposti dall'agente non validati** ([ai_agent_calibrator.py:716](core/ai_agent_calibrator.py:716)): `meters_per_pixel` ≤ 0 passa il controllo (`_estimate_tile_count` ritorna `None`) e fa esplodere la griglia; il range di scala del servizio non viene applicato.

- [x] **[C] L'overlap può superare la dimensione massima del tile.**
  `usable_px = max(max - 2*overlap, max//2, 1)` ([grid.py:29](core/grid.py:29)): con tile 256 e overlap 300 (combinazione ammessa dallo schema dell'agente) i tile reali sono 728 px, con ~80% di area rielaborata.
  → Limitare overlap a ≤ tile/4.

- [x] **[C] Parsing delle risposte cloud fragile** ([claude_engine.py:155](ocr/claude_engine.py:155), [gpt_engine.py:113](ocr/gpt_engine.py:113)): regex greedy `\[.*\]`, `item.get` su elementi non-dict e `float(None)` sollevano eccezioni (che innescano i retry a pagamento); coordinate non vincolate ai bordi del tile.
  → Usare structured outputs / tool con schema.

- [x] **[C] "Estrai toponimi" smette di funzionare dopo la prima estrazione.**
  `_start_extraction` usa `iface.activeLayer() or self.last_layer` ([toponimi_extractor.py:336](toponimi_extractor.py:336)): dopo il salvataggio il layer attivo è quello dei punti appena creato, quindi appare "Seleziona un layer WMS".
  → Preferire il layer su cui è stata disegnata l'AOI.

- [ ] **[C] La dedup non funziona con i motori cloud.**
  Soglia di 3 px ([deduplicator.py:37](core/deduplicator.py:37)), ma le coordinate di Claude/GPT sono approssimate (bbox stimato `len*10`). La guida dichiara il contrario.

- [ ] **[C] Costi della calibrazione AI non contabilizzati**: `_usage` scartato ([ai_agent_calibrator.py:475](core/ai_agent_calibrator.py:475), [:484](core/ai_agent_calibrator.py:484)), tracker non passato.

- [ ] **[C] Tesseract: parole singole e rumore.**
  Ogni parola diventa un punto ("San" e "Martino" separati); `min_confidence=0` tiene anche le letture spazzatura; nessun `--psm` per testo sparso. Raggruppare per `block_num`/`line_num` e filtrare.

- [ ] **[C] Destinazione e AOI lette al momento della conferma**, non legate al task ([toponimi_extractor.py:573](toponimi_extractor.py:573)): cambiando destinazione mentre il dock è aperto, i vecchi risultati finiscono nel nuovo file.

- [ ] **[C] Chiudere il dock con la X cancella la cache** ma lascia `self.results_dock` attivo ([results_dock.py:475](ui/results_dock.py:475)): "Rilancia con Claude" fallisce con un errore per quadrante.

- [ ] **[C] Layer con più sotto-layer WMS**: `_parse_wms_uri` tiene solo l'ultimo `layers=` ([wms_layer_utils.py:41](qgis_integration/wms_layer_utils.py:41)). XYZ e WMTS passano `is_wms_layer` ma non hanno GetCapabilities compatibile.

---

## P2 — Sicurezza e ambiente

- [ ] **[C] API key salvate in chiaro in `QSettings`** ([settings_dialog.py:330](ui/settings_dialog.py:330)). Usare `QgsAuthManager` o solo variabili d'ambiente.
- [ ] **[C] `sys.path.insert(0, …)` della cartella utente** ([toponimi_extractor.py:19](toponimi_extractor.py:19)) + `pip --target --ignore-installed`: le dipendenze transitive (pydantic, httpx, certifi, Pillow…) possono oscurare quelle di QGIS e rompere altri plugin. Usare `append` o una cartella dedicata al plugin.
- [ ] **[C] Installazione dipendenze solo Windows** (`python.exe`, `%APPDATA%`) ([dependency_check.py:46](core/dependency_check.py:46)): su Linux/macOS fallisce sempre.
- [ ] **[C] `requirements.txt` senza limiti superiori**: `anthropic` 1.x cambia backend HTTP (`httpx2`) e richiede Python ≥ 3.10.

---

## P2 — Deploy e packaging

- [ ] **[C] `toponimi_extractor.zip` è obsoleto e rotto.** Datato 28/09, contiene `ui/batch_dialog.py` (non più esistente) e manca di 10 moduli attuali (`ai_agent_calibrator`, `deduplicator`, `dependency_check`, `plugin_version`, `scale_calibrator`, `usage_tracker`, `control_panel`, `output_target_dialog`, `progress_dialog`, `results_dock`). Il README dice di installare proprio questo file.
- [ ] **[C] Nessuno script di build.** Serve un comando che generi lo zip escludendo `__pycache__/`, `test/`, lo zip stesso e i file di lavoro.
- [ ] **[C] 8 cartelle `__pycache__` nel sorgente**, nessun `.gitignore`, nessun repository git.
- [ ] **[C] Tre copie del plugin, sincronizzate a mano** (verificato il 05/10 con `diff -rq`):
  - `Documents\plugin_estrazione_toponimi` — sorgente, v0.7.1 (quello revisionato qui).
  - Profilo **QGIS4** `default` — v0.7.1, **identico al sorgente** (manca solo `test/`), plugin abilitato. È la copia realmente caricata (QGIS 4.2.2).
  - Profilo **QGIS3** `default` — **v0.6.1, obsoleta**: 9 file diversi, manca `deduplicator.py`. Non risulta abilitata né mai caricata (nessun `.pyc`). La 0.7.1 non ha quindi mai girato su QGIS 3.44.
  → Sostituire le copie con un collegamento (junction) alla cartella sorgente o con uno script di deploy; rimuovere o aggiornare la copia QGIS3.
- [ ] **[C] Le ultime modifiche non sono mai state eseguite in QGIS.** Nella copia QGIS4, `tile_renderer.py` e `results_dock.py` sono più recenti dei loro `.pyc` e `deduplicator.py` non ne ha: il retry "tile bianco" e il bottone "Rimuovi duplicati" sono stati copiati ma mai caricati. Vanno provati prima del rilascio.
- [ ] **[C] `metadata.txt`**: `tracker`, `repository`, `homepage` vuoti (obbligatori per il repository ufficiale); manca `changelog`; `experimental=True`; l'`about` cita un "terzo livello cloud" inesistente.
- [ ] **[C] Manca il file `LICENSE`** (GPL richiesta per i plugin QGIS).
- [ ] **[V] `qgisMinimumVersion=3.28` non verificato**: `QgsField(name, QMetaType.Type)` ([geopackage_writer.py:48](core/geopackage_writer.py:48)) è l'API introdotta in 3.38, e la 0.7.1 non è mai stata caricata su QGIS 3 (vedi sopra). Testare su 3.44 installato e dichiarare come minimo la versione più vecchia realmente provata.
- [ ] **[V] QGIS 4: il plugin si carica (4.2.2), ma restano enum non qualificati** (`QgsWkbTypes.PolygonGeometry`, `QgsWkbTypes.Point`, `Qgis.Info/Warning/Critical`, `QgsVectorFileWriter.NoError`, `QgsUnitTypes.DistanceMeters`) su percorsi da provare uno per uno: disegno AOI, salvataggio GeoPackage, calibrazione AI.
- [ ] **[C] README non allineato**: descrive due icone e la scelta del GeoPackage dopo "Estrai toponimi"; non cita pannello, dock risultati, dedup, AI DRIVEN, calibrazione.
- [ ] **[C] Tutte le stringhe UI sono in italiano fisso**, senza `tr()`.

---

## P3 — Pulizia del codice

**Codice morto**
- [ ] `get_map_units_per_pixel` e import `QgsRasterLayer`, `Qgis` ([wms_layer_utils.py:3](qgis_integration/wms_layer_utils.py:3), [:49](qgis_integration/wms_layer_utils.py:49))
- [ ] `PRICING_PER_MILLION_TOKENS` ([usage_tracker.py:44](core/usage_tracker.py:44))
- [x] `import os` e `/ 1.0` ([ai_agent_calibrator.py:40](core/ai_agent_calibrator.py:40), [:399](core/ai_agent_calibrator.py:399))
- [x] Ramo `tiles_total == 0` irraggiungibile ([extraction_task.py:105](tasks/extraction_task.py:105)): `build_grid` ritorna sempre ≥ 1 tile
- [ ] `total_words_found`, chiavi `image_format` / `crs` di `get_wms_params`, parametro `log_callback` mai passato
- [ ] `replace("'", "'")` ripetuto due volte è un no-op ([deduplicator.py:46](core/deduplicator.py:46)): gli apostrofi tipografici sono andati persi nel sorgente
- [ ] `action_run_batch`: nome residuo del vecchio `batch_dialog`

**Duplicazioni**
- [ ] `_PROMPT` e `_parse_response` identici in `claude_engine.py` e `gpt_engine.py`
- [ ] Blocchi Claude/GPT quasi identici in `cascade.py` (righe 109–147 e 149–185)
- [ ] Costruzione del dict risultato duplicata in [extraction_task.py:146](tasks/extraction_task.py:146) e [results_dock.py:450](ui/results_dock.py:450)
- [ ] Tile di prova centrato: `_render_and_ocr` e `scale_calibrator._center_test_extent`
- [x] `_estimate_tile_count` ricopia la formula di `build_grid`
- [ ] `open_or_create_layer` / `open_or_create_aoi_layer`
- [ ] `LOG_TAG` ridefinito in 6 file; `TEST_TILE_SIZE_PX` con due valori diversi (512 e 768)

**Struttura**
- [x] `core` importa da `ui`: `scale_to_meters_per_pixel` vive in `settings_dialog.py` ([ai_agent_calibrator.py:49](core/ai_agent_calibrator.py:49), [scale_calibrator.py:15](core/scale_calibrator.py:15)). Spostarla in `core`.
- [ ] `run_ai_driven_calibration` è una funzione da 240 righe; `ai_agent_calibrator.py` è 926 righe, per metà commenti
- [ ] Commenti-cronaca ("bug osservato: …") ovunque: spostarli in un `CHANGELOG.md` e lasciare solo il perché attuale
- [ ] Import dentro le funzioni (`shutil`, `tempfile`, `base64`, `QgsPointXY` ad ogni movimento del mouse in [map_tool.py:62](qgis_integration/map_tool.py:62))
- [ ] `self.canvas = canvas` oscura il metodo `QgsMapTool.canvas()` ([map_tool.py:18](qgis_integration/map_tool.py:18)); il rubber band non viene rimosso dalla scena
- [ ] Docstring non aggiornate (`plugin_version.py` cita "0.2.2"; `load_results` non elenca i campi tile)

**Incoerenze**
- [x] Overlap: spinbox 0–256 ([settings_dialog.py:212](ui/settings_dialog.py:212)), schema agente 50–300, guida "300+"
- [ ] Default modello: `"claude-sonnet-5"` nella firma di `recognize_words`, Haiku in `default_model()`
- [ ] Colori fissi `#555` illeggibili con tema scuro; dialogo impostazioni senza scroll area
- [x] `Image.open` senza `with` ([tesseract_engine.py:46](ocr/tesseract_engine.py:46))

---

## P3 — Test

- [ ] Coperti solo `georeference` e 2 casi di `grid`. Mancano test per moduli puri già testabili senza QGIS: `deduplicator`, `usage_tracker`, `_parse_response`, `_parse_capabilities`, `_estimate_tile_count`, `_replace_with_scope`.
- [x] `try/except ImportError: pass` attorno a `TestGrid` ([test_core.py:67](test/test_core.py:67)) nasconde i test invece di farli fallire.
- [ ] Nessun test per: overlap > tile/4, CRS in gradi, AOI più piccola di un tile, annullamento.
- [ ] Nessun linter (`ruff`/`pyflakes`) né CI.

---

## Ordine consigliato

1. P0 (correttezza: modalità automatica, CRS, cancel, unload, thread, perdita modifiche)
2. Retry mirato + controllo preliminare motori + limite tile con conferma
3. Parallelizzare le chiamate cloud e saltare i tile vuoti
4. Prezzi/modelli, `stop_reason`, validazione parametri agente
5. Script di build, zip nuovo, `.gitignore`, git, LICENSE, metadata
6. Pulizia P3 e test
