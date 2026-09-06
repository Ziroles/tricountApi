"""
Contrat HTTP : ce que l'API reçoit et ce qu'elle rend.

Les champs sont en camelCase : ils traversent le réseau vers du TypeScript, et
c'est le client qui a raison sur la forme. Les types miroitent `src/types.ts`, à
un détail près, mais un détail qui change tout :

    Assignment.personId  →  Assignment.memberUuid

Une part n'est plus attribuée à une « personne » locale saisie à la main, mais à
un **membre du tricount**, identifié par son uuid. L'appariement par nom, et sa
fragilité au moindre accent, disparaît avec ce champ.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

TipBasis = Literal["subtotal", "total"]
ReceiptStatus = Literal["draft", "settled"]
ReceiptStep = Literal["capture", "processing", "verify", "assign", "results"]
AdjustmentMode = Literal["proportional", "assigned"]


# ── Identité ──────────────────────────────────────────────────────────────────


class DeviceCreated(BaseModel):
    deviceId: str
    token: str


class AccountCredentials(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=8, max_length=256)


class OwnerSettings(BaseModel):
    """La clef Gemini n'apparaît jamais ici : seulement de quoi la reconnaître."""

    hasGeminiKey: bool
    geminiKeyHint: str | None = None
    serverHasGeminiKey: bool
    geminiModel: str


class SettingsUpdate(BaseModel):
    # None = ne pas toucher ; "" = effacer la clef enregistrée.
    geminiApiKey: str | None = None
    geminiModel: str | None = None


class Me(BaseModel):
    deviceId: str
    accountEmail: str | None
    settings: OwnerSettings


# ── Groupes ───────────────────────────────────────────────────────────────────


class Member(BaseModel):
    uuid: str
    displayName: str
    status: str


class JoinGroupRequest(BaseModel):
    shareUrl: str = Field(min_length=1, max_length=2048)


class Group(BaseModel):
    id: str
    title: str
    currency: str
    members: list[Member]
    membersSyncedAt: str | None
    receiptCount: int = 0
    lastActivityAt: str | None = None


class GroupSummary(BaseModel):
    id: str
    title: str
    currency: str
    memberCount: int
    receiptCount: int
    lastActivityAt: str | None


# ── Tickets ───────────────────────────────────────────────────────────────────


class Assignment(BaseModel):
    memberUuid: str
    shares: int = Field(ge=0)


class ReceiptLine(BaseModel):
    id: str
    label: str = ""
    description: str | None = None
    quantity: int = Field(default=1, ge=1, le=999)
    unitPriceCents: int = 0
    totalCents: int = 0
    # None = toutes les taxes du ticket s'appliquent ; [] = exonérée.
    taxCodes: list[str] | None = None
    assignments: list[Assignment] = []
    confidence: int = 100
    isManual: bool = False


class ReceiptTax(BaseModel):
    id: str
    label: str
    code: str
    ratePercent: float | None = None
    amountCents: int = 0


class Adjustment(BaseModel):
    id: str
    label: str
    amountCents: int
    mode: AdjustmentMode = "proportional"
    assignments: list[Assignment] = []


class ReceiptDocument(BaseModel):
    """Le ticket tel que la PWA l'édite : un document, envoyé et reçu d'un bloc."""

    merchant: str | None = None
    purchaseDate: str | None = None
    lines: list[ReceiptLine] = []
    taxes: list[ReceiptTax] = []
    adjustments: list[Adjustment] = []
    statedSubtotalCents: int | None = None
    statedTotalCents: int | None = None
    tipCents: int = 0
    tipBasis: TipBasis = "subtotal"
    status: ReceiptStatus = "draft"
    step: ReceiptStep = "capture"


class Receipt(ReceiptDocument):
    id: str
    groupId: str
    imageId: str | None = None
    version: int
    createdAt: str
    updatedAt: str


class ReceiptSummary(BaseModel):
    id: str
    groupId: str
    merchant: str | None
    purchaseDate: str | None
    status: ReceiptStatus
    step: ReceiptStep
    totalCents: int
    lineCount: int
    hasImage: bool
    version: int
    createdAt: str
    updatedAt: str


class ReceiptWrite(ReceiptDocument):
    """Écriture d'un ticket : le document, plus la version qu'on croit modifier."""

    version: int = Field(ge=1)


# ── Envoi vers Tricount ───────────────────────────────────────────────────────


class ShareInput(BaseModel):
    memberUuid: str
    amountCents: int


class PushExpenseRequest(BaseModel):
    description: str = ""
    totalCents: int
    payerMemberUuid: str
    shares: list[ShareInput]
    date: str | None = None


class PushExpenseResponse(BaseModel):
    transactionId: str
