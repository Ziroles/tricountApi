"""
Port of `src/extraction/normalize.test.ts`, case for case.

These tests are the safety net of the port: they describe what a model output is
allowed to become. As long as they pass identically in Python, the OCR can
switch sides without the split changing its result.
"""

from __future__ import annotations

import math

import pytest

from app.extraction.normalize import (
    canonical_tax_code,
    normalize_extraction,
    parse_rate_percent,
)

RECEIPT = {
    "merchant": "IGA EXTRA",
    "purchaseDate": "2026-03-14",
    "subtotal": "16,95",
    "total": "19,20",
    "taxes": [
        {"label": "TPS", "rate": "5", "amount": "0,75"},
        {"label": "TVQ", "rate": "9,975", "amount": "1,50"},
    ],
    "lines": [
        {"label": "SLICED BREAD", "quantity": 1, "unitPrice": "3,49", "total": "3,49", "taxable": False},
        {"label": "PLAIN YOGURT", "quantity": 2, "unitPrice": "1,50", "total": "3,00", "taxable": False},
        {"label": "DISH SOAP", "quantity": 1, "unitPrice": "10,46", "total": "10,46", "taxable": True},
    ],
}


# ── happy path ────────────────────────────────────────────────────────────────


def test_converts_amounts_to_integer_cents() -> None:
    result = normalize_extraction(RECEIPT)
    assert [line.totalCents for line in result.lines] == [349, 300, 1046]
    assert result.statedSubtotalCents == 1695
    assert result.statedTotalCents == 1920


def test_reads_the_taxes_at_the_foot_of_the_receipt() -> None:
    result = normalize_extraction(RECEIPT)
    assert [(t.code, t.label, t.ratePercent, t.amountCents) for t in result.taxes] == [
        ("TPS", "TPS", 5, 75),
        ("TVQ", "TVQ", 9.975, 150),
    ]


def test_marks_tax_exempt_lines_and_only_those() -> None:
    bread, yogurt, soap = normalize_extraction(RECEIPT).lines
    assert bread.taxCodes == []
    assert yogurt.taxCodes == []
    assert soap.taxCodes is None


def test_keeps_quantity_and_unit_price() -> None:
    _, yogurt, _ = normalize_extraction(RECEIPT).lines
    assert (yogurt.quantity, yogurt.unitPriceCents) == (2, 150)


def test_reads_the_merchant_and_the_date() -> None:
    result = normalize_extraction(RECEIPT)
    assert result.merchant == "IGA EXTRA"
    assert result.purchaseDate == "2026-03-14"


def test_reads_the_decoded_description_when_provided() -> None:
    result = normalize_extraction(
        {
            "lines": [
                {"label": "CR GCE VAN", "description": "Vanilla ice cream", "total": "4,99"},
                {"label": "POM MCINT", "total": "2,99"},
            ]
        }
    )
    assert result.lines[0].description == "Vanilla ice cream"
    assert result.lines[1].description is None


def test_subtotal_plus_taxes_lands_on_the_printed_total() -> None:
    result = normalize_extraction(RECEIPT)
    taxes = sum(tax.amountCents for tax in result.taxes)
    assert (result.statedSubtotalCents or 0) + taxes == result.statedTotalCents


# ── faulty model output ───────────────────────────────────────────────────────


def test_drops_a_line_without_a_readable_amount_rather_than_zeroing_it() -> None:
    result = normalize_extraction(
        {
            "lines": [
                {"label": "BREAD", "total": "1,05"},
                {"label": "UNREADABLE", "total": "about 3 euros"},
                {"label": "NOTHING"},
            ]
        }
    )
    assert len(result.lines) == 1
    assert result.discarded == ["UNREADABLE", "NOTHING"]


def test_accepts_a_number_where_a_string_was_asked_for() -> None:
    result = normalize_extraction({"lines": [{"label": "BREAD", "total": 1.05}]})
    assert result.lines[0].totalCents == 105


def test_recomputes_the_unit_price_when_it_does_not_land_on_the_total() -> None:
    result = normalize_extraction(
        {"lines": [{"label": "APPLES", "quantity": 3, "unitPrice": "0,99", "total": "2,98"}]}
    )
    assert (result.lines[0].totalCents, result.lines[0].unitPriceCents) == (298, 99)


def test_keeps_printed_discounts_as_negatives() -> None:
    result = normalize_extraction({"lines": [{"label": "LOYALTY DISCOUNT", "total": "-2,50"}]})
    assert result.lines[0].totalCents == -250


def test_brings_an_absurd_quantity_back_to_one() -> None:
    result = normalize_extraction(
        {
            "lines": [
                {"label": "A", "total": "1,00", "quantity": 0},
                {"label": "B", "total": "1,00", "quantity": -3},
                {"label": "C", "total": "1,00", "quantity": 5000},
                {"label": "D", "total": "1,00", "quantity": 2.4},
            ]
        }
    )
    assert [line.quantity for line in result.lines] == [1, 1, 1, 2]


def test_marks_the_lines_the_model_calls_uncertain() -> None:
    result = normalize_extraction(
        {
            "lines": [
                {"label": "SURE", "total": "1,00"},
                {"label": "DOUBTFUL", "total": "2,00", "uncertain": True},
            ]
        }
    )
    assert result.lines[0].confidence >= 70
    assert result.lines[1].confidence < 70


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("14/03/2026", None),
        ("1789-07-14", None),
        ("2026-13-45", None),
        ("2026-03-14", "2026-03-14"),
    ],
)
def test_refuses_an_invented_or_malformed_date(raw: str, expected: str | None) -> None:
    assert normalize_extraction({"purchaseDate": raw}).purchaseDate == expected


@pytest.mark.parametrize("value", [None, 42, "text", [], {}])
def test_survives_an_empty_null_or_wrongly_typed_response(value: object) -> None:
    result = normalize_extraction(value)
    assert result.lines == []
    assert result.statedTotalCents is None


def test_never_loses_an_amount_in_a_float() -> None:
    result = normalize_extraction(
        {
            "lines": [
                {"label": "A", "total": "0,07"},
                {"label": "B", "total": "29,29"},
                {"label": "C", "total": "1234,56"},
            ]
        }
    )
    assert [line.totalCents for line in result.lines] == [7, 2929, 123456]


# ── taxes ─────────────────────────────────────────────────────────────────────


def test_brings_english_and_french_labels_to_the_same_code() -> None:
    assert canonical_tax_code("TPS") == "TPS"
    assert canonical_tax_code("GST") == "TPS"
    assert canonical_tax_code("TVQ 9,975%") == "TVQ"
    assert canonical_tax_code("QST") == "TVQ"
    assert canonical_tax_code("HST") == "TVH"
    assert canonical_tax_code("PST") == "TVP"


def test_sums_a_tax_printed_twice() -> None:
    result = normalize_extraction(
        {"taxes": [{"label": "TPS", "amount": "1,00"}, {"label": "GST", "amount": "0,50"}], "lines": []}
    )
    assert len(result.taxes) == 1
    assert result.taxes[0].amountCents == 150


def test_drops_a_tax_without_a_readable_amount() -> None:
    result = normalize_extraction(
        {
            "taxes": [{"label": "TPS"}, {"label": "TVQ", "amount": "unreadable"}, {"amount": "1,00"}],
            "lines": [],
        }
    )
    assert result.taxes == []


def test_accepts_a_zero_tax() -> None:
    """A zero tax is information — a zero-rated regime — not an absence."""
    result = normalize_extraction({"taxes": [{"label": "TPS", "amount": "0,00"}], "lines": []})
    assert result.taxes[0].amountCents == 0


def test_survives_taxes_that_are_not_an_array() -> None:
    assert normalize_extraction({"taxes": "TPS 1,00", "lines": []}).taxes == []


# ── rates ─────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("raw", "expected"), [("5", 5), ("9,975", 9.975), ("13 %", 13), ("14.975", 14.975)]
)
def test_reads_a_rate(raw: str, expected: float) -> None:
    assert parse_rate_percent(raw) == expected


@pytest.mark.parametrize("raw", ["", "0", "-5", "95", "A", None, {}])
def test_drops_an_implausible_rate(raw: object) -> None:
    assert parse_rate_percent(raw) is None


# ── amounts to the cent ───────────────────────────────────────────────────────


def _line_of(**raw: object) -> object:
    return normalize_extraction({"lines": [{"label": "Item", "quantity": 1, **raw}]})


def test_accepts_json_numbers_as_well_as_strings() -> None:
    assert _line_of(total=10.5).lines[0].totalCents == 1050
    assert _line_of(total="10,50").lines[0].totalCents == 1050


def test_does_not_lose_a_line_over_a_floating_point_artefact() -> None:
    # What a 3.33 sometimes becomes once it has been through JSON.
    result = _line_of(total=3.3300000000000005)
    assert result.lines[0].totalCents == 333
    assert result.discarded == []


def test_rounds_to_the_cent_instead_of_throwing_the_line_away() -> None:
    assert _line_of(total=10.999).lines[0].totalCents == 1100
    assert _line_of(total="10,994").lines[0].totalCents == 1099


def test_still_drops_what_is_not_an_amount() -> None:
    assert _line_of(total="free").lines == []
    assert _line_of(total=math.nan).lines == []


def test_applies_the_same_rule_to_the_stated_total_and_subtotal() -> None:
    result = normalize_extraction({"subtotal": 16.949999999999999, "total": 19.2})
    assert result.statedSubtotalCents == 1695
    assert result.statedTotalCents == 1920
