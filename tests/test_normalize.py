"""
Portage de `src/extraction/normalize.test.ts`, cas pour cas.

Ces tests sont le filet du portage : ils décrivent ce qu'une sortie de modèle a
le droit de devenir. Tant qu'ils passent à l'identique en Python, l'OCR peut
changer de camp sans que la répartition change de résultat.
"""

from __future__ import annotations

import math

import pytest

from app.extraction.normalize import (
    canonical_tax_code,
    normalize_extraction,
    parse_rate_percent,
)

TICKET = {
    "merchant": "IGA EXTRA",
    "purchaseDate": "2026-03-14",
    "subtotal": "16,95",
    "total": "19,20",
    "taxes": [
        {"label": "TPS", "rate": "5", "amount": "0,75"},
        {"label": "TVQ", "rate": "9,975", "amount": "1,50"},
    ],
    "lines": [
        {"label": "PAIN TRANCHE", "quantity": 1, "unitPrice": "3,49", "total": "3,49", "taxable": False},
        {"label": "YOGOURT NATURE", "quantity": 2, "unitPrice": "1,50", "total": "3,00", "taxable": False},
        {"label": "SAVON A VAISSELLE", "quantity": 1, "unitPrice": "10,46", "total": "10,46", "taxable": True},
    ],
}


# ── cas nominal ───────────────────────────────────────────────────────────────


def test_convertit_les_montants_en_centimes_entiers() -> None:
    result = normalize_extraction(TICKET)
    assert [line.totalCents for line in result.lines] == [349, 300, 1046]
    assert result.statedSubtotalCents == 1695
    assert result.statedTotalCents == 1920


def test_lit_les_taxes_du_pied_de_ticket() -> None:
    result = normalize_extraction(TICKET)
    assert [(t.code, t.label, t.ratePercent, t.amountCents) for t in result.taxes] == [
        ("TPS", "TPS", 5, 75),
        ("TVQ", "TVQ", 9.975, 150),
    ]


def test_marque_les_lignes_detaxees_et_seulement_celles_la() -> None:
    pain, yogourt, savon = normalize_extraction(TICKET).lines
    assert pain.taxCodes == []
    assert yogourt.taxCodes == []
    assert savon.taxCodes is None


def test_conserve_quantite_et_prix_unitaire() -> None:
    _, yogourt, _ = normalize_extraction(TICKET).lines
    assert (yogourt.quantity, yogourt.unitPriceCents) == (2, 150)


def test_lit_le_commercant_et_la_date() -> None:
    result = normalize_extraction(TICKET)
    assert result.merchant == "IGA EXTRA"
    assert result.purchaseDate == "2026-03-14"


def test_lit_la_description_decodee_si_fournie() -> None:
    result = normalize_extraction(
        {
            "lines": [
                {"label": "CR GCE VAN", "description": "Crème glacée vanille", "total": "4,99"},
                {"label": "POM MCINT", "total": "2,99"},
            ]
        }
    )
    assert result.lines[0].description == "Crème glacée vanille"
    assert result.lines[1].description is None


def test_sous_total_plus_taxes_retombe_sur_le_total_imprime() -> None:
    result = normalize_extraction(TICKET)
    taxes = sum(tax.amountCents for tax in result.taxes)
    assert (result.statedSubtotalCents or 0) + taxes == result.statedTotalCents


# ── sorties fautives du modèle ────────────────────────────────────────────────


def test_ecarte_une_ligne_sans_montant_lisible_plutot_que_d_y_mettre_zero() -> None:
    result = normalize_extraction(
        {
            "lines": [
                {"label": "PAIN", "total": "1,05"},
                {"label": "ILLISIBLE", "total": "environ 3 euros"},
                {"label": "RIEN"},
            ]
        }
    )
    assert len(result.lines) == 1
    assert result.discarded == ["ILLISIBLE", "RIEN"]


def test_accepte_un_nombre_la_ou_une_chaine_etait_demandee() -> None:
    result = normalize_extraction({"lines": [{"label": "PAIN", "total": 1.05}]})
    assert result.lines[0].totalCents == 105


def test_recalcule_le_prix_unitaire_quand_il_ne_tombe_pas_sur_le_total() -> None:
    result = normalize_extraction(
        {"lines": [{"label": "POMMES", "quantity": 3, "unitPrice": "0,99", "total": "2,98"}]}
    )
    assert (result.lines[0].totalCents, result.lines[0].unitPriceCents) == (298, 99)


def test_garde_les_remises_imprimees_en_negatif() -> None:
    result = normalize_extraction({"lines": [{"label": "REMISE FIDELITE", "total": "-2,50"}]})
    assert result.lines[0].totalCents == -250


def test_ramene_une_quantite_aberrante_a_un() -> None:
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


def test_marque_les_lignes_que_le_modele_dit_incertaines() -> None:
    result = normalize_extraction(
        {
            "lines": [
                {"label": "SUR", "total": "1,00"},
                {"label": "DOUTEUX", "total": "2,00", "uncertain": True},
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
def test_refuse_une_date_inventee_ou_mal_formee(raw: str, expected: str | None) -> None:
    assert normalize_extraction({"purchaseDate": raw}).purchaseDate == expected


@pytest.mark.parametrize("value", [None, 42, "texte", [], {}])
def test_survit_a_une_reponse_vide_nulle_ou_d_un_autre_type(value: object) -> None:
    result = normalize_extraction(value)
    assert result.lines == []
    assert result.statedTotalCents is None


def test_ne_perd_jamais_un_montant_dans_un_flottant() -> None:
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


def test_ramene_les_libelles_anglais_et_francais_au_meme_code() -> None:
    assert canonical_tax_code("TPS") == "TPS"
    assert canonical_tax_code("GST") == "TPS"
    assert canonical_tax_code("TVQ 9,975%") == "TVQ"
    assert canonical_tax_code("QST") == "TVQ"
    assert canonical_tax_code("HST") == "TVH"
    assert canonical_tax_code("PST") == "TVP"


def test_additionne_une_taxe_imprimee_deux_fois() -> None:
    result = normalize_extraction(
        {"taxes": [{"label": "TPS", "amount": "1,00"}, {"label": "GST", "amount": "0,50"}], "lines": []}
    )
    assert len(result.taxes) == 1
    assert result.taxes[0].amountCents == 150


def test_ecarte_une_taxe_sans_montant_lisible() -> None:
    result = normalize_extraction(
        {
            "taxes": [{"label": "TPS"}, {"label": "TVQ", "amount": "illisible"}, {"amount": "1,00"}],
            "lines": [],
        }
    )
    assert result.taxes == []


def test_accepte_une_taxe_a_zero() -> None:
    """Une taxe à zéro est une information — un régime détaxé — pas une absence."""
    result = normalize_extraction({"taxes": [{"label": "TPS", "amount": "0,00"}], "lines": []})
    assert result.taxes[0].amountCents == 0


def test_survit_a_des_taxes_qui_ne_sont_pas_un_tableau() -> None:
    assert normalize_extraction({"taxes": "TPS 1,00", "lines": []}).taxes == []


# ── taux ──────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("raw", "expected"), [("5", 5), ("9,975", 9.975), ("13 %", 13), ("14.975", 14.975)]
)
def test_lit_un_taux(raw: str, expected: float) -> None:
    assert parse_rate_percent(raw) == expected


@pytest.mark.parametrize("raw", ["", "0", "-5", "95", "A", None, {}])
def test_ecarte_un_taux_implausible(raw: object) -> None:
    assert parse_rate_percent(raw) is None


# ── montants au centime ───────────────────────────────────────────────────────


def _line_of(**raw: object) -> object:
    return normalize_extraction({"lines": [{"label": "Article", "quantity": 1, **raw}]})


def test_accepte_les_nombres_json_autant_que_les_chaines() -> None:
    assert _line_of(total=10.5).lines[0].totalCents == 1050
    assert _line_of(total="10,50").lines[0].totalCents == 1050


def test_ne_perd_pas_une_ligne_sur_un_artefact_de_virgule_flottante() -> None:
    # Ce qu'un 3,33 devient parfois une fois passé par du JSON.
    result = _line_of(total=3.3300000000000005)
    assert result.lines[0].totalCents == 333
    assert result.discarded == []


def test_arrondit_au_centime_au_lieu_de_jeter_la_ligne() -> None:
    assert _line_of(total=10.999).lines[0].totalCents == 1100
    assert _line_of(total="10,994").lines[0].totalCents == 1099


def test_ecarte_toujours_ce_qui_n_est_pas_un_montant() -> None:
    assert _line_of(total="gratuit").lines == []
    assert _line_of(total=math.nan).lines == []


def test_applique_la_meme_regle_au_total_et_au_sous_total_lus() -> None:
    result = normalize_extraction({"subtotal": 16.949999999999999, "total": 19.2})
    assert result.statedSubtotalCents == 1695
    assert result.statedTotalCents == 1920
