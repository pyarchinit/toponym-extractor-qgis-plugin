"""Motore OCR di fallback basato su Claude (Anthropic) vision.

Usato quando Tesseract non trova testo in un tile o lo trova con confidenza
troppo bassa (es. testo inclinato, font decorativo, sovrapposizioni). Al
modello viene chiesto di restituire non solo il testo ma anche il bounding
box approssimativo IN PIXEL di ciascuna singola etichetta/toponimo trovato
nell'immagine, in modo da poter georeferenziare ogni parola separatamente
(mai un punto unico per l'intero tile).
"""

import base64
import json
import os
import re

ENGINE_NAME = "claude"

DEFAULT_API_KEY_ENV_VARS = ("ANTHROPIC_API_KEY", "CLAUDE_API_KEY")

_PROMPT = """Analizza questa immagine ritagliata da una mappa raster. Individua \
tutte le etichette testuali (toponimi: nomi di luoghi, fiumi, monti, vie, ecc.) \
presenti, anche se il testo è ruotato, curvo o di bassa qualità.

Per OGNI etichetta trovata restituisci un oggetto JSON con:
- "text": il testo esatto dell'etichetta
- "x": coordinata orizzontale in pixel del CENTRO del testo (0 = bordo sinistro dell'immagine)
- "y": coordinata verticale in pixel del CENTRO del testo (0 = bordo superiore dell'immagine)
- "rotation_deg": angolo di rotazione stimato del testo in gradi (0 = orizzontale)
- "confidence": una stima di confidenza da 0 a 100

IMPORTANTE: un toponimo può essere scritto su DUE O PIÙ RIGHE ravvicinate, con \
lo stesso carattere e colore (es. "Póggio" sopra e "Berni" sotto, oppure "San \
Martino" sopra e "in Converseto" sotto). In quel caso è UNA SOLA etichetta: \
restituiscila come un unico oggetto, con le righe unite da uno spazio nell'ordine \
di lettura ("Póggio Berni") e con x, y al centro dell'intero blocco di testo. \
Non unire invece etichette diverse (es. un nome e una quota numerica, o due \
toponimi distinti lontani tra loro).

Rispondi SOLO con un array JSON valido, senza testo aggiuntivo. Se non trovi \
nessuna etichetta, rispondi con [].

Dimensioni immagine: {width}x{height} pixel."""


class ClaudeNotAvailable(Exception):
    pass


class ClaudeResponseError(Exception):
    pass


# I modelli con thinking sempre attivo lo conteggiano dentro max_tokens: serve
# margine oltre al JSON di risposta, anche su tile densi di toponimi.
MAX_OUTPUT_TOKENS = 16000

# Tempo massimo di una singola chiamata. Senza limite esplicito la libreria
# aspetta fino a 10 minuti e poi ritenta due volte: mezz'ora di attesa muta.
REQUEST_TIMEOUT_SECONDS = 240.0

_clients = {}


def _get_client(anthropic, key):
    """Un solo client (e quindi un solo pool di connessioni) per API key,
    riusato per tutti i tile invece di rinegoziare TLS a ogni chiamata."""
    client = _clients.get(key)
    if client is None:
        client = _clients[key] = anthropic.Anthropic(
            api_key=key, timeout=REQUEST_TIMEOUT_SECONDS, max_retries=1
        )
    return client


def _candidate_env_vars(env_var=None):
    """Variabili d'ambiente da controllare, nell'ordine. La variabile scelta
    esplicitamente (dal dialogo Impostazioni, o il suo default
    "ANTHROPIC_API_KEY") ha sempre la precedenza, ma se non è impostata
    nell'ambiente proviamo comunque le altre variabili standard prima di
    arrenderci: altrimenti un utente con solo CLAUDE_API_KEY impostata (mai
    toccato il dialogo, quindi env_var="ANTHROPIC_API_KEY" di default) si
    ritroverebbe Claude "non disponibile" senza una causa evidente, pur
    avendo una chiave valida nell'ambiente."""
    if not env_var:
        return DEFAULT_API_KEY_ENV_VARS
    ordered = (env_var,) + DEFAULT_API_KEY_ENV_VARS
    seen = set()
    result = []
    for name in ordered:
        if name and name not in seen:
            seen.add(name)
            result.append(name)
    return tuple(result)


def _get_api_key(explicit_key=None, env_var=None):
    if explicit_key:
        return explicit_key
    for name in _candidate_env_vars(env_var):
        value = os.environ.get(name)
        if value:
            return value
    return None


def _import_deps():
    try:
        import anthropic
    except ImportError as exc:
        raise ClaudeNotAvailable(
            "Libreria 'anthropic' non installata nell'ambiente Python di QGIS."
        ) from exc
    return anthropic


def is_available(explicit_key=None, env_var=None):
    if not _get_api_key(explicit_key, env_var):
        return False
    try:
        _import_deps()
        return True
    except ClaudeNotAvailable:
        return False


def recognize_words(
    image_path, image_width, image_height, api_key=None, env_var=None, model="claude-sonnet-5"
):
    """Invia il tile a Claude vision e ritorna (words, usage) dove words è una
    lista di dict {text, left, top, width, height, confidence, rotation_deg}
    (bounding box in pixel stimato dal centro) e usage è un dict
    {input_tokens, output_tokens} con i token realmente consumati.
    """
    anthropic = _import_deps()
    key = _get_api_key(api_key, env_var)
    if not key:
        raise ClaudeNotAvailable(
            f"Nessuna API key trovata (variabili controllate: {', '.join(_candidate_env_vars(env_var))})."
        )

    with open(image_path, "rb") as f:
        img_b64 = base64.standard_b64encode(f.read()).decode("ascii")

    media_type = "image/png" if image_path.lower().endswith(".png") else "image/jpeg"

    client = _get_client(anthropic, key)
    message = client.messages.create(
        model=model,
        max_tokens=MAX_OUTPUT_TOKENS,
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": media_type,
                            "data": img_b64,
                        },
                    },
                    {
                        "type": "text",
                        "text": _PROMPT.format(width=image_width, height=image_height),
                    },
                ],
            }
        ],
    )

    # Una risposta troncata o rifiutata contiene un JSON incompleto o nessun
    # JSON: senza questo controllo il tile risulterebbe "0 toponimi" in silenzio.
    if message.stop_reason in ("max_tokens", "refusal"):
        raise ClaudeResponseError(f"risposta non utilizzabile (stop_reason={message.stop_reason})")

    raw_text = "".join(
        block.text for block in message.content if getattr(block, "type", None) == "text"
    )
    usage = {
        "input_tokens": getattr(message.usage, "input_tokens", 0) if message.usage else 0,
        "output_tokens": getattr(message.usage, "output_tokens", 0) if message.usage else 0,
    }
    return _parse_response(raw_text), usage


def _parse_response(raw_text):
    match = re.search(r"\[.*\]", raw_text, re.DOTALL)
    if not match:
        return []
    try:
        items = json.loads(match.group(0))
    except json.JSONDecodeError:
        return []

    words = []
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        text = str(item.get("text", "")).strip()
        if not text:
            continue
        try:
            cx = float(item.get("x", 0))
            cy = float(item.get("y", 0))
            confidence = float(item.get("confidence", 50.0))
            rotation_deg = float(item.get("rotation_deg", 0.0))
        except (TypeError, ValueError):
            continue
        # Stima larghezza/altezza del bbox a partire dalla lunghezza del testo,
        # dato che il modello ritorna solo il centro.
        approx_w = max(10, len(text) * 10)
        approx_h = 20
        words.append(
            {
                "text": text,
                "left": int(cx - approx_w / 2),
                "top": int(cy - approx_h / 2),
                "width": int(approx_w),
                "height": int(approx_h),
                "confidence": confidence,
                "rotation_deg": rotation_deg,
            }
        )
    return words
