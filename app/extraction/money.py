"""
Monetary arithmetic in integer cents.

A faithful port of `src/lib/money.ts` on the PWA side. The application has never
let a float represent money, and this port must not be the occasion to introduce
one: the tests in `tests/test_money.py` are the same as those in
`money.test.ts`, and that is deliberate.

Two roundings coexist, as in JavaScript, and they are not interchangeable:
 - `js_round` reproduces `Math.round`: halves go towards +∞ (−175.5 → −175);
 - `round_half_up` reproduces the client's `roundHalfUp`: halves move away from
   zero (−175.5 → −176).
Confusing the two shifts cents on negative lines, that is, on printed discounts.
"""

from __future__ import annotations

import math
import re

# 2^53 − 1: beyond this, JavaScript no longer guarantees integer exactness, and
# `Number.isSafeInteger` refuses. We refuse likewise, to return the same nulls.
MAX_SAFE_INTEGER = 2**53 - 1

# Ordinary, non-breaking and narrow no-break spaces: all three turn up in
# amounts copied from a receipt.
_SPACES = re.compile(r"[\s  ]")
_CURRENCY_EDGES = re.compile(r"^[$€]+|[$€]+$")
_AMOUNT = re.compile(r"^(-?)(\d*)(?:[.,](\d{0,2}))?$")
_LOOSE_NOISE = re.compile(r"[\s  $€]")


def js_round(value: float) -> int:
    """JavaScript's `Math.round`: halves go towards +∞, not away from zero."""
    return math.floor(value + 0.5)


def round_half_up(value: float) -> int:
    """Symmetric rounding: halves move away from zero, in both directions."""
    if value < 0:
        return -js_round(-value)
    return js_round(value)


def parse_amount_to_cents(raw: object) -> int | None:
    """
    Typed or printed amount → integer cents, or None if it is not one.

    Strict by construction: at most two decimals, comma or dot, a single sign.
    Anything that does not fit that mould is not guessed, it is refused — the
    caller then decides whether to drop the line or attempt a more tolerant
    reading (see `as_cents` in normalize.py).
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
    # A lone "-", a lone ",": a sign or a separator does not make an amount.
    if whole == "" and not decimal_raw:
        return None

    decimals = (decimal_raw or "").ljust(2, "0")
    cents = int(whole or "0") * 100 + int(decimals)
    if cents > MAX_SAFE_INTEGER:
        return None
    return -cents if sign == "-" else cents


def to_cents(amount: float) -> int | None:
    """Number in currency units → cents, refusing infinity and NaN."""
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
    JavaScript's `String(number)`. Useful because the model may return a number
    where a string was asked for: JS writes "5" where Python would write "5.0",
    and the difference would surface all the way up to the displayed label.
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
    JavaScript's `Number(string)`, cut down to what we need: returns NaN rather
    than raising, because the caller tests for finiteness, not for exceptions.
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
    Tolerant reading of an amount: strip noise and currency, then turn the first
    comma into a dot. JavaScript's `replace(',', '.')` only acts on the first
    occurrence — reproducing that detail means "1,234,56" stays unreadable here
    too, instead of being misread.
    """
    return js_number(_LOOSE_NOISE.sub("", text).replace(",", ".", 1))
