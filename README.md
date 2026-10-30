# Toponimi Extractor — plugin QGIS

Estrazione automatica di toponimi ed etichette testuali da un layer WMS raster,
tramite selezione di un'area, scomposizione in quadranti alla risoluzione
ottimale del servizio, e riconoscimento del testo con una pipeline OCR a
cascata (Tesseract locale + fallback a modelli vision Claude/GPT). L'output è
un layer puntuale GeoPackage, con un punto per ogni singolo toponimo
riconosciuto (centrato sul testo stesso, non sul quadrante che lo contiene).

## Funzionalità

- **Seleziona area**: strumento interattivo per disegnare un rettangolo (AOI)
  sulla mappa, sopra un layer WMS raster attivo.
- **Estrai toponimi**: scompone l'area in quadranti dimensionati in base ai
  limiti dichiarati dal servizio WMS (`GetCapabilities`), esegue OCR su ogni
  quadrante in un task in background (non blocca la UI di QGIS) e scrive i
  risultati in un layer puntuale GeoPackage aggiunto automaticamente al
  progetto.
- **Impostazioni**: scelta del motore OCR primario (Tesseract locale oppure
  solo cloud), lingua Tesseract, soglia di confidenza sotto la quale si
  attiva il fallback cloud, provider cloud (Claude, GPT, o entrambi),
  override opzionali per dimensione tile e risoluzione.

## Pipeline OCR a cascata

1. **Tesseract locale** (se abilitato): riconoscimento parola per parola con
   bounding box, così da georeferenziare ogni toponimo separatamente anche
   quando un quadrante ne contiene molti.
2. **Fallback cloud** (Claude e/o GPT vision): attivato automaticamente se
   Tesseract non trova testo, o se la confidenza media delle parole trovate è
   sotto la soglia configurata. Le API key vengono lette dalle variabili
   d'ambiente `ANTHROPIC_API_KEY` / `OPENAI_API_KEY`, con possibilità di
   override nelle impostazioni del plugin.

Ogni risultato salvato nel GeoPackage include: testo, motore OCR che lo ha
prodotto, confidenza, riga/colonna del quadrante di provenienza, rotazione
stimata del testo.

## Installazione in QGIS

1. Apri QGIS → **Estensioni → Gestisci e installa estensioni**.
2. Scheda **Installa da ZIP**, seleziona `toponimi_extractor.zip`.
3. Clicca **Installa Estensione**.
4. Compariranno due icone in toolbar ("Seleziona area", "Estrai toponimi") e
   una voce di menu "Toponimi Extractor" con le impostazioni.

## Dipendenze Python

Il Python "embedded" di QGIS su Windows è separato da un eventuale Python di
sistema. Installa le dipendenze in quell'ambiente, tipicamente tramite la
OSGeo4W Shell:

```
py3_env
python -m pip install -r requirements.txt
```

oppure singolarmente:

```
python -m pip install pytesseract Pillow anthropic openai
```

- **Tesseract OCR** (motore locale): va installato separatamente come
  binario di sistema, poi indicato nelle impostazioni del plugin se non è nel
  PATH di sistema. Vedi le [istruzioni di installazione](https://github.com/tesseract-ocr/tesseract#installing-tesseract)
  e gli [installer già pronti](https://tesseract-ocr.github.io/tessdoc/Installation.html). Per l'italiano
  assicurati che il pacchetto lingua `ita` di Tesseract sia installato.
- **ANTHROPIC_API_KEY** / **OPENAI_API_KEY**: variabili d'ambiente per i
  motori cloud di fallback. Puoi anche inserirle come override diretto nelle
  impostazioni del plugin (salvate localmente tramite `QSettings`).

## Utilizzo

1. Carica un layer WMS raster nel progetto e selezionalo nel pannello Layer.
2. Clicca **Seleziona area** e disegna un rettangolo sulla mappa.
3. Clicca **Estrai toponimi**, scegli il percorso del GeoPackage di output,
   avvia l'estrazione.
4. A completamento, il layer puntuale con i toponimi trovati viene aggiunto
   al progetto.

## Sviluppo

Per lo sviluppo iterativo si consiglia il plugin **Plugin Reloader** (ricarica
il codice senza riavviare QGIS ad ogni modifica). I test della logica pura
(griglia, georeferenziazione) non richiedono QGIS e si eseguono con:

```
python test/test_core.py -v
```
