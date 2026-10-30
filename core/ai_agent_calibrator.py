"""Calibrazione AI-driven: un agente Claude o GPT (chain-of-thought + tool use)
decide in autonomia risoluzione, dimensione tile, overlap e motore OCR da
usare per l'estrazione, senza che l'utente debba scegliere nulla a mano.

Differenza rispetto a `scale_calibrator.py`:
- `scale_calibrator` prova scale candidate fisse e giudica con la sola
  confidenza media di Tesseract (euristica, nessuna lettura visiva).
- Questo modulo manda il tile di prova COME IMMAGINE all'AI ad ogni
  iterazione: l'AI guarda lo screenshot, legge l'anteprima testuale del
  risultato OCR, e decide essa stessa i prossimi parametri da provare,
  fino a un massimo di `MAX_ITERATIONS` tentativi (richiesta esplicita
  dell'utente: "massimo 5 prove").

Loop agentico (chain of thought esplicitato nel campo "reasoning" che il
modello deve restituire ad ogni turno):
  1. Punto di partenza: metadati WMS (GetCapabilities) + un primo rendering.
  2. Ad ogni iterazione, il modello vede:
     - l'immagine del tile di prova corrente
     - il testo riconosciuto da Tesseract su quel tile (se disponibile)
     - lo storico dei tentativi precedenti con il punteggio ottenuto
     e risponde con UNA chiamata a uno dei tool esposti:
       - `try_parameters`: chiede di renderizzare e testare una nuova
         combinazione di (risoluzione, tile_size_px, overlap_px, motore OCR)
       - `accept_configuration`: dichiara la configurazione corrente buona e
         ferma il ciclo
  3. Il ciclo si ferma quando il modello chiama `accept_configuration`,
     oppure dopo MAX_ITERATIONS tentativi (in quel caso si usa il tentativo
     con punteggio più alto tra quelli provati, e l'utente viene avvisato).

Il "punteggio" di ciascun tentativo è calcolato automaticamente (numero di
parole trovate, confidenza media, frazione di testo con bbox plausibile) ma
la DECISIONE su cosa provare dopo è sempre dell'AI, non di una soglia fissa:
è lei a leggere l'immagine e capire se, ad esempio, il testo è troppo piccolo,
se la mappa è vuota in quel punto, o se conviene semplicemente cambiare
motore invece che la risoluzione.
"""

import json
import tempfile

from qgis.core import QgsRectangle, QgsMessageLog, Qgis, QgsUnitTypes

from ..qgis_integration import tile_renderer
from ..ocr import tesseract_engine
from .grid import Tile, clamp_overlap, coarsest_useful_resolution, estimate_tile_count as _estimate_tile_count
from .usage_tracker import default_model
from . import wms_capabilities
from .wms_capabilities import scale_to_meters_per_pixel

LOG_TAG = "Toponimi Extractor"

MAX_ITERATIONS = 5

# Lato del tile di prova quadrato (px): lo stesso lato massimo dei tile reali
# (vedi OCR_FRIENDLY_TILE_PX in tasks.extraction_task), così il primo tentativo
# corrisponde a un tile vero dell'estrazione e non a un ritaglio più piccolo.
TEST_TILE_SIZE_PX = 1400

# Denominatori di scala cartografica di riferimento, dal più largo (economico)
# al più dettagliato. Il primo è il limite oltre il quale il tentativo iniziale
# non si allarga (vedi _starting_resolution).
STARTER_SCALE_DENOMINATORS = [10000, 5000, 2000, 1000, 500]

# Fallback quando il CRS del layer NON è in metri (es. gradi, piedi): non ha
# senso applicare la formula OGC scala→m/px, quindi si usa una frazione fissa
# dell'estensione dell'AOI come primo tentativo (es. 1/600 del lato più corto
# dell'AOI come dimensione di un pixel), che l'AI potrà comunque correggere
# nei turni successivi guardando l'immagine risultante.
FALLBACK_AOI_FRACTION_PER_PIXEL = 1.0 / 600.0

# Limite di sicurezza sul numero di tile che l'estrazione REALE produrrebbe
# con la configurazione scelta dall'agente, applicato sull'intera AOI (non sul
# singolo tile di prova 768px usato in calibrazione). L'agente valuta solo la
# leggibilità del tile di prova: non ha mai visione diretta di quanti tile
# risulterebbero sull'area intera, e nulla nello schema del tool gli impedisce
# di proporre una risoluzione molto fine (es. 0.05 m/px) che sul tile di test
# (piccolo) sembra ragionevole ma sull'AOI reale (molto più grande) produce
# decine di migliaia di tile — bug osservato: 18000 tile lanciati, ciascuno
# con una chiamata OCR, su un'unica estrazione. Qualunque proposta che
# supererebbe questo limite viene scartata e l'agente viene informato del
# perché, invece di essere applicata ciecamente.
MAX_ESTIMATED_TILES_FOR_EXTRACTION = 500

# Lato massimo (px) dell'immagine di OVERVIEW dell'intera AOI, mostrata
# all'agente PRIMA di qualunque tentativo di zoom di dettaglio — un passo che
# prima mancava del tutto: l'agente vedeva solo un crop piccolo (768px) al
# centro dell'area, senza mai sapere quanto fosse grande l'AOI nel suo
# insieme, né quanti tile ne sarebbero risultati a una data risoluzione.
# Risultato osservato: proposte di risoluzione plausibili SUL CROP piccolo ma
# che applicate all'AOI reale (molto più estesa) producevano 18000+ tile.
OVERVIEW_MAX_SIDE_PX = 1200


# Il thinking dei modelli che lo hanno sempre attivo conta dentro max_tokens:
# con un tetto troppo basso la risposta si tronca prima della chiamata al tool.
AGENT_MAX_TOKENS = 8000


class AiCalibrationError(Exception):
    pass


class AiCalibrationCanceled(Exception):
    """L'utente ha annullato la ricerca."""


class AttemptResult:
    """Un singolo tentativo (render + OCR + punteggio) nel ciclo agentico."""

    def __init__(self, iteration, meters_per_pixel, tile_size_px, overlap_px, engine, words, image_path):
        self.iteration = iteration
        self.meters_per_pixel = meters_per_pixel
        self.tile_size_px = tile_size_px
        self.overlap_px = overlap_px
        self.engine = engine
        self.words = words
        self.image_path = image_path
        self.avg_confidence = (
            sum(w["confidence"] for w in words) / len(words) if words else 0.0
        )
        self.words_found = len(words)
        self.score = self._compute_score()

    def _compute_score(self):
        """Punteggio euristico usato solo come riferimento mostrato all'AI e
        come criterio di scelta del "migliore tra i 5" se nessuna proposta
        viene mai accettata esplicitamente. La decisione di continuare o
        fermarsi resta comunque dell'AI turno per turno."""
        if self.words_found == 0:
            return 0.0
        # Premia sia la confidenza media sia il numero di parole trovate
        # (un solo toponimo letto benissimo non basta su un tile denso),
        # con un tetto per non premiare all'infinito mappe molto dense.
        density_bonus = min(self.words_found, 20) / 20.0 * 30.0
        return self.avg_confidence * 0.7 + density_bonus

    def to_prompt_summary(self):
        sample_words = ", ".join(w["text"] for w in self.words[:15])
        return (
            f"Tentativo #{self.iteration}: risoluzione={self.meters_per_pixel:.4f} unità CRS/px, "
            f"tile={self.tile_size_px}px, overlap={self.overlap_px}px, motore={self.engine} → "
            f"{self.words_found} parole trovate, confidenza media {self.avg_confidence:.0f}%, "
            f"punteggio={self.score:.0f}/100.\n"
            f"Testo letto (max 15 parole): {sample_words or '(nessuno)'}"
        )


class AiCalibrationResult:
    def __init__(self, accepted, attempts, reasoning_log):
        self.accepted = accepted  # AttemptResult scelto come configurazione finale
        self.attempts = attempts  # lista di tutti gli AttemptResult provati
        self.reasoning_log = reasoning_log  # lista di stringhe (chain of thought leggibile)

    @property
    def meters_per_pixel(self):
        return self.accepted.meters_per_pixel

    @property
    def tile_size_px(self):
        return self.accepted.tile_size_px

    @property
    def overlap_px(self):
        return self.accepted.overlap_px

    @property
    def engine(self):
        return self.accepted.engine

    def report_text(self):
        lines = [a.to_prompt_summary() for a in self.attempts]
        return "\n\n".join(lines)


# ---------------------------------------------------------------------------
# Tool esposti all'AI (JSON schema, formato Anthropic / compatibile OpenAI)
# ---------------------------------------------------------------------------

_TOOLS_ANTHROPIC = [
    {
        "name": "try_parameters",
        "description": (
            "Chiede di renderizzare un nuovo tile di prova al centro dell'area con i "
            "parametri indicati ed eseguire l'OCR su di esso, per valutarne il risultato. "
            "Usalo quando il tentativo corrente non è ancora abbastanza buono e vuoi "
            "provare un'altra combinazione di risoluzione/tile/motore."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "reasoning": {
                    "type": "string",
                    "description": "Ragionamento: perché provare questi parametri dato quanto osservato finora.",
                },
                "meters_per_pixel": {
                    "type": "number",
                    "description": "Risoluzione a terra desiderata, in unità del CRS per pixel. Più basso = più dettaglio, più tile, più costo.",
                },
                "tile_size_px": {
                    "type": "integer",
                    "minimum": 256,
                    "maximum": 1400,
                    "description": "Lato del tile di prova in pixel (tra 256 e 1400).",
                },
                "overlap_px": {
                    "type": "integer",
                    "minimum": 50,
                    "maximum": 300,
                    "description": "Sovrapposizione tra tile adiacenti in pixel, per non tagliare testo sul bordo (tra 50 e 300).",
                },
                "engine": {
                    "type": "string",
                    "enum": ["tesseract", "claude", "gpt"],
                    "description": "Motore OCR da usare per QUESTO test.",
                },
            },
            "required": ["reasoning", "meters_per_pixel", "tile_size_px", "overlap_px", "engine"],
        },
    },
    {
        "name": "accept_configuration",
        "description": (
            "Dichiara che l'ultimo tentativo testato (quello appena valutato) ha parametri "
            "sufficientemente buoni per essere usati sull'intera area, e ferma il ciclo di calibrazione."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "reasoning": {
                    "type": "string",
                    "description": "Perché questa configurazione è adeguata per l'intera estrazione.",
                },
            },
            "required": ["reasoning"],
        },
    },
]

# Stesso contratto, formato "function" per Chat Completions (OpenAI).
_TOOLS_OPENAI = [
    {"type": "function", "function": {"name": t["name"], "description": t["description"], "parameters": t["input_schema"]}}
    for t in _TOOLS_ANTHROPIC
]


_SYSTEM_PROMPT = """Sei un agente esperto di cartografia storica e OCR, parte di un plugin QGIS. \
Il tuo compito è trovare, con il minor numero di tentativi possibile (massimo {max_iterations}), \
la combinazione di risoluzione di rendering, dimensione tile, overlap e motore OCR che permette \
di leggere al meglio i toponimi (nomi di luoghi, fiumi, monti, quote) su una mappa raster WMS, \
per poi applicarla all'estrazione sull'intera area selezionata dall'utente.

PRIMA di qualunque tentativo, ti viene mostrata una immagine di OVERVIEW: un rendering dell'INTERA \
area selezionata dall'utente (non un crop), a bassa risoluzione solo per darti un colpo d'occhio \
d'insieme — dimensioni reali dell'area, densità approssimativa di toponimi, dove si concentra il \
testo. Usa questa immagine SOLO per farti un'idea generale, mai per leggere OCR (è troppo piccola e \
sgranata per quello): servirà a te per scegliere con cognizione di causa il primo livello di zoom di \
dettaglio da testare, sapendo già che scala ha senso provare.

Dopo l'overview, ad ogni turno ti viene mostrata l'immagine del tile di prova DI DETTAGLIO più \
recente (un crop centrato nell'area, non l'intera area) insieme al risultato OCR ottenuto su di \
esso, e insieme a QUANTI TILE RISULTEREBBERO SULL'INTERA AREA se applicassi quella stessa \
risoluzione/dimensione a tutta l'estrazione (non solo al crop di prova). Devi decidere se:
- il risultato è già abbastanza buono per procedere su tutta l'area → chiama `accept_configuration`;
- conviene provare un'altra combinazione di parametri → chiama `try_parameters` con i nuovi valori.

VINCOLO ASSOLUTO sul numero di tile: l'area selezionata dall'utente può essere molto più grande del \
singolo crop di prova che vedi. Una risoluzione che sul crop piccolo sembra dare un buon dettaglio, \
applicata all'intera area può produrre MIGLIAIA di tile reali (una chiamata OCR ciascuno): non è mai \
accettabile proporre una configurazione che supererebbe {max_tiles} tile sull'area intera — il numero \
stimato ti viene sempre mostrato esplicitamente, usalo per scegliere una risoluzione più larga (un \
meters_per_pixel più alto, cioè meno dettaglio) PRIMA di cercare ancora più dettaglio: se un tile è \
illeggibile, prova prima ad alzare tile_size_px o cambiare motore, non necessariamente ad abbassare \
ulteriormente la risoluzione se questo farebbe esplodere il conteggio tile.

Linee guida di dominio (cartografia/OCR) da applicare nel tuo ragionamento:
- Se l'immagine è vuota, sfocata o troppo piccola da leggere anche a occhio: la risoluzione è \
troppo bassa (meters_per_pixel troppo alto) → riducilo, MA controlla sempre il conteggio tile stimato \
prima di farlo: se è già vicino al limite, preferisci un'altra strada (motore diverso, tile_size più \
grande) piuttosto che spingere la risoluzione oltre il vincolo.
- Se il testo si legge bene a occhio ma l'OCR non trova nulla o trova poco: il problema è il \
motore, non la risoluzione → prova un motore cloud (claude o gpt) invece di aumentare ulteriormente \
la risoluzione, che aumenterebbe solo i costi senza bisogno.
- Se il testo è curvo, ruotato o corsivo (comune lungo fiumi, confini, toponimi storici): Tesseract \
tende a fallire anche con buona risoluzione → preferisci un motore cloud (claude o gpt), che legge \
testo ruotato meglio di Tesseract.
- Se trovi pochissime parole ma quelle trovate hanno confidenza alta: potrebbe semplicemente essere \
un'area povera di toponimi (es. zona rurale/mare), non un problema di parametri → valuta se accettare.
- Se il tile_size scelto è molto grande, la stessa risoluzione produce immagini pesanti e lente da \
renderizzare e può far perdere dettaglio ai motori cloud (che ridimensionano immagini molto grandi): \
preferisci alzare la risoluzione (meters_per_pixel più basso) piuttosto che tile enormi — MA SOLO SE \
questo non supera il vincolo sul numero di tile sopra.
- Aumentare la risoluzione (meters_per_pixel più basso) e usare motori cloud hanno entrambi un costo \
(più tile da renderizzare/più chiamate API): non cercare la perfezione assoluta, cerca la combinazione \
PIÙ ECONOMICA che legge comunque bene i toponimi visibili nel tile, rispettando sempre il vincolo sul \
numero massimo di tile.
- Quando il conteggio stimato è già 1 tile, hai raggiunto il costo minimo: una risoluzione ancora \
più grossolana (meters_per_pixel più alto) NON fa risparmiare nulla e toglie solo pixel al testo, \
quindi non proporla mai (verrebbe comunque riportata al valore che riempie un tile). In quel caso \
l'immagine che vedi è esattamente quella che verrà letta nell'estrazione: se i toponimi si leggono, \
accetta; se non si leggono, chiedi PIÙ dettaglio (meters_per_pixel più basso).
- Non inventare mai toponimi: se un'etichetta non è leggibile, non va considerata letta.
- Hai al massimo {max_iterations} tentativi di dettaglio in totale (incluso il primo, già fatto; \
l'overview iniziale non conta come tentativo). Se stai per finire i tentativi disponibili e nessuna \
configurazione è ancora ottima, accetta la migliore vista finora piuttosto che sprecare l'ultimo \
tentativo su un cambiamento marginale.

Rispondi SEMPRE chiamando uno dei due tool: non rispondere con testo libero."""


def _starting_resolution(layer, aoi_extent, tile_size_px, scale_range=None):
    """Calcola il valore di meters_per_pixel (o equivalente nelle unità del
    CRS) da usare come PRIMO tentativo, prima che l'AI abbia visto qualunque
    immagine.

    Si parte dalla configurazione PIÙ ECONOMICA, non dalla più dettagliata:
    l'intera area in un solo tile. Un'area di pochi km su una carta 1:25000
    si legge già così (è quello che si ottiene incollando uno screenshot
    della mappa in un modello vision); partire invece da una scala molto
    fine mostrava all'agente un ritaglio di poche decine di metri e
    produceva stime di ~1000 tile fin dal primo tentativo. Se il testo non
    si legge sarà l'agente a chiedere più dettaglio.

    `scale_range`: tupla opzionale (min_scale_denominator, max_scale_denominator)
    dichiarata dal servizio WMS (vedi wms_capabilities.fetch_layer_scale_range).
    Fuori da quel range il server restituisce un'immagine bianca per policy,
    qualunque sia il dettaglio richiesto, quindi la partenza viene riportata
    al suo interno.

    - CRS in METRI: area intera in un tile, ma mai più grossolana della scala
      più larga di STARTER_SCALE_DENOMINATORS (su aree molto estese un solo
      tile sarebbe illeggibile in partenza).
    - CRS in GRADI o altre unità: la formula scala→m/px non è applicabile;
      si usa una frazione fissa del lato più corto dell'AOI come dimensione
      di un pixel.
    """
    min_scale, max_scale = scale_range if scale_range else (None, None)

    try:
        units = layer.crs().mapUnits()
    except Exception:
        units = None

    if units == QgsUnitTypes.DistanceMeters:
        coarsest = scale_to_meters_per_pixel(STARTER_SCALE_DENOMINATORS[0])
        longest_side = max(aoi_extent.width(), aoi_extent.height())
        mpp = min(longest_side / tile_size_px, coarsest) if longest_side > 0 else coarsest
        if max_scale is not None:
            mpp = min(mpp, scale_to_meters_per_pixel(max_scale))
        if min_scale is not None:
            mpp = max(mpp, scale_to_meters_per_pixel(min_scale))
        return mpp

    # CRS non in metri (gradi, piedi, ecc.): fallback basato sull'estensione
    # reale dell'AOI, indipendente dall'unità (scale_range è espresso in
    # denominatori di scala cartografica e non è applicabile qui senza una
    # conversione affidabile unità-CRS→metri, che per gradi dipende dalla
    # latitudine: si lascia che l'AI corregga nei turni successivi).
    aoi_span = min(aoi_extent.width(), aoi_extent.height())
    if aoi_span <= 0:
        return FALLBACK_AOI_FRACTION_PER_PIXEL  # AOI degenere: valore qualsiasi, verrà corretto dall'AI
    return aoi_span * FALLBACK_AOI_FRACTION_PER_PIXEL


def _render_and_ocr(layer, aoi_extent, meters_per_pixel, tile_size_px, overlap_px, engine, ocr_settings, temp_dir, iteration):
    """Renderizza il tile di prova al centro dell'AOI e lo passa al motore
    OCR richiesto. Ritorna un AttemptResult.

    Il tile di prova non esce mai dall'AOI: se l'area a questa risoluzione è
    più piccola di un tile, l'immagine è l'area stessa (cioè esattamente il
    tile che l'estrazione produrrebbe). Un ritaglio a dimensione fissa
    mostrerebbe invece territorio esterno all'area, tanto più quanto più la
    risoluzione è grossolana: più toponimi "trovati", punteggio più alto, e
    l'agente premiato per aver scelto una risoluzione illeggibile.
    """
    width_px = max(1, min(tile_size_px, round(aoi_extent.width() / meters_per_pixel)))
    height_px = max(1, min(tile_size_px, round(aoi_extent.height() / meters_per_pixel)))
    half_width_map = (width_px * meters_per_pixel) / 2.0
    half_height_map = (height_px * meters_per_pixel) / 2.0
    center_x = aoi_extent.center().x()
    center_y = aoi_extent.center().y()
    test_tile = Tile(
        row=0, col=0,
        xmin=center_x - half_width_map, ymin=center_y - half_height_map,
        xmax=center_x + half_width_map, ymax=center_y + half_height_map,
        width_px=width_px, height_px=height_px,
    )
    image_path = tile_renderer.render_tile_to_file(
        layer, test_tile, temp_dir, filename=f"ai_calib_{iteration}.png"
    )

    words = _run_single_engine_ocr(image_path, width_px, height_px, engine, ocr_settings)

    return AttemptResult(iteration, meters_per_pixel, tile_size_px, overlap_px, engine, words, image_path)


def _render_aoi_overview(layer, aoi_extent, temp_dir):
    """Renderizza l'INTERA AOI (non un crop centrale) a una risoluzione che la
    fa stare in al più OVERVIEW_MAX_SIDE_PX di lato, per dare all'agente una
    visione d'insieme PRIMA che proponga risoluzioni di dettaglio. Ritorna
    (image_path, overview_meters_per_pixel, aoi_width_m, aoi_height_m).
    """
    aoi_width = aoi_extent.width()
    aoi_height = aoi_extent.height()
    longest_side = max(aoi_width, aoi_height)
    # meters_per_pixel (o unità CRS/px) tale per cui il lato più lungo
    # dell'AOI occupi esattamente OVERVIEW_MAX_SIDE_PX pixel nell'overview.
    overview_mpp = longest_side / OVERVIEW_MAX_SIDE_PX if longest_side > 0 else 1.0

    overview_width_px = max(1, round(aoi_width / overview_mpp))
    overview_height_px = max(1, round(aoi_height / overview_mpp))

    overview_tile = Tile(
        row=0, col=0,
        xmin=aoi_extent.xMinimum(), ymin=aoi_extent.yMinimum(),
        xmax=aoi_extent.xMaximum(), ymax=aoi_extent.yMaximum(),
        width_px=overview_width_px, height_px=overview_height_px,
    )
    image_path = tile_renderer.render_tile_to_file(layer, overview_tile, temp_dir, filename="ai_calib_overview.png")

    return image_path, overview_mpp, aoi_width, aoi_height


def _run_single_engine_ocr(image_path, width, height, engine, ocr_settings):
    """OCR con UN SOLO motore specifico (non la cascata), perché durante la
    calibrazione è l'AI stessa a scegliere quale motore provare ad ogni turno."""
    if engine == "tesseract":
        if ocr_settings.get("tesseract_cmd"):
            tesseract_engine.configure_tesseract_cmd(ocr_settings["tesseract_cmd"])
        try:
            return tesseract_engine.recognize_words(
                image_path, lang=ocr_settings.get("tesseract_lang", "ita"), min_confidence=0
            )
        except tesseract_engine.TesseractNotAvailable as exc:
            QgsMessageLog.logMessage(f"Tesseract non disponibile durante calibrazione AI: {exc}", LOG_TAG, Qgis.Warning)
            return []

    if engine == "claude":
        from ..ocr import claude_engine
        env_var = ocr_settings.get("anthropic_api_key_env_var")
        key = ocr_settings.get("anthropic_api_key")
        if not claude_engine.is_available(key, env_var):
            return []
        model = ocr_settings.get("claude_model") or default_model("claude")
        words, _usage = claude_engine.recognize_words(image_path, width, height, api_key=key, env_var=env_var, model=model)
        return words

    if engine == "gpt":
        from ..ocr import gpt_engine
        key = ocr_settings.get("openai_api_key")
        if not gpt_engine.is_available(key):
            return []
        model = ocr_settings.get("gpt_model") or default_model("gpt")
        words, _usage = gpt_engine.recognize_words(image_path, width, height, api_key=key, model=model)
        return words

    return []


def _vision_capable_agent(provider):
    return provider in ("claude", "gpt")


def _call_agent_anthropic(client, model, messages, system_prompt):
    response = client.messages.create(
        model=model,
        max_tokens=AGENT_MAX_TOKENS,
        system=system_prompt,
        tools=_TOOLS_ANTHROPIC,
        messages=messages,
    )
    tool_use_blocks = [b for b in response.content if getattr(b, "type", None) == "tool_use"]
    if not tool_use_blocks:
        # Il modello non ha chiamato un tool: forziamo un fallback testuale,
        # trattandolo come "nessuna decisione" (il chiamante gestisce l'uscita).
        return None, response
    block = tool_use_blocks[0]
    # Tutti gli id tool_use di QUESTA risposta vanno chiusi da un tool_result
    # nel turno successivo (non solo quello che useremo come decisione): se il
    # modello ne emette più di uno nello stesso turno, ignorarne anche solo
    # uno fa rifiutare la richiesta successiva con lo stesso errore 400
    # "tool_use ids were found without tool_result blocks".
    all_ids = [b.id for b in tool_use_blocks]
    return {"name": block.name, "input": block.input, "id": block.id, "all_ids": all_ids}, response


def _call_agent_openai(client, model, messages):
    response = client.chat.completions.create(
        model=model,
        max_tokens=AGENT_MAX_TOKENS,
        tools=_TOOLS_OPENAI,
        tool_choice="required",
        messages=messages,
    )
    choice = response.choices[0]
    tool_calls = choice.message.tool_calls or []
    if not tool_calls:
        return None, response
    call = tool_calls[0]
    return {
        "name": call.function.name,
        "input": json.loads(call.function.arguments),
        "id": call.id,
        "all_ids": [c.id for c in tool_calls],
    }, response


def _image_to_base64(image_path):
    import base64
    with open(image_path, "rb") as f:
        return base64.standard_b64encode(f.read()).decode("ascii")


def run_ai_driven_calibration(
    layer,
    aoi_extent,
    ocr_settings,
    agent_provider="claude",
    progress_callback=None,
    wms_params=None,
    overlap_px=200,
    is_canceled=None,
):
    """Esegue il ciclo agentico di calibrazione (max MAX_ITERATIONS tentativi)
    e ritorna un AiCalibrationResult pronto per essere applicato
    all'estrazione completa.

    `agent_provider`: "claude" o "gpt" — quale API usare come "cervello"
    dell'agente (deve avere una API key valida nelle ocr_settings).
    `wms_params`: dict con "base_url"/"layer_name" del servizio WMS attivo
    (vedi qgis_integration.wms_layer_utils.get_wms_params), usato per
    interrogare MinScaleDenominator/MaxScaleDenominator dichiarati dal
    servizio — fuori da quel range il server risponde con un'immagine bianca
    a PRESCINDERE dalla risoluzione richiesta, quindi va sia evitato nel primo
    tentativo sia comunicato esplicitamente all'agente.
    """
    if not _vision_capable_agent(agent_provider):
        raise AiCalibrationError(f"Provider agente non supportato: {agent_provider}")

    def _stop_if_canceled():
        # Una chiamata al modello già partita non si può interrompere: si
        # controlla tra un passo e l'altro.
        if is_canceled is not None and is_canceled():
            raise AiCalibrationCanceled()

    if progress_callback:
        progress_callback("Lettura dei limiti di scala dichiarati dal servizio WMS...")

    client, model = _build_agent_client(agent_provider, ocr_settings)

    scale_range = (None, None)
    if wms_params:
        try:
            scale_range = wms_capabilities.fetch_layer_scale_range(
                wms_params.get("base_url"), wms_params.get("layer_name")
            )
        except Exception as exc:
            QgsMessageLog.logMessage(
                f"[AI calibration] Lettura range di scala WMS fallita (si procede senza vincolo noto): {exc}",
                LOG_TAG, Qgis.Warning,
            )
            scale_range = (None, None)

    min_scale, max_scale = scale_range
    if min_scale is not None or max_scale is not None:
        QgsMessageLog.logMessage(
            f"[AI calibration] Servizio WMS dichiara range di scala valido: "
            f"1:{min_scale or '?'} — 1:{max_scale or '?'}", LOG_TAG, Qgis.Info,
        )

    temp_dir = tempfile.mkdtemp(prefix="toponimi_extractor_ai_calib_")
    attempts = []
    reasoning_log = []

    try:
        # Primo tentativo: valore di partenza euristico (non deciso dall'AI,
        # serve solo a darle un punto di partenza concreto da valutare),
        # calcolato sulla scala cartografica reale, sulle unità del CRS del
        # layer e sul range di scala dichiarato dal servizio — mai un numero
        # arbitrario di "unità CRS/px" (vedi _starting_resolution per il
        # perché: un valore fuori range produce un tile bianco qualunque sia
        # il dettaglio richiesto).
        tile_size_px = TEST_TILE_SIZE_PX
        meters_per_pixel = _starting_resolution(layer, aoi_extent, tile_size_px, scale_range=scale_range)
        overlap_px = clamp_overlap(overlap_px, tile_size_px)
        engine = "tesseract" if ocr_settings.get("use_tesseract", True) else ocr_settings.get("cloud_provider", "claude")
        if engine == "both":
            engine = "claude"

        if min_scale is not None or max_scale is not None:
            scale_range_note = (
                f"\n\nIMPORTANTE — vincolo dichiarato dal servizio WMS: questo layer è visibile SOLO "
                f"per scale comprese tra 1:{min_scale or 'nessun limite'} e 1:{max_scale or 'nessun limite'}. "
                f"Se un tile risulta completamente bianco/vuoto A QUALSIASI risoluzione tu provi, il problema "
                f"NON è il livello di dettaglio: è che la scala richiesta sta uscendo da questo range (ricorda "
                f"che una risoluzione più bassa in meters_per_pixel corrisponde a una scala PIÙ dettagliata, cioè "
                f"un denominatore di scala PIÙ PICCOLO — e viceversa). In quel caso prova una risoluzione che "
                f"corrisponda a una scala chiaramente dentro questo range, invece di continuare a variare "
                f"meters_per_pixel in una direzione che resta comunque fuori range."
            )
        else:
            scale_range_note = (
                "\n\nIl servizio WMS non dichiara un range di scala esplicito: se un tile risulta "
                "completamente bianco/vuoto a più risoluzioni diverse, considera anche l'ipotesi che il punto "
                "centrale dell'area scelta ricada semplicemente in una zona priva di contenuto cartografico "
                "per questo layer (es. area non coperta), non necessariamente un problema di risoluzione."
            )

        system_prompt_text = _SYSTEM_PROMPT.format(
            max_iterations=MAX_ITERATIONS, max_tiles=MAX_ESTIMATED_TILES_FOR_EXTRACTION
        ) + scale_range_note

        messages_anthropic = []
        messages_openai = [{"role": "system", "content": system_prompt_text}]
        pending_tool_use_ids = []  # id dei tool_use del turno precedente, da "chiudere" in questo turno
        rejection_note = None  # motivo del rifiuto dell'ultima proposta, da riferire all'agente
        attempt = None

        # --- Passo 0 (overview): mostra all'agente l'INTERA AOI prima di
        # qualunque tentativo di zoom di dettaglio, così sceglie il primo
        # livello di dettaglio sapendo già quanto è estesa l'area reale —
        # non consuma uno dei MAX_ITERATIONS tentativi: è un messaggio
        # puramente informativo in più nella cronologia, il primo
        # try_parameters vero arriva comunque al turno 1 del loop sotto
        # (che include già il conteggio tile nel suo user_text).
        if progress_callback:
            progress_callback("Rendering overview dell'intera area selezionata...")
        overview_path, overview_mpp, aoi_width_m, aoi_height_m = _render_aoi_overview(layer, aoi_extent, temp_dir)
        overview_text = (
            f"OVERVIEW dell'intera area selezionata: {aoi_width_m:.0f} x {aoi_height_m:.0f} unità CRS "
            f"(renderizzata a {overview_mpp:.4f} unità CRS/px solo per colpo d'occhio, NON per OCR)."
        )
        reasoning_log.append(f"Overview: area di {aoi_width_m:.0f} x {aoi_height_m:.0f} unità CRS mostrata all'agente.")

        for iteration in range(1, MAX_ITERATIONS + 1):
            _stop_if_canceled()
            estimated_tiles = _estimate_tile_count(aoi_extent, meters_per_pixel, tile_size_px, overlap_px)

            if progress_callback:
                progress_callback(
                    f"Tentativo AI {iteration}/{MAX_ITERATIONS}: rendering dell'immagine e lettura con il "
                    f"motore '{engine}' a {meters_per_pixel:.3g} unità CRS/px "
                    f"(~{estimated_tiles} quadranti sull'intera area)..."
                )

            # Dopo una proposta rifiutata i parametri sono quelli del tentativo
            # precedente: rifare rendering e OCR darebbe lo stesso risultato.
            if rejection_note is None:
                attempt = _render_and_ocr(
                    layer, aoi_extent, meters_per_pixel, tile_size_px, overlap_px, engine,
                    ocr_settings, temp_dir, iteration,
                )
                attempts.append(attempt)

                QgsMessageLog.logMessage(f"[AI calibration] {attempt.to_prompt_summary()}", LOG_TAG, Qgis.Info)
                if progress_callback:
                    progress_callback(attempt.to_prompt_summary())

            is_last_iteration = iteration == MAX_ITERATIONS
            user_text = (
                f"{attempt.to_prompt_summary()}\n\n"
                f"Conteggio tile stimato sull'INTERA area con questi stessi parametri: "
                f"~{estimated_tiles} tile (limite massimo consentito: {MAX_ESTIMATED_TILES_FOR_EXTRACTION})."
            )
            if rejection_note is not None:
                user_text = (
                    f"La tua ultima proposta è stata RIFIUTATA e non è stata testata: {rejection_note} "
                    f"Quello che segue è ancora il tentativo precedente.\n\n{user_text}"
                )
                rejection_note = None
            if is_last_iteration:
                user_text += (
                    "\n\nQuesto è l'ULTIMO tentativo disponibile: devi chiamare "
                    "accept_configuration a prescindere dal risultato."
                )

            _stop_if_canceled()
            if progress_callback:
                progress_callback(
                    f"Tentativo AI {iteration}/{MAX_ITERATIONS}: l'agente valuta il risultato e decide "
                    f"il prossimo passo (può richiedere anche un paio di minuti)..."
                )

            decision, raw_response = _ask_agent_for_decision(
                agent_provider, client, model, messages_anthropic, messages_openai,
                user_text, attempt.image_path, is_last_iteration,
                pending_tool_use_ids=pending_tool_use_ids,
                system_prompt_text=system_prompt_text,
                extra_image_path=overview_path if iteration == 1 else None,
                extra_image_text=overview_text if iteration == 1 else None,
            )

            if decision is None:
                # Il modello non ha usato un tool: trattiamo come accettazione
                # implicita del tentativo corrente, per non bloccare il flusso.
                reasoning_log.append(
                    f"Turno {iteration}: risposta senza tool call, interpretata come accettazione."
                )
                break

            # Questo/i tool_use dovranno essere "chiusi" da un tool_result/tool
            # nel messaggio che mandiamo al turno successivo (se ce ne sarà uno).
            pending_tool_use_ids = decision.get("all_ids") or [decision["id"]]

            reasoning = decision["input"].get("reasoning", "")
            reasoning_log.append(f"Turno {iteration} ({decision['name']}): {reasoning}")

            if decision["name"] == "accept_configuration" or is_last_iteration:
                break

            # try_parameters: valida PRIMA di applicare. Una proposta che
            # supererebbe il limite di tile sull'area reale viene scartata:
            # si resta sui parametri precedenti (già renderizzati e quindi
            # già sicuri) e il prossimo turno informa l'agente del rifiuto,
            # invece di materializzare una griglia di migliaia di tile.
            next_params = decision["input"]
            try:
                proposed_mpp = float(next_params.get("meters_per_pixel", meters_per_pixel))
                proposed_tile_size = max(256, min(1400, int(next_params.get("tile_size_px", tile_size_px))))
                proposed_overlap = int(next_params.get("overlap_px", overlap_px))
            except (TypeError, ValueError):
                proposed_mpp, proposed_tile_size, proposed_overlap = 0.0, tile_size_px, overlap_px
            proposed_overlap = clamp_overlap(max(50, min(300, proposed_overlap)), proposed_tile_size)
            proposed_engine = next_params.get("engine", engine)
            if proposed_engine not in ("tesseract", "claude", "gpt"):
                proposed_engine = engine

            # Più grossolano di "tutta l'area in un tile" non fa risparmiare
            # nulla e rende il testo illeggibile: si riporta a quel valore.
            full_tile_mpp = coarsest_useful_resolution(aoi_extent, proposed_tile_size)
            if 0 < full_tile_mpp < proposed_mpp:
                reasoning_log.append(
                    f"Turno {iteration}: risoluzione proposta {proposed_mpp:.4f} più grossolana di un tile "
                    f"pieno, riportata a {full_tile_mpp:.4f} unità CRS/px."
                )
                proposed_mpp = full_tile_mpp

            proposed_tile_count = _estimate_tile_count(aoi_extent, proposed_mpp, proposed_tile_size, proposed_overlap)

            if proposed_tile_count is None:
                rejection_note = "i parametri non sono validi (meters_per_pixel deve essere un numero positivo)."
                reasoning_log.append(f"Turno {iteration}: proposta RIFIUTATA (parametri non validi).")
            elif proposed_tile_count > MAX_ESTIMATED_TILES_FOR_EXTRACTION:
                rejection_note = (
                    f"con meters_per_pixel={proposed_mpp:.4f} l'area intera produrrebbe ~{proposed_tile_count} "
                    f"tile, oltre il limite di {MAX_ESTIMATED_TILES_FOR_EXTRACTION}."
                )
                reasoning_log.append(
                    f"Turno {iteration}: proposta RIFIUTATA (meters_per_pixel={proposed_mpp:.4f} → "
                    f"~{proposed_tile_count} tile stimati, oltre il limite di {MAX_ESTIMATED_TILES_FOR_EXTRACTION}); "
                    f"mantenuti i parametri precedenti per il prossimo tentativo."
                )
                QgsMessageLog.logMessage(
                    f"[AI calibration] Proposta scartata: {proposed_tile_count} tile stimati "
                    f"(limite {MAX_ESTIMATED_TILES_FOR_EXTRACTION}), mpp={proposed_mpp}", LOG_TAG, Qgis.Warning,
                )
                # meters_per_pixel/tile_size_px/overlap_px/engine restano invariati
                # per il prossimo giro del loop: l'agente viene informato del
                # rifiuto tramite rejection_note.
            else:
                meters_per_pixel = proposed_mpp
                tile_size_px = proposed_tile_size
                overlap_px = proposed_overlap
                engine = proposed_engine

        best = max(attempts, key=lambda a: a.score)
        # L'ultimo tentativo valutato è, per costruzione del loop, quello che
        # l'AI ha accettato esplicitamente (o l'ultimo disponibile); se però
        # un tentativo precedente ha un punteggio nettamente migliore, lo
        # segnaliamo comunque nel report così l'utente lo vede.
        accepted = attempts[-1]
        if best.score > accepted.score + 15 and best is not accepted:
            reasoning_log.append(
                f"Nota: il tentativo #{best.iteration} ha punteggio più alto "
                f"({best.score:.0f} vs {accepted.score:.0f}) di quello accettato dall'AI; "
                f"mantenuto comunque quello scelto dall'agente salvo diversa indicazione."
            )

        # Rete di sicurezza finale: anche il tentativo ACCETTATO deve rispettare
        # il limite di tile sull'area reale (ogni attempt in questo loop è già
        # stato validato prima di diventare 'corrente', quindi in condizioni
        # normali questo non dovrebbe mai scattare — ma resta un ultimo
        # controllo esplicito prima di restituire la configurazione a
        # ExtractionTask, piuttosto che fidarsi ciecamente del loop sopra).
        accepted_tile_count = _estimate_tile_count(
            aoi_extent, accepted.meters_per_pixel, accepted.tile_size_px, accepted.overlap_px
        )
        if accepted_tile_count is not None and accepted_tile_count > MAX_ESTIMATED_TILES_FOR_EXTRACTION:
            reasoning_log.append(
                f"ATTENZIONE: la configurazione finale produrrebbe ~{accepted_tile_count} tile sull'area "
                f"reale, oltre il limite di sicurezza di {MAX_ESTIMATED_TILES_FOR_EXTRACTION}. Scelto invece "
                f"il tentativo con risoluzione più larga tra quelli provati per restare entro il limite."
            )
            safe_candidates = [
                a for a in attempts
                if (_estimate_tile_count(aoi_extent, a.meters_per_pixel, a.tile_size_px, a.overlap_px) or 0)
                <= MAX_ESTIMATED_TILES_FOR_EXTRACTION
            ]
            if safe_candidates:
                accepted = max(safe_candidates, key=lambda a: a.score)
            # Se NESSUN tentativo provato rispetta il limite (caso limite,
            # area enorme), si mantiene 'accepted' così com'è: ExtractionTask
            # applicherà comunque min(..., OCR_FRIENDLY_TILE_PX) a valle, e
            # l'utente vede comunque l'avviso esplicito sopra nel riepilogo.

        return AiCalibrationResult(accepted, attempts, reasoning_log)
    finally:
        import shutil
        shutil.rmtree(temp_dir, ignore_errors=True)


def _build_agent_client(provider, ocr_settings):
    if provider == "claude":
        from ..ocr import claude_engine
        anthropic = claude_engine._import_deps()
        key = claude_engine._get_api_key(
            ocr_settings.get("anthropic_api_key"), ocr_settings.get("anthropic_api_key_env_var")
        )
        if not key:
            raise AiCalibrationError("Nessuna API key Claude disponibile per l'agente AI-driven.")
        model = ocr_settings.get("claude_model") or default_model("claude")
        return claude_engine._get_client(anthropic, key), model

    if provider == "gpt":
        from ..ocr import gpt_engine
        openai = gpt_engine._import_deps()
        key = gpt_engine._get_api_key(ocr_settings.get("openai_api_key"))
        if not key:
            raise AiCalibrationError("Nessuna API key OpenAI disponibile per l'agente AI-driven.")
        model = ocr_settings.get("gpt_model") or default_model("gpt")
        return gpt_engine._get_client(openai, key), model

    raise AiCalibrationError(f"Provider agente non supportato: {provider}")


def _ask_agent_for_decision(
    provider, client, model, messages_anthropic, messages_openai, user_text, image_path, is_last_iteration,
    pending_tool_use_ids=None, system_prompt_text=None, extra_image_path=None, extra_image_text=None,
):
    """Manda il tentativo corrente (immagine + testo) al modello e ritorna la
    sua decisione (tool_use). `pending_tool_use_ids` sono gli id dei tool_use
    emessi nella risposta del turno precedente (lista vuota/None al
    primissimo turno, che non segue nessuna chiamata a tool): sia l'API
    Anthropic sia Chat Completions richiedono che OGNI blocco
    tool_use/tool_call nella risposta precedente sia seguito, nel messaggio
    immediatamente successivo, dal corrispondente tool_result/tool —
    altrimenti la richiesta viene rifiutata con "invalid_request_error"
    (errore osservato: "tool_use ids were found without tool_result blocks
    immediately after"). Analogamente, l'API Anthropic richiede stretta
    alternanza user/assistant: non si può mandare un messaggio "user" extra
    separato (es. l'overview) prima di questo senza un "assistant" in mezzo,
    quindi `extra_image_path`/`extra_image_text`, se forniti (solo al primo
    turno), vengono inclusi come blocchi AGGIUNTIVI nello stesso messaggio
    "user", invece che in un messaggio a parte.
    """
    img_b64 = _image_to_base64(image_path)
    media_type = "image/png"
    pending_tool_use_ids = pending_tool_use_ids or []

    extra_blocks_anthropic = []
    extra_blocks_openai = []
    if extra_image_path:
        extra_img_b64 = _image_to_base64(extra_image_path)
        extra_blocks_anthropic = [
            {"type": "text", "text": extra_image_text or ""},
            {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": extra_img_b64}},
        ]
        extra_blocks_openai = [
            {"type": "text", "text": extra_image_text or ""},
            {"type": "image_url", "image_url": {"url": f"data:{media_type};base64,{extra_img_b64}"}},
        ]

    if provider == "claude":
        image_and_text_content = extra_blocks_anthropic + [
            {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": img_b64}},
            {"type": "text", "text": user_text},
        ]
        if pending_tool_use_ids:
            # Il risultato del PRIMO tool_use pendente VEICOLA il nuovo
            # tentativo (immagine + resoconto OCR) come proprio content: è il
            # modo standard di restituire contenuto multimodale in risposta a
            # un tool_use. Eventuali altri tool_use pendenti nello stesso
            # turno (il nostro prompt ne chiede uno solo, ma non è garantito)
            # ricevono un tool_result testuale neutro: devono comunque
            # comparire, altrimenti l'intera richiesta viene rifiutata.
            tool_results = [
                {"type": "tool_result", "tool_use_id": pending_tool_use_ids[0], "content": image_and_text_content}
            ]
            for extra_id in pending_tool_use_ids[1:]:
                tool_results.append(
                    {"type": "tool_result", "tool_use_id": extra_id, "content": "Ignorato: un solo tool per turno."}
                )
            messages_anthropic.append({"role": "user", "content": tool_results})
        else:
            messages_anthropic.append({"role": "user", "content": image_and_text_content})

        decision, response = _call_agent_anthropic(
            client, model, messages_anthropic,
            system_prompt_text or _SYSTEM_PROMPT.format(
                max_iterations=MAX_ITERATIONS, max_tiles=MAX_ESTIMATED_TILES_FOR_EXTRACTION
            ),
        )
        # Rimettiamo in cronologia la risposta del modello (necessario perché
        # il prossimo turno possa referenziare correttamente il suo tool_use_id).
        messages_anthropic.append({"role": "assistant", "content": response.content})
        return decision, response

    if provider == "gpt":
        if pending_tool_use_ids:
            # Chat Completions vuole un messaggio "tool" dedicato (già
            # testuale) per OGNI tool_call pendente, prima di un nuovo
            # messaggio "user" con il contenuto del nuovo tentativo.
            messages_openai.append(
                {"role": "tool", "tool_call_id": pending_tool_use_ids[0], "content": user_text}
            )
            for extra_id in pending_tool_use_ids[1:]:
                messages_openai.append(
                    {"role": "tool", "tool_call_id": extra_id, "content": "Ignorato: un solo tool per turno."}
                )
            messages_openai.append(
                {
                    "role": "user",
                    "content": extra_blocks_openai + [
                        {"type": "image_url", "image_url": {"url": f"data:{media_type};base64,{img_b64}"}},
                    ],
                }
            )
        else:
            messages_openai.append(
                {
                    "role": "user",
                    "content": extra_blocks_openai + [
                        {"type": "text", "text": user_text},
                        {"type": "image_url", "image_url": {"url": f"data:{media_type};base64,{img_b64}"}},
                    ],
                }
            )

        decision, response = _call_agent_openai(client, model, messages_openai)
        choice_message = response.choices[0].message
        messages_openai.append(
            {
                "role": "assistant",
                "content": choice_message.content,
                "tool_calls": choice_message.tool_calls,
            }
        )
        return decision, response

    raise AiCalibrationError(f"Provider agente non supportato: {provider}")
