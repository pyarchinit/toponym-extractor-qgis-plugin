"""Orchestrazione della pipeline OCR a cascata su un singolo tile.

Livello 1: Tesseract locale (se abilitato nelle impostazioni).
Livello 2: fallback cloud (Claude e/o GPT, a seconda di quali sono
disponibili/configurati) quando Tesseract non trova nulla o la confidenza
media delle parole trovate è sotto soglia.

Il risultato è sempre una lista di "parole" con bbox in pixel relativo al
tile, cioè un punto per singolo toponimo — mai un punto unico per tile.
"""

import time

from qgis.core import QgsMessageLog, Qgis

from . import tesseract_engine
from . import claude_engine
from . import gpt_engine
from ..core.usage_tracker import default_model

LOG_TAG = "Toponimi Extractor"

# Un'estrazione con motore cloud primario (non solo fallback selettivo) può
# chiamare l'API decine o centinaia di volte consecutive, una per tile: senza
# alcun retry, il primo errore transitorio (rate limit 429, timeout di rete)
# fa fallire quel tile con motore "none" — e se il rate limit persiste, FA
# FALLIRE SISTEMATICAMENTE OGNI TILE per il resto dell'estrazione (sintomo
# osservato: "motore none" su tutti i quadranti, nessun toponimo trovato).
# Retry minimale con backoff esponenziale per dare tempo al rate limit di
# rientrare, senza introdurre una libreria esterna.
CLOUD_CALL_MAX_RETRIES = 3
CLOUD_CALL_BACKOFF_SECONDS = 2.0


def _is_transient_error(exc):
    """True per gli errori che ha senso ritentare: rate limit, timeout,
    errori del server, rete. Una API key errata (401), un modello inesistente
    (404) o una risposta non parsabile falliscono identici a ogni tentativo:
    ritentarli costa solo attesa, moltiplicata per ogni tile."""
    status = getattr(exc, "status_code", None)
    if status is not None:
        return status in (408, 409, 429) or status >= 500
    return type(exc).__name__ in ("APIConnectionError", "APITimeoutError")


def _sleep_unless_canceled(seconds, is_canceled):
    """Attesa a piccoli passi, interrotta se l'utente annulla l'estrazione.
    Ritorna False se annullata."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if is_canceled is not None and is_canceled():
            return False
        time.sleep(0.2)
    return True


def _call_with_retry(func, engine_label, max_retries=CLOUD_CALL_MAX_RETRIES, is_canceled=None):
    """Richiama `func()` (una chiamata a un motore cloud) con retry e backoff
    esponenziale sugli errori transitori (rate limit, timeout), che altrimenti
    farebbero fallire in blocco l'intera estrazione tile dopo tile. Rilancia
    subito gli errori permanenti, e l'ultima eccezione se anche l'ultimo
    tentativo fallisce."""
    last_exc = None
    for attempt in range(max_retries + 1):
        try:
            return func()
        except Exception as exc:
            last_exc = exc
            if attempt >= max_retries or not _is_transient_error(exc):
                break
            wait_seconds = CLOUD_CALL_BACKOFF_SECONDS * (2 ** attempt)
            QgsMessageLog.logMessage(
                f"{engine_label}: tentativo {attempt + 1}/{max_retries + 1} fallito ({exc}), "
                f"nuovo tentativo tra {wait_seconds:.0f}s...",
                LOG_TAG, Qgis.Warning,
            )
            if not _sleep_unless_canceled(wait_seconds, is_canceled):
                break
    raise last_exc


def run_cascade(image_path, image_width, image_height, settings, usage_tracker=None, is_canceled=None):
    """settings è un dict con le chiavi:
      - use_tesseract (bool)
      - tesseract_lang (str)
      - tesseract_cmd (str o None)
      - confidence_threshold (float, 0-100)
      - use_cloud_fallback (bool)
      - cloud_provider ("claude", "gpt", "both")
      - anthropic_api_key (str o None, altrimenti da env)
      - anthropic_api_key_env_var (str o None: nome della variabile d'ambiente da
        leggere per la API key Claude, es. "ANTHROPIC_API_KEY" o "CLAUDE_API_KEY";
        se None si prova ANTHROPIC_API_KEY poi CLAUDE_API_KEY)
      - openai_api_key (str o None, altrimenti da env)

    `usage_tracker`, se fornito, viene aggiornato con chiamate/token per ogni
    motore cloud usato (core.usage_tracker.UsageTracker).

    Ritorna (words, engine_used) dove words è la lista di dict prodotta dal
    motore risolutivo e engine_used è il nome del motore che ha fornito il
    risultato finale ("tesseract", "claude", "gpt", o "claude+gpt").
    """
    words = []
    engine_used = None

    if settings.get("use_tesseract", True):
        try:
            if settings.get("tesseract_cmd"):
                tesseract_engine.configure_tesseract_cmd(settings["tesseract_cmd"])
            words = tesseract_engine.recognize_words(
                image_path,
                lang=settings.get("tesseract_lang", "ita"),
                min_confidence=0,
            )
            engine_used = tesseract_engine.ENGINE_NAME
        except tesseract_engine.TesseractNotAvailable as exc:
            QgsMessageLog.logMessage(f"Tesseract non disponibile: {exc}", LOG_TAG, Qgis.Warning)
            if usage_tracker is not None:
                usage_tracker.record_error(tesseract_engine.ENGINE_NAME, str(exc))
            words = []
            engine_used = None
    else:
        QgsMessageLog.logMessage("Tesseract disabilitato nelle impostazioni.", LOG_TAG, Qgis.Info)

    needs_fallback = _needs_cloud_fallback(words, settings)

    if needs_fallback and settings.get("use_cloud_fallback", False):
        provider = settings.get("cloud_provider", "claude")
        fallback_words = []
        used = []

        if provider in ("claude", "both"):
            anthropic_env_var = settings.get("anthropic_api_key_env_var")
            claude_model = settings.get("claude_model") or default_model("claude")
            if claude_engine.is_available(settings.get("anthropic_api_key"), anthropic_env_var):
                try:
                    claude_words, claude_usage = _call_with_retry(
                        lambda: claude_engine.recognize_words(
                            image_path,
                            image_width,
                            image_height,
                            api_key=settings.get("anthropic_api_key"),
                            env_var=anthropic_env_var,
                            model=claude_model,
                        ),
                        "Claude",
                        is_canceled=is_canceled,
                    )
                    fallback_words.extend(claude_words)
                    used.append(claude_engine.ENGINE_NAME)
                    if usage_tracker is not None:
                        usage_tracker.record_call(
                            claude_engine.ENGINE_NAME,
                            model_id=claude_model,
                            input_tokens=claude_usage.get("input_tokens", 0),
                            output_tokens=claude_usage.get("output_tokens", 0),
                        )
                except Exception as exc:
                    QgsMessageLog.logMessage(f"Errore fallback Claude: {exc}", LOG_TAG, Qgis.Warning)
                    if usage_tracker is not None:
                        usage_tracker.record_error(claude_engine.ENGINE_NAME, str(exc))
            else:
                QgsMessageLog.logMessage(
                    "Fallback Claude non disponibile (libreria 'anthropic' mancante o API key assente).",
                    LOG_TAG,
                    Qgis.Warning,
                )
                if usage_tracker is not None:
                    usage_tracker.record_error(
                        claude_engine.ENGINE_NAME, "non disponibile (libreria mancante o API key assente)"
                    )

        if provider in ("gpt", "both"):
            gpt_model = settings.get("gpt_model") or default_model("gpt")
            if gpt_engine.is_available(settings.get("openai_api_key")):
                try:
                    gpt_words, gpt_usage = _call_with_retry(
                        lambda: gpt_engine.recognize_words(
                            image_path,
                            image_width,
                            image_height,
                            api_key=settings.get("openai_api_key"),
                            model=gpt_model,
                        ),
                        "GPT",
                        is_canceled=is_canceled,
                    )
                    fallback_words.extend(gpt_words)
                    used.append(gpt_engine.ENGINE_NAME)
                    if usage_tracker is not None:
                        usage_tracker.record_call(
                            gpt_engine.ENGINE_NAME,
                            model_id=gpt_model,
                            input_tokens=gpt_usage.get("input_tokens", 0),
                            output_tokens=gpt_usage.get("output_tokens", 0),
                        )
                except Exception as exc:
                    QgsMessageLog.logMessage(f"Errore fallback GPT: {exc}", LOG_TAG, Qgis.Warning)
                    if usage_tracker is not None:
                        usage_tracker.record_error(gpt_engine.ENGINE_NAME, str(exc))
            else:
                QgsMessageLog.logMessage(
                    "Fallback GPT non disponibile (libreria 'openai' mancante o API key assente).",
                    LOG_TAG,
                    Qgis.Warning,
                )
                if usage_tracker is not None:
                    usage_tracker.record_error(
                        gpt_engine.ENGINE_NAME, "non disponibile (libreria mancante o API key assente)"
                    )

        if fallback_words:
            words = fallback_words
            engine_used = "+".join(used) if used else engine_used
    elif needs_fallback:
        QgsMessageLog.logMessage(
            "Nessuna parola trovata da Tesseract e fallback cloud disattivato nelle impostazioni.",
            LOG_TAG,
            Qgis.Info,
        )

    return words, (engine_used or "none")


def _needs_cloud_fallback(words, settings):
    if not words:
        return True
    threshold = settings.get("confidence_threshold", 60.0)
    avg_conf = sum(w["confidence"] for w in words) / len(words)
    return avg_conf < threshold
