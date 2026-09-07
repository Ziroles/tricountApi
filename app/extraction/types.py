"""
Output contract of the extraction, consumed as-is by the PWA.

Field names are camelCase because they travel over the network to TypeScript:
the client is right about shape here, not the Python convention.
`ExtractionResult` is the exact mirror of `src/extraction/types.ts`.
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
    # None = "every tax on the receipt applies", [] = exempt, list = applicable
    # codes. The model only produces the first two cases today; the third stays
    # open for manual entry.
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
    # Lines the model produced but we refused to keep: better to name them to
    # the user than to make them vanish silently.
    discarded: list[str] = []


def empty_extraction() -> ExtractionResult:
    return ExtractionResult()
