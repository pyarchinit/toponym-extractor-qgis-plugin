# Guida esaustiva — Impostazioni, cartografia, risoluzione, tiling e OCR

Questa guida copre **tutte le casistiche pratiche** che puoi incontrare usando Toponimi Extractor: come leggere una cartografia WMS, come scegliere risoluzione e dimensione dei quadranti, quando usare Tesseract o il cloud, e come funziona la nuova modalità **🤖 AI DRIVEN**, che automatizza tutte queste scelte con un agente che ragiona e decide da solo.

---

## Parte 1 — Il dialogo Impostazioni, campo per campo

### 1.1 Motore OCR primario

| Opzione | Significato |
|---|---|
| **Tesseract locale** (default) | Ogni tile viene letto prima con Tesseract (gratuito, offline). Il cloud interviene solo come fallback se la confidenza è bassa. |
| **Solo cloud (Claude/GPT)** | Tesseract viene saltato: ogni tile va direttamente al provider cloud scelto. Il fallback cloud è forzato sempre attivo in questo caso (se `use_tesseract=False`, `load_ocr_settings` in [ui/settings_dialog.py](ui/settings_dialog.py) impone `use_cloud_fallback=True` a prescindere dal checkbox, altrimenti non ci sarebbe alcun motore attivo). |

### 1.2 Tesseract

| Campo | Significato |
|---|---|
| **Percorso eseguibile** | Path a `tesseract.exe`. Vuoto = PATH di sistema. |
| **Lingua (codice Tesseract)** | `ita` per italiano, `eng` per inglese, `lat` se disponibile per toponimi latini/antichi. |

### 1.3 Fallback cloud

| Campo | Significato |
|---|---|
| **Abilita fallback cloud** | Se spento (con motore primario Tesseract), il cloud non viene mai chiamato. |
| **Provider** | Claude, GPT o Entrambi. |
| **Soglia confidenza minima** (default 60%) | Sotto questa confidenza media di Tesseract scatta il fallback cloud sul tile. |
| **Variabile d'ambiente / Override API key** | Preferisci sempre la variabile d'ambiente: l'override salvato nel dialogo finisce in chiaro in `QSettings`. |
| **Modello Claude / GPT** | Scelta costo/precisione, prezzo per 1M token mostrato in interfaccia. |

### 1.4 Quadranti — dimensione, overlap, risoluzione

| Campo | Significato |
|---|---|
| **Dimensione max tile (px)** | `0` = automatico (max 1400px, soglia scelta perché Claude/GPT ridimensionano immagini più grandi di ~1568px sul lato lungo, perdendo dettaglio sul testo piccolo — vedi `OCR_FRIENDLY_TILE_PX` in [tasks/extraction_task.py](tasks/extraction_task.py)). |
| **Sovrapposizione tile (px)** (default 200) | Evita di tagliare un toponimo a metà sul confine tra due quadranti. |
| **Automatica** | Usa la risoluzione a cui stavi guardando la mappa quando hai disegnato l'area: quello che vedi a video è quello che viene letto. Se l'area sta in un solo quadrante viene comunque resa alla massima risoluzione che quel quadrante consente, quindi disegnare a mappa rimpicciolita non peggiora la lettura di aree piccole. |
| **Scala QGIS** | Imposti un denominatore di scala (1:N); valido solo per CRS in metri. |
| **Risoluzione manuale** | Valore diretto in unità CRS/px. |
| **Calibrazione automatica** | Testa scale fisse (10000→250) con sola euristica Tesseract (confidenza + conteggio parole), nessuna AI coinvolta — vedi [core/scale_calibrator.py](core/scale_calibrator.py). |
| **🤖 AI DRIVEN** (nuovo) | Un agente AI (Claude o GPT) guarda il tile di prova, legge l'OCR, e decide da solo risoluzione/tile/overlap/motore in max 5 tentativi. Vedi Parte 3. |

---

## Parte 2 — Casistiche di cartografia, risoluzione e tiling

Questa è la mappa mentale che l'agente AI DRIVEN usa internamente (nel suo system prompt), ed è anche la guida pratica per le scelte manuali.

### 2.1 Tipologie di fonte cartografica e problemi tipici

| Tipo di mappa | Problema tipico | Sintomo nell'OCR | Soluzione consigliata |
|---|---|---|---|
| **Mappa storica scansionata** (es. catasto, IGM antico) | Risoluzione nativa del WMS bassa, carta ingiallita, rumore di scansione | Tesseract trova poco o niente anche a confidenza "media" | Aumentare risoluzione (meters_per_pixel più basso); se non basta, passare a motore cloud |
| **WMS con GetCapabilities inaffidabile** | Il servizio dichiara uno `ScaleHint`/risoluzione che non riflette la leggibilità reale del testo | Modalità "Automatica" produce tile dove il testo è troppo piccolo o enormi e lenti | Non fidarsi di "Automatica": usare Scala QGIS, Calibrazione o AI DRIVEN |
| **Mappa con testo curvo lungo fiumi/confini** | Lettere non allineate orizzontalmente, spaziate lungo una curva | Tesseract spesso ignora completamente la parola o la legge come rumore | Serve un motore cloud vision (Claude/GPT): leggono la geometria della scritta, non solo righe di testo |
| **Font decorativo/corsivo storico** | Grazie, serif ornati, corsivo cartografico per fiumi/monti | Tesseract confonde lettere simili (es. "f"/"s" lunga) | Motore cloud; eventualmente aumentare risoluzione per dare più dettaglio ai tratti |
| **Quote numeriche sovrapposte a curve di livello** | Numeri piccoli, spesso sovrapposti a linee nere | OCR confonde cifre con segmenti di curva | Aumentare risoluzione; se persiste, cloud con prompt che enfatizza i numeri (il prompt attuale già chiede esplicitamente quote/numeri) |
| **Toponimi ripetuti sui bordi tra tile** | Stesso nome diviso a metà tra due quadranti adiacenti | Duplicati o parole tagliate a metà | Aumentare overlap_px (es. da 200 a 300+) |
| **Sfondo rumoroso** (texture, retinatura di stampa, igrometria della scansione) | "Puntinato" o texture dietro il testo | Falsi positivi OCR (rumore letto come lettere), confidenza bassa generalizzata | Motore cloud (più robusto al rumore visivo); eventualmente tile più piccoli per isolare meglio il contesto |
| **Aree senza toponimi** (mare aperto, zona rurale vuota) | Nessun problema reale di lettura: semplicemente non c'è testo | 0 parole trovate anche con parametri corretti | Non è un problema di calibrazione — è normale; non alzare risoluzione all'infinito inseguendo un risultato che non esiste |
| **Mappa multilingue / toponimi in dialetto o latino** | Lingua Tesseract non corrispondente | Parole lette ma trascritte in modo errato (non problema di confidenza bbox, ma di dizionario linguistico) | Cambiare `tesseract_lang` (es. `ita+lat`) prima di toccare risoluzione/motore |

### 2.2 Come decidere la risoluzione (meters_per_pixel)

Regola generale: **la risoluzione deve essere abbastanza fine perché il testo più piccolo della mappa sia leggibile a occhio nello screenshot del tile**, non abbastanza fine da massimizzare il dettaglio in assoluto (costa di più, tile più pesanti, nessun beneficio oltre un certo punto).

- Se un test tile mostra il testo come "macchie" indistinte anche con lo zoom → risoluzione troppo bassa (valore meters_per_pixel troppo alto): dimezzalo e riprova.
- Se il testo è leggibile a occhio ma OCR comunque sbaglia → il problema NON è la risoluzione, è il motore: passa a un motore cloud invece di continuare ad aumentare la risoluzione (sprecherebbe solo tempo/costo).
- Non esiste un valore universale: dipende dalla fonte (una CTR 1:5000 vettoriale-rasterizzata ha bisogno di risoluzioni diverse da un catasto scansionato a 300dpi).

### 2.3 Come decidere la dimensione del tile

- **Tile troppo grandi** (>1400-1568px lato lungo): i motori cloud vision ridimensionano internamente le immagini oltre questa soglia, quindi il testo piccolo perde dettaglio PRIMA ancora di essere letto. Anche il rendering locale diventa più lento e più tile falliscono per timeout sui WMS lenti.
- **Tile troppo piccoli**: moltiplicano il numero di chiamate (soprattutto costose in modalità cloud) e aumentano il rischio di tagliare toponimi a metà, anche con overlap.
- Punto di partenza ragionevole: 768-1400px, poi aggiustare in base a quanto denso di testo è il tile (mappe molto dense di etichette piccole → preferire tile più piccoli ma più risoluzione, non il contrario).

### 2.4 Come decidere l'overlap

- Overlap insufficiente → toponimi tagliati a metà sul confine tra quadranti, letti come due parole diverse o persi.
- Overlap eccessivo → più area ri-elaborata più volte, rischio di duplicati nella tabella risultati (gestibile comunque in revisione manuale, ma meglio evitarlo).
- 150-250px è un buon compromesso per la maggior parte delle scale; aumentare se i toponimi tendono a essere lunghi (nomi composti, "San Giovanni in..." ecc.).

### 2.5 Come scegliere il motore OCR

| Situazione | Motore consigliato |
|---|---|
| Testo stampato, orizzontale, buon contrasto | Tesseract (gratuito) |
| Testo curvo/ruotato/corsivo | Cloud (Claude o GPT) |
| Sfondo rumoroso, scansione di bassa qualità | Cloud |
| Volume enorme di tile, budget limitato | Tesseract + fallback cloud selettivo (soglia confidenza) |
| Pochi quadranti molto difficili individuati dopo revisione | Rilancio manuale di Claude dalla tabella risultati, non cambio della strategia globale |

---

## Parte 3 — Modalità 🤖 AI DRIVEN: come funziona l'agente

### 3.1 Obiettivo

Evitare che l'utente debba scegliere manualmente risoluzione, dimensione tile, overlap e motore OCR. Un agente AI (Claude o GPT, a scelta nelle impostazioni) **guarda l'immagine** del tile di prova, **legge il risultato OCR** ottenuto su di esso, e **decide da solo** cosa provare, fino a un massimo di **5 tentativi**.

Implementato in [core/ai_agent_calibrator.py](core/ai_agent_calibrator.py), integrato in [toponimi_extractor.py](toponimi_extractor.py) tramite `_run_ai_driven_calibration`, attivabile dal radio button "🤖 AI DRIVEN" in [ui/settings_dialog.py](ui/settings_dialog.py).

### 3.2 Differenza rispetto alla "Calibrazione automatica" esistente

| | Calibrazione automatica (`scale_calibrator.py`) | AI DRIVEN (`ai_agent_calibrator.py`) |
|---|---|---|
| Cosa prova | Solo scale fisse predefinite (10000→250) | Qualsiasi combinazione di risoluzione/tile/overlap/motore, decisa dinamicamente |
| Chi giudica | Soglia numerica fissa (confidenza media + conteggio parole) | L'AI, guardando l'immagine e il testo letto |
| Motore testato | Solo Tesseract | Tesseract, Claude o GPT, a scelta dell'AI turno per turno |
| Decisione "continuare o fermarsi" | Automatica (prima scala che supera la soglia) | Dell'AI, con ragionamento esplicito salvato nel report |
| Costo | Gratuito (solo Tesseract) | Richiede chiamate API (sia per il test OCR cloud sia per il "cervello" dell'agente) |

### 3.3 Il ciclo agentico (chain of thought + tool use)

```
Turno 1:
  → render tile di prova al CENTRO dell'AOI (parametri di partenza euristici)
  → OCR con il motore di partenza
  → l'agente riceve: immagine del tile + testo letto + confidenza + punteggio
  → l'agente ragiona (campo "reasoning") e chiama UNO dei due tool:
      • try_parameters(meters_per_pixel, tile_size_px, overlap_px, engine, reasoning)
      • accept_configuration(reasoning)

Se try_parameters → Turno 2 con i nuovi parametri richiesti dall'AI, ripete.
Se accept_configuration → ciclo terminato, si usa l'ULTIMO tentativo valutato.

Dopo 5 tentativi comunque: si forza l'accettazione dell'ultimo (il prompt
di sistema lo comunica esplicitamente all'AI all'ultimo turno).
```

I due **tool** esposti all'agente (schema completo in `_TOOLS_ANTHROPIC`/`_TOOLS_OPENAI`):

- **`try_parameters`** — chiede un nuovo tentativo con nuovi parametri: `meters_per_pixel`, `tile_size_px`, `overlap_px`, `engine` (`tesseract`/`claude`/`gpt`), più un campo `reasoning` obbligatorio che spiega perché.
- **`accept_configuration`** — dichiara buono l'ultimo tentativo e ferma il ciclo, con un `reasoning` che spiega perché è adeguato per l'intera estrazione.

Il **system prompt** dell'agente (vedi `_SYSTEM_PROMPT` nel modulo) codifica esplicitamente le stesse linee guida di dominio della Parte 2: immagine vuota → abbassa meters_per_pixel; testo leggibile a occhio ma OCR sbaglia → cambia motore, non risoluzione; testo curvo/corsivo → preferisci cloud; poche parole ma alta confidenza → forse è un'area senza toponimi, valuta di accettare; cerca la configurazione più economica che legge comunque bene, non la perfezione assoluta.

### 3.4 Punteggio automatico (solo di supporto, non decisionale)

Ogni tentativo riceve anche un punteggio euristico (`AttemptResult.score`, 0-100) basato su confidenza media e numero di parole trovate, mostrato all'AI nel prompt come riferimento oggettivo. **La decisione di continuare o fermarsi resta sempre dell'AI**: il punteggio è solo un numero che l'agente può usare nel suo ragionamento, non una soglia automatica che blocca o forza il ciclo.

### 3.5 Cosa succede dopo la calibrazione

Al termine (accettazione esplicita o 5° tentativo raggiunto), il plugin:

1. Mostra un riepilogo all'utente con risoluzione/tile/overlap/motore scelti, il ragionamento completo dell'agente turno per turno, e il dettaglio di tutti i tentativi.
2. Applica automaticamente i parametri scelti a `settings` per l'estrazione completa sull'intera AOI:
   - `meters_per_pixel_override` e `max_tile_px_override` → usati da `grid.build_grid` per generare la griglia reale di quadranti.
   - `overlap_px` → stesso overlap validato nel test.
   - Il motore scelto diventa il motore primario per tutta l'estrazione (`use_tesseract` impostato di conseguenza); se l'agente ha scelto un motore cloud, il fallback cloud resta comunque attivo come rete di sicurezza sui singoli tile dove quel motore specifico dovesse fallire.
3. Procede con `ExtractionTask` esattamente come nelle altre modalità — nessuna differenza nel resto della pipeline (georeferenziazione, scrittura GeoPackage, revisione manuale in `ResultsDock`).

### 3.6 Se nessun tentativo è "buono"

Se dopo 5 tentativi la qualità resta scarsa (mappa oggettivamente molto difficile, o area senza toponimi), il plugin procede comunque con la migliore configurazione tra quelle provate (quella accettata dall'AI, o l'ultima se il modello non ha mai chiamato esplicitamente `accept_configuration`), e lo segnala chiaramente nel riepilogo mostrato all'utente — nessun blocco silenzioso, nessuna estrazione "a sorpresa" con parametri scadenti senza avviso.

### 3.7 Prerequisiti e costi

- Richiede una API key valida (Claude o GPT, secondo la scelta "AI DRIVEN: motore agente decisionale") già configurata nella sezione **Fallback cloud** delle impostazioni.
- Ogni calibrazione costa fino a 5 chiamate al modello "cervello" (testo + immagine) più, se l'agente sceglie di testare un motore cloud come OCR, chiamate aggiuntive a quel motore sul tile di prova — tutte MOLTO più piccole e circoscritte (un solo tile, max 5 volte) rispetto al costo dell'intera estrazione, che può coinvolgere decine di tile.
- Il modello consigliato per il "cervello" dell'agente è lo stesso configurato come modello Claude/GPT nelle impostazioni cloud (riusa `claude_model`/`gpt_model`).

---

## Riepilogo pratico — quale modalità scegliere

| Scenario | Modalità consigliata |
|---|---|
| Non hai idea della mappa, vuoi il minimo sforzo | 🤖 **AI DRIVEN** |
| Mappa già nota, parametri già testati in passato | **Scala QGIS** o **Risoluzione manuale** diretta |
| Vuoi restare gratuito (no chiamate cloud) e la mappa è "normale" | **Calibrazione automatica** (solo Tesseract) |
| WMS con risoluzione nativa già buona e testo semplice | **Automatica** |
| Mappa quasi interamente illeggibile da Tesseract | Motore primario **Solo cloud**, eventualmente dopo un primo giro AI DRIVEN per scegliere anche risoluzione/tile ottimali |
| Solo pochi quadranti problematici individuati dopo l'estrazione | Non cambiare impostazioni globali: usa **"Rilancia con Claude sul quadrante"** nella tabella risultati |

---

## Parte 4 — Pulizia duplicati da overlap tra quadranti

### Il problema

I quadranti (tile) si sovrappongono leggermente tramite il parametro **Sovrapposizione tile (px)** (vedi 1.4), apposta per non tagliare un toponimo a metà sul confine tra due tile adiacenti. L'effetto collaterale è che un toponimo che cade nella fascia di overlap viene quasi sempre **letto due volte**: una come parte del tile a sinistra/sopra, una come parte del tile a destra/sotto — risultando in due punti quasi coincidenti con lo stesso testo nella tabella risultati.

### La soluzione: bottone "🧹 Rimuovi duplicati" nella tabella risultati

Nel pannello risultati (`ResultsDock`), prima del bottone finale "OK - Crea punti nel GeoPackage", c'è un gruppo **"Pulizia duplicati (overlap tra quadranti)"** con il bottone **"🧹 Rimuovi duplicati (stesso testo, punti vicini)"**.

**Criterio di deduplicazione** (implementato in `core/deduplicator.py`), due righe sono considerate lo stesso toponimo duplicato se:

1. **Il testo è uguale**, confrontato in modo tollerante: case-insensitive, spazi multipli collassati, accenti/trattini normalizzati (così "Póggio" e "Poggio", o "POGGIO BERNI" e "Poggio Berni", vengono correttamente riconosciuti come lo stesso toponimo anche se letti con maiuscole o accenti diversi dai due passaggi OCR).
2. **La posizione è vicina**, con una soglia calcolata automaticamente dalla risoluzione reale con cui quel punto è stato renderizzato (non un valore fisso arbitrario in metri, che sarebbe sbagliato sia per CRS in gradi sia per mappe a scale molto diverse tra loro): la soglia è pari a 3 volte la dimensione di un pixel al momento del rendering. Un vero duplicato da overlap cade tipicamente a pochi pixel di distanza tra le due letture.

Tra i duplicati trovati, viene **mantenuto quello con confidenza più alta** (la lettura più affidabile delle due), scartando le altre.

**Importante — cosa NON viene unito**: due toponimi con lo stesso nome ma realmente distanti sulla mappa (es. due "San Marino" in punti diversi dell'area) restano entrambi, perché la distanza supera ampiamente la soglia di overlap. Allo stesso modo, due toponimi diversi che casualmente cadono vicini (es. "Monte Alto" e "Monte Basso" a pochi metri) non vengono mai uniti, perché il testo è diverso.

**Opzione "Solo righe selezionate"**: se vuoi applicare la pulizia solo a un sottoinsieme della tabella (es. dopo aver rilanciato Claude su alcuni quadranti), seleziona le righe interessate e spunta questa casella prima di premere il bottone.

**Quando usarlo**: sempre, come ultimo passo prima di confermare la creazione dei punti — specialmente con overlap alto (>150px) o su mappe dove molti toponimi cadono vicino ai bordi dei quadranti. Il bottone può essere premuto più volte senza rischio: se non trova duplicati, lo segnala semplicemente senza modificare nulla.
