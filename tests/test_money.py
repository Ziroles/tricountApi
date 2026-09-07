"""
Port of `src/lib/money.test.ts`.

The cases are taken over one for one, values included: if the server and the
client diverge by a cent on the same amount, the "subtotal + taxes = total"
check starts lying, and that is exactly what these tests forbid.
"""

from __future__ import annotations

import math

import pytest

from app.extraction.money import js_round, parse_amount_to_cents, round_half_up


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("12,34", 1234),
        ("12.34", 1234),
        ("1 234,56", 123456),
        ("12,34 $", 1234),
        ("12.34 €", 1234),
        ("12", 1200),
        ("12,5", 1250),
        ("0,05", 5),
        ("-3,20", -320),
        (",99", 99),
        ("+4,00", 400),
    ],
)
def test_reads_amounts(raw: str, expected: int) -> None:
    assert parse_amount_to_cents(raw) == expected


@pytest.mark.parametrize("raw", ["", "-", "abc", "12,345", "1.2.3", "12$34", "12€34"])
def test_rejects_what_is_not_an_amount(raw: str) -> None:
    assert parse_amount_to_cents(raw) is None


def test_never_goes_through_a_float() -> None:
    assert parse_amount_to_cents("0,07") == 7
    assert parse_amount_to_cents("1,10") == 110
    assert parse_amount_to_cents("29,29") == 2929


def test_non_breaking_spaces_from_the_receipt() -> None:
    # Non-breaking and narrow no-break space: both come out of copy-pastes.
    assert parse_amount_to_cents("1 234,56") == 123456
    assert parse_amount_to_cents("1 234,56") == 123456


def test_round_half_up_rounds_symmetrically() -> None:
    assert round_half_up(0.5) == 1
    assert round_half_up(-0.5) == -1
    assert round_half_up(2.4) == 2


def test_the_two_roundings_differ_on_negatives() -> None:
    """`Math.round` goes towards +∞, `roundHalfUp` moves away from zero. This is not a
    detail: negative lines are the discounts printed on the receipt."""
    assert js_round(-175.5) == -175
    assert round_half_up(-175.5) == -176


def test_refuses_what_is_not_a_string() -> None:
    assert parse_amount_to_cents(12.34) is None
    assert parse_amount_to_cents(None) is None
    assert parse_amount_to_cents(math.nan) is None
