"""
Contrat de sortie de l'extraction, consommé tel quel par la PWA.

Les noms de champs sont en camelCase parce qu'ils traversent le réseau vers du
TypeScript : c'est le client qui a raison sur la forme, pas la convention
Python. `ExtractionResult` est le miroir exact de `src/extraction/types.ts`.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class ExtractedLine(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    label: str
    description: str | None = None
    quantity: int
    unitPriceCents: int
    totalCents: int
    # None = « toutes les taxes du ticket s'appliquent », [] = exonérée,
    # liste = codes applicables. Le modèle ne produit aujourd'hui que les deux
    # premiers cas ; le troisième reste ouvert pour la saisie manuelle.
    taxCodes: list[str] | None
    confidence: int


class ExtractedTax(BaseModel):
    code: str
    label: str
    ratePercent: float | None
    amountCents: int


class ExtractionResult(BaseModel):
    lines: list[ExtractedLine] = []
    taxes: list[ExtractedTax] = []
    statedSubtotalCents: int | None = None
    statedTotalCents: int | None = None
    merchant: str | None = None
    purchaseDate: str | None = None
    # Lignes que le modèle a produites mais qu'on a refusé de retenir : mieux
    # vaut les nommer à l'utilisateur que de les faire disparaître en silence.
    discarded: list[str] = []


def empty_extraction() -> ExtractionResult:
    return ExtractionResult()
