"""
Arithmétique monétaire en centimes entiers.

Portage fidèle de `src/lib/money.ts` côté PWA. L'application n'a jamais laissé
un flottant représenter de l'argent, et ce portage ne doit pas être l'occasion
d'en introduire un : les tests de `tests/test_money.py` sont les mêmes que ceux
de `money.test.ts`, et c'est délibéré.

Deux arrondis cohabitent, comme en JavaScript, et ils ne sont pas
interchangeables :
 - `js_round` reproduit `Math.round` : la moitié part vers +∞ (−175,5 → −175) ;
 - `round_half_up` reproduit le `roundHalfUp` du client : la moitié s'éloigne de
   zéro (−175,5 → −176).
Confondre les deux décale des centimes sur les lignes négatives, c'est-à-dire
sur les remises imprimées.
"""

from __future__ import annotations

import math
import re

# 2^53 − 1 : au-delà, JavaScript ne garantit plus l'exactitude d'un entier, et
# `Number.isSafeInteger` refuse. On refuse pareil, pour rendre les mêmes null.
MAX_SAFE_INTEGER = 2**53 - 1

# Espaces ordinaires, insécables et insécables fins : les trois se rencontrent
# dans les montants recopiés depuis un ticket.
_SPACES = re.compile(r"[\s  ]")
_CURRENCY_EDGES = re.compile(r"^[$€]+|[$€]+$")
_AMOUNT = re.compile(r"^(-?)(\d*)(?:[.,](\d{0,2}))?$")
_LOOSE_NOISE = re.compile(r"[\s  $€]")


def js_round(value: float) -> int:
    """`Math.round` de JavaScript : la moitié va vers +∞, pas away-from-zero."""
    return math.floor(value + 0.5)


def round_half_up(value: float) -> int:
    """Arrondi symétrique : la moitié s'éloigne de zéro, dans les deux sens."""
    if value < 0:
        return -js_round(-value)
    return js_round(value)


def parse_amount_to_cents(raw: object) -> int | None:
    """
    Montant saisi ou imprimé → centimes entiers, ou None si ce n'en est pas un.

    Strict par construction : deux décimales au plus, virgule ou point, un seul
    signe. Ce qui ne rentre pas dans ce moule n'est pas deviné, il est refusé —
    l'appelant décide alors s'il écarte la ligne ou tente une lecture plus
    tolérante (voir `as_cents` dans normalize.py).
    """
    if not isinstance(raw, str):
        return None

    cleaned = _SPACES.sub("", raw)
    cleaned = _CURRENCY_EDGES.sub("", cleaned)
    if cleaned.startswith("+"):
        cleaned = cleaned[1:]
    if cleaned in ("", "-"):
        return None

    match = _AMOUNT.match(cleaned)
    if match is None:
        return None

    sign, whole, decimal_raw = match.group(1), match.group(2) or "", match.group(3)
    # « - » seul, « , » seul : un signe ou un séparateur ne fait pas un montant.
    if whole == "" and not decimal_raw:
        return None

    decimals = (decimal_raw or "").ljust(2, "0")
    cents = int(whole or "0") * 100 + int(decimals)
    if cents > MAX_SAFE_INTEGER:
        return None
    return -cents if sign == "-" else cents


def to_cents(amount: float) -> int | None:
    """Nombre en unités monétaires → centimes, en refusant l'infini et le NaN."""
    if not isinstance(amount, (int, float)) or isinstance(amount, bool):
        return None
    if math.isnan(amount) or math.isinf(amount):
        return None
    cents = round_half_up(amount * 100)
    if abs(cents) > MAX_SAFE_INTEGER:
        return None
    return cents


def number_to_string(value: float) -> str:
    """
    `String(nombre)` de JavaScript. Utile parce que le modèle peut rendre un
    nombre là où une chaîne était demandée : JS écrit « 5 » quand Python écrirait
    « 5.0 », et la différence remonterait jusqu'au libellé affiché.
    """
    if isinstance(value, int):
        return str(value)
    if math.isnan(value):
        return "NaN"
    if math.isinf(value):
        return "Infinity" if value > 0 else "-Infinity"
    if value == int(value) and abs(value) < 1e21:
        return str(int(value))
    return repr(value)


def js_number(text: str) -> float:
    """
    `Number(chaîne)` de JavaScript, réduit à ce dont on a besoin : renvoie NaN
    plutôt que de lever, parce que l'appelant teste la finitude, pas l'exception.
    """
    stripped = text.strip()
    if stripped == "":
        return 0.0
    try:
        return float(stripped)
    except ValueError:
        return math.nan


def loose_number(text: str) -> float:
    """
    Lecture tolérante d'un montant : on retire bruit et devise, puis on ramène
    la première virgule à un point. `replace(',', '.')` de JavaScript n'agit que
    sur la première occurrence — reproduire ce détail fait que « 1,234,56 »
    reste illisible ici aussi, au lieu d'être lu de travers.
    """
    return js_number(_LOOSE_NOISE.sub("", text).replace(",", ".", 1))
