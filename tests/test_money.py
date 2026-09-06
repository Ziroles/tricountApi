"""
Portage de `src/lib/money.test.ts`.

Les cas sont repris un pour un, valeurs comprises : si le serveur et le client
divergent d'un centime sur un même montant, le contrôle « sous-total + taxes =
total » se met à mentir, et c'est exactement ce que ces tests interdisent.
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
        ("1 234,56", 123456),
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
def test_lit_les_montants(raw: str, expected: int) -> None:
    assert parse_amount_to_cents(raw) == expected


@pytest.mark.parametrize("raw", ["", "-", "abc", "12,345", "1.2.3", "12$34", "12€34"])
def test_rejette_ce_qui_n_est_pas_un_montant(raw: str) -> None:
    assert parse_amount_to_cents(raw) is None


def test_ne_passe_jamais_par_un_flottant() -> None:
    assert parse_amount_to_cents("0,07") == 7
    assert parse_amount_to_cents("1,10") == 110
    assert parse_amount_to_cents("29,29") == 2929


def test_espaces_insecables_du_ticket() -> None:
    # Espace insécable et insécable fin : les deux sortent des copiés-collés.
    assert parse_amount_to_cents("1 234,56") == 123456
    assert parse_amount_to_cents("1 234,56") == 123456


def test_round_half_up_arrondit_symetriquement() -> None:
    assert round_half_up(0.5) == 1
    assert round_half_up(-0.5) == -1
    assert round_half_up(2.4) == 2


def test_les_deux_arrondis_different_sur_les_negatifs() -> None:
    """`Math.round` va vers +∞, `roundHalfUp` s'éloigne de zéro. Ce n'est pas un détail :
    les lignes négatives sont les remises imprimées sur le ticket."""
    assert js_round(-175.5) == -175
    assert round_half_up(-175.5) == -176


def test_refuse_ce_qui_n_est_pas_une_chaine() -> None:
    assert parse_amount_to_cents(12.34) is None
    assert parse_amount_to_cents(None) is None
    assert parse_amount_to_cents(math.nan) is None
