"""Ricerca dei toponimi duplicati generati dal plugin stesso, cioè dalla
sovrapposizione (overlap) tra quadranti adiacenti.

Perché esistono: `core.grid.build_grid` genera quadranti che si sovrappongono
lungo i bordi per non tagliare a metà un nome. Un toponimo che cade nella
fascia di sovrapposizione viene quindi letto due volte, una per quadrante, e
produce due punti vicinissimi.

Il rischio da evitare è l'opposto: due luoghi distinti con lo stesso nome,
anche a poca distanza (due "Ca' Nova", due "Molino"), sono due etichette
vere sulla carta e NON vanno uniti. Per questo due risultati sono
considerati duplicati solo se valgono TUTTE queste condizioni:

  1. provengono da due quadranti diversi: due nomi uguali letti nella stessa
     immagine sono due scritte distinte sulla carta, non un doppione;
  2. entrambi i punti cadono nella zona che i due quadranti hanno in comune
     (con la tolleranza della distanza massima): solo lì il plugin può aver
     letto due volte la stessa scritta;
  3. il testo è uguale o molto simile (maiuscole, accenti, un carattere
     letto diversamente, oppure un nome troncato sul bordo del quadrante);
  4. la distanza tra i due punti non supera quella scelta dall'utente.

Di ogni gruppo si tiene la lettura più completa (il nome più lungo, se
l'altro è un troncamento) e, a parità, quella con confidenza più alta.
"""

import difflib
import re
import unicodedata

DEFAULT_MAX_DISTANCE = 20.0

# Somiglianza minima (0-1) perché due testi non identici siano la stessa
# scritta letta in modo leggermente diverso. Volutamente alta: "Monte Alto" e
# "Monte Basso" devono restare distinti.
MIN_TEXT_SIMILARITY = 0.85

# Lunghezza minima perché un testo possa essere considerato il troncamento di
# un altro (evita di unire sigle o frammenti di una o due lettere).
MIN_TRUNCATED_LENGTH = 4


def _normalize_text(text):
    """Normalizza il testo per il confronto: minuscolo, spazi collassati,
    apostrofi e trattini tipografici uniformati a quelli base."""
    text = text.strip().lower()
    text = text.replace("’", "'").replace("‘", "'")
    text = text.replace("‐", "-").replace("–", "-").replace("—", "-")
    text = re.sub(r"\s+", " ", text)
    return text


def _normalize_text_strict(text):
    """Come _normalize_text, ma senza segni diacritici: un motore OCR può
    trascrivere un accento e l'altro no ("Poggio" / "Póggio")."""
    normalized = _normalize_text(text)
    decomposed = unicodedata.normalize("NFKD", normalized)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def texts_match(text_a, text_b):
    """True se i due testi sono la stessa scritta: uguali, quasi uguali, o
    uno il troncamento dell'altro a inizio/fine (nome tagliato dal bordo)."""
    a = _normalize_text_strict(text_a)
    b = _normalize_text_strict(text_b)
    if not a or not b:
        return False
    if a == b:
        return True

    shorter, longer = (a, b) if len(a) <= len(b) else (b, a)
    if len(shorter) >= MIN_TRUNCATED_LENGTH and (longer.startswith(shorter) or longer.endswith(shorter)):
        return True

    return difflib.SequenceMatcher(None, a, b).ratio() >= MIN_TEXT_SIMILARITY


def _tile_id(result):
    row, col = result.get("tile_row"), result.get("tile_col")
    if row is None or col is None:
        return None
    return row, col


def _inside_tile(result, tile_owner, margin):
    """True se il punto di `result` cade dentro il quadrante da cui proviene
    `tile_owner`, allargato di `margin`. Senza i dati del quadrante non si
    può escludere nulla: True."""
    try:
        return (
            tile_owner["tile_xmin"] - margin <= result["x"] <= tile_owner["tile_xmax"] + margin
            and tile_owner["tile_ymin"] - margin <= result["y"] <= tile_owner["tile_ymax"] + margin
        )
    except (KeyError, TypeError):
        return True


def _euclidean(result_a, result_b):
    dx = result_a["x"] - result_b["x"]
    dy = result_a["y"] - result_b["y"]
    return (dx * dx + dy * dy) ** 0.5


def is_plugin_duplicate(result_a, result_b, max_distance, distance_fn=None):
    """True se i due risultati sono la stessa scritta letta due volte dal
    plugin (vedi i quattro criteri in testa al modulo). `distance_fn(a, b)`
    ritorna la distanza nelle stesse unità di `max_distance`; di default è la
    distanza euclidea nelle unità del CRS."""
    tile_a, tile_b = _tile_id(result_a), _tile_id(result_b)
    if tile_a is None or tile_b is None or tile_a == tile_b:
        return False

    distance = (distance_fn or _euclidean)(result_a, result_b)
    if distance > max_distance:
        return False

    # Il margine è nelle unità del CRS: la distanza reale tra i due punti è
    # sempre una stima valida, qualunque unità usi distance_fn.
    margin = _euclidean(result_a, result_b)
    if not (_inside_tile(result_a, result_b, margin) and _inside_tile(result_b, result_a, margin)):
        return False

    return texts_match(result_a.get("toponimo", ""), result_b.get("toponimo", ""))


def find_duplicate_groups(results, max_distance=DEFAULT_MAX_DISTANCE, distance_fn=None):
    """Ritorna i gruppi di duplicati come liste di INDICI in `results`. Se A
    è duplicato di B e B di C, finiscono tutti nello stesso gruppo (succede
    agli incroci della griglia, dove si sovrappongono fino a quattro quadranti).
    """
    n = len(results)
    parent = list(range(n))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i in range(n):
        for j in range(i + 1, n):
            if find(i) != find(j) and is_plugin_duplicate(results[i], results[j], max_distance, distance_fn):
                parent[find(i)] = find(j)

    groups = {}
    for idx in range(n):
        groups.setdefault(find(idx), []).append(idx)
    return [g for g in groups.values() if len(g) > 1]


def _best_of_group(results, group):
    """Indice della lettura da tenere: la più completa (testo più lungo) e, a
    parità di lunghezza, quella con confidenza più alta."""
    return max(
        group,
        key=lambda i: (len(_normalize_text_strict(results[i].get("toponimo", ""))), results[i].get("confidence", 0.0)),
    )


def deduplicate(results, max_distance=DEFAULT_MAX_DISTANCE, distance_fn=None):
    """Ritorna (kept_results, removed_count, groups_report) dove:
    - kept_results: i risultati senza i doppioni (uno solo per gruppo);
    - removed_count: quanti risultati sono stati scartati;
    - groups_report: una riga leggibile per gruppo, con cosa viene tenuto,
      cosa scartato e a che distanza, per farlo controllare all'utente.
    """
    measure = distance_fn or _euclidean
    indices_to_drop = set()
    groups_report = []

    for group in find_duplicate_groups(results, max_distance, distance_fn):
        best_idx = _best_of_group(results, group)
        best = results[best_idx]
        dropped = []
        for idx in group:
            if idx == best_idx:
                continue
            indices_to_drop.add(idx)
            other = results[idx]
            dropped.append(
                f"\"{other.get('toponimo', '')}\" a {measure(best, other):.0f} m "
                f"(quadrante {other.get('tile_row')},{other.get('tile_col')}, "
                f"confidenza {other.get('confidence', 0.0):.0f}%)"
            )
        groups_report.append(
            f"TENGO \"{best.get('toponimo', '')}\" (quadrante {best.get('tile_row')},{best.get('tile_col')}, "
            f"confidenza {best.get('confidence', 0.0):.0f}%)\n   scarto: " + "; ".join(dropped)
        )

    kept_results = [r for i, r in enumerate(results) if i not in indices_to_drop]
    return kept_results, len(indices_to_drop), groups_report
