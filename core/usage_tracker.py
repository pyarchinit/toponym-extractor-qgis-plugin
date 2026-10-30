"""Tracciamento di chiamate/token per motore OCR durante un'estrazione, con
stima approssimativa del costo in USD.

I prezzi sono per milione di token, aggiornati manualmente qui sotto: possono
cambiare nel tempo, quindi il totale mostrato è sempre una STIMA, non una
fattura esatta.
"""

from .i18n import tr

# Modelli disponibili per motore cloud, con prezzo USD per 1M token
# (input, output). Aggiornare se i listini prezzi dei provider cambiano.
# Il primo modello di ogni lista è il default.
CLAUDE_MODELS = [
    ("claude-haiku-4-5-20251001", "Claude Haiku 4.5 (più economico)", 1.00, 5.00),
    ("claude-sonnet-5-5", "Claude Sonnet 5.5 (bilanciato)", 2.00, 10.00),
    ("claude-sonnet-5", "Claude Sonnet 5", 2.00, 10.00),
    ("claude-opus-5-5", "Claude Opus 5.5 (più preciso)", 4.00, 20.00),
]

GPT_MODELS = [
    ("gpt-4o-mini", "GPT-4o mini (più economico)", 0.15, 0.60),
    ("gpt-4o", "GPT-4o (bilanciato)", 2.50, 10.00),
]

MODELS_BY_ENGINE = {
    "claude": CLAUDE_MODELS,
    "gpt": GPT_MODELS,
}


def default_model(engine_name):
    models = MODELS_BY_ENGINE.get(engine_name, [])
    return models[0][0] if models else None


def model_pricing(engine_name, model_id):
    """Ritorna (input_price, output_price) USD/1M token per il modello scelto,
    o None se non trovato."""
    for mid, _, in_price, out_price in MODELS_BY_ENGINE.get(engine_name, []):
        if mid == model_id:
            return in_price, out_price
    return None


# Retro-compatibilità: prezzo del modello di default per motore.
PRICING_PER_MILLION_TOKENS = {
    engine: {"input": models[0][2], "output": models[0][3]}
    for engine, models in MODELS_BY_ENGINE.items()
    if models
}


class UsageTracker:
    """Accumula chiamate e token per (motore, modello) durante un'estrazione."""

    def __init__(self):
        self.tile_count_by_engine = {}
        # Le chiavi delle mappe sotto sono tuple (engine_name, model_id).
        self.calls_by_key = {}
        self.input_tokens_by_key = {}
        self.output_tokens_by_key = {}
        # Errori reali intercettati durante le chiamate ai motori cloud (es.
        # eccezioni di rete/rate-limit/autenticazione): prima venivano SOLO
        # loggati in QgsMessageLog e inghiottiti, risultando all'utente in un
        # generico "motore: none" senza nessun indizio del perché — vedi
        # ocr/cascade.py. Qui si tiene traccia (messaggio, conteggio) di modo
        # che il riepilogo finale mostrato in UI possa segnalarli esplicitamente.
        self.errors_by_message = {}

    def record_tile_engine(self, engine_name):
        """Conta un tile processato con questo motore (anche "tesseract" o "none")."""
        self.tile_count_by_engine[engine_name] = self.tile_count_by_engine.get(engine_name, 0) + 1

    def record_error(self, engine_name, message):
        """Registra un errore reale (eccezione) intercettato durante una
        chiamata a un motore cloud, per poterlo mostrare nel riepilogo finale
        invece di lasciarlo solo nel log applicativo di QGIS."""
        key = f"{engine_name}: {message}"
        self.errors_by_message[key] = self.errors_by_message.get(key, 0) + 1

    def error_count(self):
        return sum(self.errors_by_message.values())

    def record_call(self, engine_name, model_id=None, input_tokens=0, output_tokens=0):
        """Registra una chiamata API a un motore cloud, con i token usati (se noti)."""
        key = (engine_name, model_id or default_model(engine_name))
        self.calls_by_key[key] = self.calls_by_key.get(key, 0) + 1
        self.input_tokens_by_key[key] = self.input_tokens_by_key.get(key, 0) + input_tokens
        self.output_tokens_by_key[key] = self.output_tokens_by_key.get(key, 0) + output_tokens

    def estimated_cost_usd_for_key(self, key):
        engine_name, model_id = key
        pricing = model_pricing(engine_name, model_id)
        if pricing is None:
            return 0.0
        in_price, out_price = pricing
        input_tokens = self.input_tokens_by_key.get(key, 0)
        output_tokens = self.output_tokens_by_key.get(key, 0)
        return input_tokens / 1_000_000 * in_price + output_tokens / 1_000_000 * out_price

    def total_estimated_cost_usd(self):
        return sum(self.estimated_cost_usd_for_key(key) for key in self.calls_by_key)

    def summary_lines(self):
        """Righe testuali pronte da mostrare in UI/log, una per (motore, modello) usato."""
        lines = []
        engines_with_calls = {engine for engine, _ in self.calls_by_key}
        for engine, tiles in sorted(self.tile_count_by_engine.items()):
            if engine not in engines_with_calls:
                lines.append(tr("{engine}: {tiles} quadranti (locale, nessun costo)").format(engine=engine, tiles=tiles))

        for key in sorted(self.calls_by_key):
            engine_name, model_id = key
            calls = self.calls_by_key[key]
            cost = self.estimated_cost_usd_for_key(key)
            in_tok = self.input_tokens_by_key.get(key, 0)
            out_tok = self.output_tokens_by_key.get(key, 0)
            lines.append(
                tr("{engine} ({model}): {calls} chiamate, {tokens_in}+{tokens_out} token, ~${cost:.4f} (stima)").format(
                    engine=engine_name, model=model_id, calls=calls, tokens_in=in_tok, tokens_out=out_tok, cost=cost
                )
            )
        return lines

    def summary_text(self):
        total_cost = self.total_estimated_cost_usd()
        lines = self.summary_lines()
        if total_cost > 0:
            lines.append(tr("Totale stimato: ~${cost:.4f} USD (stima approssimativa)").format(cost=total_cost))
        if self.errors_by_message:
            lines.append("")
            lines.append("⚠ " + tr("Errori durante l'OCR (motore saltato su quei quadranti):"))
            for message, count in sorted(self.errors_by_message.items(), key=lambda kv: -kv[1]):
                lines.append(f"  {message} (x{count})")
        return "\n".join(lines) if lines else tr("Nessun motore OCR utilizzato.")
