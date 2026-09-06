"""
Assainissement de la sortie du modèle de vision.

Portage fidèle de `src/extraction/normalize.ts`. C'est le seul endroit où une
réponse de modèle devient une donnée de confiance : tout ce qui vient de Gemini
passe ici, et rien n'en sort qui n'ait été vérifié.

Le principe directeur, hérité du client : une sortie de modèle est *plausible*
par construction, donc non vérifiée par défaut. Une ligne sans montant lisible
est écartée, pas ramenée à zéro — un zéro se fond dans un total, une ligne
manquante se voit.
"""

from __future__ import annotations

import json
import math
import re
import unicodedata
from typing import Any

from .money import (
    js_number,
    js_round,
    loose_number,
    number_to_string,
    parse_amount_to_cents,
    to_cents,
)
from .types import ExtractedLine, ExtractedTax, ExtractionResult, empty_extraction

CONFIDENCE_SURE = 95
CONFIDENCE_UNSURE = 50

# L'ordre compte : on retient le premier alias dont le libellé est préfixé.
# TPS avant GST, TVQ avant QST — deux écritures d'une même taxe doivent tomber
# sur un code unique, sans quoi elles seraient comptées deux fois.
TAX_ALIASES: dict[str, str] = {
    "TPS": "TPS",
    "GST": "TPS",
    "TVQ": "TVQ",
    "QST": "TVQ",
    "TVH": "TVH",
    "HST": "TVH",
    "TVP": "TVP",
    "PST": "TVP",
    "RST": "TVP",
}

_ISO_DATE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})")
_NON_LETTERS = re.compile(r"[^A-Z]")


def _stringify(value: Any) -> str:
    """`JSON.stringify`, pour nommer une ligne écartée qui n'a même pas de libellé."""
    try:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError):
        return str(value)


def as_string(value: Any) -> str | None:
    """Chaîne non vide, ou None. Un booléen n'est pas du texte et ne le devient pas."""
    if isinstance(value, str):
        trimmed = value.strip()
        return None if trimmed == "" else trimmed
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)) and not math.isnan(value) and not math.isinf(value):
        return number_to_string(value)
    return None


def as_cents(value: Any) -> int | None:
    """
    Montant du modèle → centimes.

    Le modèle répond en JSON : 3,33 peut y arriver sous la forme
    3.3300000000000005, et un montant à trois décimales doit s'arrondir au
    centime — le rejeter ferait disparaître la ligne du ticket. La saisie
    manuelle, elle, reste stricte (`parse_amount_to_cents`).
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return to_cents(value)

    text = as_string(value)
    if text is None:
        return None

    strict = parse_amount_to_cents(text)
    if strict is not None:
        return strict

    loose = loose_number(text)
    if math.isnan(loose) or math.isinf(loose):
        return None
    return to_cents(loose)


def parse_rate_percent(value: Any) -> float | None:
    """
    Taux affiché en pied de ticket. Il n'est qu'un indice : le montant imprimé
    fait foi. Hors de ]0 ; 30], c'est une lecture fautive, pas un taux.
    """
    text = as_string(value)
    if text is None:
        return None
    rate = js_number(text.replace("%", "", 1).replace(",", ".", 1).strip())
    if math.isnan(rate) or math.isinf(rate) or rate <= 0 or rate > 30:
        return None
    return round(rate * 1000) / 1000


def canonical_tax_code(label: str) -> str:
    """« TVQ 9,975 % », « QST » et « qst » désignent la même taxe : un seul code."""
    key = unicodedata.normalize("NFD", label)
    key = "".join(ch for ch in key if not unicodedata.combining(ch))
    key = _NON_LETTERS.sub("", key.upper())
    for alias, code in TAX_ALIASES.items():
        if key.startswith(alias):
            return code
    return "TAXE" if key == "" else key[:6]


def normalize_taxes(raw: Any) -> list[ExtractedTax]:
    """
    Taxes du pied de ticket. Une taxe imprimée deux fois est *additionnée*, non
    choisie : deux lignes TPS sur un même ticket sont deux montants réels.
    """
    if not isinstance(raw, list):
        return []

    taxes: list[ExtractedTax] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        label = as_string(item.get("label"))
        amount_cents = as_cents(item.get("amount"))
        # Une taxe sans montant lisible n'est pas une taxe à zéro : on l'écarte.
        if label is None or amount_cents is None:
            continue
        code = canonical_tax_code(label)
        existing = next((tax for tax in taxes if tax.code == code), None)
        if existing is not None:
            existing.amountCents += amount_cents
            continue
        taxes.append(
            ExtractedTax(
                code=code,
                label=label,
                ratePercent=parse_rate_percent(item.get("rate")),
                amountCents=amount_cents,
            )
        )
    return taxes


def as_quantity(value: Any) -> int:
    """Quantité plausible, ou 1. Une quantité aberrante ne doit pas tuer la ligne."""
    if isinstance(value, bool):
        quantity = math.nan
    elif isinstance(value, (int, float)):
        quantity = float(value)
    else:
        text = as_string(value)
        quantity = math.nan if text is None else js_number(text.replace(",", ".", 1))

    if math.isnan(quantity) or math.isinf(quantity) or quantity < 1 or quantity > 999:
        return 1
    return js_round(quantity)


def as_iso_date(value: Any) -> str | None:
    """Date d'achat en AAAA-MM-JJ. Une date hors du plausible est une hallucination."""
    text = as_string(value)
    if text is None:
        return None
    match = _ISO_DATE.match(text)
    if match is None:
        return None
    year, month, day = int(match.group(1)), int(match.group(2)), int(match.group(3))
    if year < 2000 or year > 2100 or month < 1 or month > 12 or day < 1 or day > 31:
        return None
    return f"{match.group(1)}-{match.group(2)}-{match.group(3)}"


def normalize_line(raw: Any) -> ExtractedLine | str:
    """Une ligne d'article, ou le libellé sous lequel elle a été écartée."""
    if not isinstance(raw, dict):
        return _stringify(raw)

    label = as_string(raw.get("label"))
    description = as_string(raw.get("description"))
    total_cents = as_cents(raw.get("total"))

    # Sans montant, il n'y a rien à répartir : la ligne est nommée, pas inventée.
    if total_cents is None:
        return label if label is not None else _stringify(raw)

    quantity = as_quantity(raw.get("quantity"))
    unit_from_model = as_cents(raw.get("unitPrice"))
    # Le prix unitaire du modèle n'est retenu que s'il tombe juste sur le total.
    # Sinon c'est le total, seul montant réellement imprimé, qui fait foi.
    if unit_from_model is not None and unit_from_model * quantity == total_cents:
        unit_price_cents = unit_from_model
    else:
        unit_price_cents = js_round(total_cents / quantity)

    return ExtractedLine(
        label=label if label is not None else "Article",
        description=description,
        quantity=quantity,
        unitPriceCents=unit_price_cents,
        totalCents=total_cents,
        taxCodes=[] if raw.get("taxable") is False else None,
        confidence=CONFIDENCE_UNSURE if raw.get("uncertain") is True else CONFIDENCE_SURE,
    )


def normalize_extraction(raw: Any) -> ExtractionResult:
    """Point d'entrée unique : sortie brute du modèle → résultat exploitable."""
    if not isinstance(raw, dict):
        return empty_extraction()

    lines: list[ExtractedLine] = []
    discarded: list[str] = []

    raw_lines = raw.get("lines")
    if isinstance(raw_lines, list):
        for item in raw_lines:
            normalized = normalize_line(item)
            if isinstance(normalized, str):
                discarded.append(normalized)
            else:
                lines.append(normalized)

    return ExtractionResult(
        lines=lines,
        taxes=normalize_taxes(raw.get("taxes")),
        statedSubtotalCents=as_cents(raw.get("subtotal")),
        statedTotalCents=as_cents(raw.get("total")),
        merchant=as_string(raw.get("merchant")),
        purchaseDate=as_iso_date(raw.get("purchaseDate")),
        discarded=discarded,
    )
