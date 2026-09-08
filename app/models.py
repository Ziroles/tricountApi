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


class SaltRequest(BaseModel):
    """Asked before logging in: the client cannot derive anything without it."""

    email: str = Field(min_length=3, max_length=320)


class SaltResponse(BaseModel):
    kdfSalt: str


class AccountLogin(BaseModel):
    """
    `proof` is NOT the password.

    The client derives `master = PBKDF2(password, kdfSalt)` and sends only one
    branch of it. The other branch never leaves the browser and is what unlocks
    the Gemini key. The server therefore cannot decrypt what it stores, even
    while a user logs in — which is the entire property we are buying here.

    Server-side, `proof` is treated exactly as a password was: hashed with
    PBKDF2 before storage. Nothing in `auth.py` had to change.
    """

    email: str = Field(min_length=3, max_length=320)
    proof: str = Field(min_length=32, max_length=256)


class AccountCreate(AccountLogin):
    # Drawn by the client, stored as-is. Public: it only has to be unique.
    kdfSalt: str = Field(min_length=16, max_length=128)


class OwnerSettings(BaseModel):
    """
    The Gemini key travels as a blob the server cannot read.

    There is no `hint` field any more: the client decrypts, and computes its own
    preview. We have nothing to preview.
    """

    geminiKeyBlob: str | None = None
    serverHasGeminiKey: bool
    geminiModel: str


class SettingsUpdate(BaseModel):
    # None = leave untouched; "" = clear the stored blob.
    geminiKeyBlob: str | None = Field(default=None, max_length=4096)
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


class ExtractionSubmit(BaseModel):
    """
    Raw model output, produced by the browser that called Gemini with the user's
    own key. Untrusted by construction — it was already untrusted when the
    server made the call itself, so `normalize_extraction` needs no change.
    """

    rawText: str = Field(min_length=1, max_length=500_000)


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
