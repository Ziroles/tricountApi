"""
Sanitising the vision model's output.

A faithful port of `src/extraction/normalize.ts`. This is the only place where a
model response becomes trusted data: everything coming out of Gemini goes
through here, and nothing leaves that has not been checked.

The guiding principle, inherited from the client: a model output is *plausible*
by construction, therefore unverified by default. A line without a readable
amount is dropped, not zeroed — a zero blends into a total, a missing line shows.
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

# Order matters: we keep the first alias the label starts with. TPS before GST,
# TVQ before QST — two spellings of the same tax must land on a single code,
# otherwise they would be counted twice.
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
    """`JSON.stringify`, to name a dropped line that does not even have a label."""
    try:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError):
        return str(value)


def as_string(value: Any) -> str | None:
    """Non-empty string, or None. A boolean is not text and does not become one."""
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
    Model amount → cents.

    The model answers in JSON: 3.33 can arrive as 3.3300000000000005, and an
    amount with three decimals must round to the cent — rejecting it would make
    the line disappear from the receipt. Manual entry, on the other hand, stays
    strict (`parse_amount_to_cents`).
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
    Rate printed at the foot of the receipt. It is only a hint: the printed
    amount is what counts. Outside ]0 ; 30], it is a misreading, not a rate.
    """
    text = as_string(value)
    if text is None:
        return None
    rate = js_number(text.replace("%", "", 1).replace(",", ".", 1).strip())
    if math.isnan(rate) or math.isinf(rate) or rate <= 0 or rate > 30:
        return None
    return round(rate * 1000) / 1000


def canonical_tax_code(label: str) -> str:
    """`TVQ 9,975 %`, `QST` and `qst` name the same tax: one single code."""
    key = unicodedata.normalize("NFD", label)
    key = "".join(ch for ch in key if not unicodedata.combining(ch))
    key = _NON_LETTERS.sub("", key.upper())
    for alias, code in TAX_ALIASES.items():
        if key.startswith(alias):
            return code
    return "TAX" if key == "" else key[:6]


def normalize_taxes(raw: Any) -> list[ExtractedTax]:
    """
    Taxes from the foot of the receipt. A tax printed twice is *summed*, not
    picked: two GST lines on the same receipt are two real amounts.
    """
    if not isinstance(raw, list):
        return []

    taxes: list[ExtractedTax] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        label = as_string(item.get("label"))
        amount_cents = as_cents(item.get("amount"))
        # A tax without a readable amount is not a zero tax: we drop it.
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
    """Plausible quantity, or 1. An absurd quantity must not kill the line."""
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
    """Purchase date in YYYY-MM-DD. A date outside the plausible is a hallucination."""
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
    """One item line, or the label under which it was dropped."""
    if not isinstance(raw, dict):
        return _stringify(raw)

    label = as_string(raw.get("label"))
    description = as_string(raw.get("description"))
    total_cents = as_cents(raw.get("total"))

    # Without an amount there is nothing to split: the line is named, not invented.
    if total_cents is None:
        return label if label is not None else _stringify(raw)

    quantity = as_quantity(raw.get("quantity"))
    unit_from_model = as_cents(raw.get("unitPrice"))
    # The model's unit price is only kept if it lands exactly on the total.
    # Otherwise the total, the only amount actually printed, is what counts.
    if unit_from_model is not None and unit_from_model * quantity == total_cents:
        unit_price_cents = unit_from_model
    else:
        unit_price_cents = js_round(total_cents / quantity)

    return ExtractedLine(
        label=label if label is not None else "Item",
        description=description,
        quantity=quantity,
        unitPriceCents=unit_price_cents,
        totalCents=total_cents,
        taxCodes=[] if raw.get("taxable") is False else None,
        confidence=CONFIDENCE_UNSURE if raw.get("uncertain") is True else CONFIDENCE_SURE,
    )


def normalize_extraction(raw: Any) -> ExtractionResult:
    """Single entry point: raw model output → usable result."""
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
