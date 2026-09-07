"""
HTTP contract: what the API accepts and what it returns.

Field names are camelCase: they travel over the network to TypeScript, and the
client is the one that is right about shape. The types mirror `src/types.ts`,
with one detail apart — but a detail that changes everything:

    Assignment.personId  →  Assignment.memberUuid

A share is no longer assigned to a local "person" typed in by hand, but to a
**tricount member**, identified by their uuid. Name matching, and its fragility
to the slightest accent, disappears with this field.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

TipBasis = Literal["subtotal", "total"]
ReceiptStatus = Literal["draft", "settled"]
ReceiptStep = Literal["capture", "processing", "verify", "assign", "results"]
AdjustmentMode = Literal["proportional", "assigned"]


# ── Identity ──────────────────────────────────────────────────────────────────


class DeviceCreated(BaseModel):
    deviceId: str
    token: str


class AccountCredentials(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=8, max_length=256)


class OwnerSettings(BaseModel):
    """The Gemini key never appears here: only enough to recognise it."""

    hasGeminiKey: bool
    geminiKeyHint: str | None = None
    serverHasGeminiKey: bool
    geminiModel: str


class SettingsUpdate(BaseModel):
    # None = leave untouched; "" = clear the saved key.
    geminiApiKey: str | None = None
    geminiModel: str | None = None


class Me(BaseModel):
    deviceId: str
    accountEmail: str | None
    settings: OwnerSettings


# ── Groups ────────────────────────────────────────────────────────────────────


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


# ── Receipts ──────────────────────────────────────────────────────────────────


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
    # None = every tax on the receipt applies; [] = exempt.
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
    """The receipt as the PWA edits it: one document, sent and received whole."""

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
    """Writing a receipt: the document, plus the version we believe we are editing."""

    version: int = Field(ge=1)


# ── Pushing to Tricount ───────────────────────────────────────────────────────


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
