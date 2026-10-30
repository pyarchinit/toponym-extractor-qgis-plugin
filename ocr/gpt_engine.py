"""Motore OCR di fallback basato su GPT (OpenAI) vision.

Stessa logica di claude_engine: restituisce testo + posizione approssimativa
in pixel per ogni singola etichetta trovata nel tile, mai un punto unico
per l'intero quadrante.
"""

import base64
import json
import os
import re

ENGINE_NAME = "gpt"

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


class GptNotAvailable(Exception):
    pass


class GptResponseError(Exception):
    pass


# Tempo massimo di una singola chiamata (vedi claude_engine).
REQUEST_TIMEOUT_SECONDS = 240.0

_clients = {}


def _get_client(openai, key):
    """Un solo client (e quindi un solo pool di connessioni) per API key,
    riusato per tutti i tile invece di rinegoziare TLS a ogni chiamata."""
    client = _clients.get(key)
    if client is None:
        client = _clients[key] = openai.OpenAI(api_key=key, timeout=REQUEST_TIMEOUT_SECONDS, max_retries=1)
    return client


def _get_api_key(explicit_key=None):
    return explicit_key or os.environ.get("OPENAI_API_KEY")


def _import_deps():
    try:
        import openai
    except ImportError as exc:
        raise GptNotAvailable(
            "Libreria 'openai' non installata nell'ambiente Python di QGIS."
        ) from exc
    return openai


def is_available(explicit_key=None):
    if not _get_api_key(explicit_key):
        return False
    try:
        _import_deps()
        return True
    except GptNotAvailable:
        return False


def recognize_words(image_path, image_width, image_height, api_key=None, model="gpt-4o"):
    """Invia il tile a GPT vision e ritorna (words, usage) dove words è una
    lista di dict {text, left, top, width, height, confidence, rotation_deg}
    e usage è un dict {input_tokens, output_tokens} con i token realmente
    consumati.
    """
    openai = _import_deps()
    key = _get_api_key(api_key)
    if not key:
        raise GptNotAvailable("OPENAI_API_KEY non impostata.")

    with open(image_path, "rb") as f:
        img_b64 = base64.standard_b64encode(f.read()).decode("ascii")

    media_type = "image/png" if image_path.lower().endswith(".png") else "image/jpeg"

    client = _get_client(openai, key)
    response = client.chat.completions.create(
        model=model,
        max_tokens=4096,
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": _PROMPT.format(width=image_width, height=image_height),
                    },
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:{media_type};base64,{img_b64}"},
                    },
                ],
            }
        ],
    )

    # Risposta troncata = JSON incompleto: meglio un errore visibile che un
    # tile con "0 toponimi" in silenzio.
    if response.choices[0].finish_reason == "length":
        raise GptResponseError("risposta troncata (finish_reason=length)")

    raw_text = response.choices[0].message.content or ""
    usage = {
        "input_tokens": getattr(response.usage, "prompt_tokens", 0) if response.usage else 0,
        "output_tokens": getattr(response.usage, "completion_tokens", 0) if response.usage else 0,
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
