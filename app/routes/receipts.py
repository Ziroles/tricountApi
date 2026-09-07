"""
Receipts: creation, reading, writing, photo, OCR scan, push to Tricount.

A receipt belongs to the **group**, not to the device that created it: every
member of the group sees it and can correct it. That is what makes the group
useful, and what forces the optimistic lock below.

**Concurrency.** Every write carries the `version` the client believes it is
editing. If the server has moved on in the meantime, it answers 409 with the
current receipt attached, and it is up to the client to announce that someone
else got there first. Nothing is overwritten silently: this is money, not a
draft.

**The computation stays on the client.** `settle()` is deterministic from the
document, and it feeds a live UI — the tip slider recomputes on every keystroke.
The server does not redo it; it rechecks the invariant on push, where a mistake
would become irreversible.
"""

from __future__ import annotations

import logging
import secrets

from fastapi import APIRouter, Depends, File, HTTPException, Response, UploadFile

from .. import auth, config, db, tricount_client
from ..extraction import gemini
from ..models import (
    PushExpenseRequest,
    PushExpenseResponse,
    Receipt,
    ReceiptSummary,
    ReceiptWrite,
)
from .groups import require_access
from .identity import gemini_key_for, model_for

logger = logging.getLogger("splitticket.receipts")

router = APIRouter(prefix="/v1", tags=["receipts"])

DOCUMENT_FIELDS = (
    "merchant",
    "purchaseDate",
    "lines",
    "taxes",
    "adjustments",
    "statedSubtotalCents",
    "statedTotalCents",
    "tipCents",
    "tipBasis",
    "status",
    "step",
)

ALLOWED_IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp", "image/heic", "image/heif"}


def receipt_total_cents(document: dict) -> int:
    """
    Receipt total: subtotal + taxes + adjustments.

    Mirror of `receiptTotal` (`src/lib/compute.ts`). The tip is not part of it —
    it does not appear on the printed receipt, so it is not part of the amount
    that represents it in a list.
    """
    lines = sum(int(line.get("totalCents") or 0) for line in document.get("lines") or [])
    taxes = sum(int(tax.get("amountCents") or 0) for tax in document.get("taxes") or [])
    adjustments = sum(
        int(item.get("amountCents") or 0) for item in document.get("adjustments") or []
    )
    return lines + taxes + adjustments


def _row_to_receipt(row) -> Receipt:
    document = db.loads(row["document"], {})
    return Receipt(
        id=row["id"],
        groupId=row["group_id"],
        imageId=row["image_id"],
        version=row["version"],
        createdAt=row["created_at"],
        updatedAt=row["updated_at"],
        **{field: document.get(field) for field in DOCUMENT_FIELDS if document.get(field) is not None},
    )


def _fetch(receipt_id: str, owner: auth.Owner):
    row = db.query_one("SELECT * FROM receipt WHERE id = ?", (receipt_id,))
    if row is None:
        raise HTTPException(
            status_code=404, detail={"code": "receipt_not_found", "reason": "Receipt not found."}
        )
    # Access to a receipt goes through access to the group: there is no other door.
    require_access(row["group_id"], owner)
    return row


def _write(row, document: dict, image_id: str | None = None) -> Receipt:
    stamp = auth.now()
    version = row["version"] + 1
    db.execute(
        "UPDATE receipt SET merchant = ?, purchase_date = ?, status = ?, step = ?,"
        " stated_subtotal_cents = ?, stated_total_cents = ?, tip_cents = ?, tip_basis = ?,"
        " total_cents = ?, document = ?, image_id = ?, version = ?, updated_at = ?"
        " WHERE id = ?",
        (
            document.get("merchant"),
            document.get("purchaseDate"),
            document.get("status", "draft"),
            document.get("step", "capture"),
            document.get("statedSubtotalCents"),
            document.get("statedTotalCents"),
            int(document.get("tipCents") or 0),
            document.get("tipBasis", "subtotal"),
            receipt_total_cents(document),
            db.dumps(document),
            image_id if image_id is not None else row["image_id"],
            version,
            stamp,
            row["id"],
        ),
    )
    return _row_to_receipt(db.query_one("SELECT * FROM receipt WHERE id = ?", (row["id"],)))


# ── CRUD ──────────────────────────────────────────────────────────────────────


@router.get("/groups/{group_id}/receipts", response_model=list[ReceiptSummary])
def list_receipts(
    group_id: str, owner: auth.Owner = Depends(auth.current_owner)
) -> list[ReceiptSummary]:
    require_access(group_id, owner)
    rows = db.query(
        "SELECT * FROM receipt WHERE group_id = ? ORDER BY created_at DESC", (group_id,)
    )
    return [
        ReceiptSummary(
            id=row["id"],
            groupId=row["group_id"],
            merchant=row["merchant"],
            purchaseDate=row["purchase_date"],
            status=row["status"],
            step=row["step"],
            totalCents=row["total_cents"],
            lineCount=len(db.loads(row["document"], {}).get("lines") or []),
            hasImage=bool(row["image_id"]),
            version=row["version"],
            createdAt=row["created_at"],
            updatedAt=row["updated_at"],
        )
        for row in rows
    ]


@router.post("/groups/{group_id}/receipts", response_model=Receipt, status_code=201)
def create_receipt(
    group_id: str, body: ReceiptWrite | None = None, owner: auth.Owner = Depends(auth.current_owner)
) -> Receipt:
    """Create a receipt. The body is optional: without it, we start from an empty draft."""
    require_access(group_id, owner)
    document = body.model_dump(exclude={"version"}) if body else ReceiptWrite(version=1).model_dump(
        exclude={"version"}
    )
    receipt_id = auth.new_id()
    stamp = auth.now()
    db.execute(
        "INSERT INTO receipt (id, group_id, created_by_type, created_by_id, merchant,"
        " purchase_date, status, step, stated_subtotal_cents, stated_total_cents, tip_cents,"
        " tip_basis, total_cents, image_id, document, version, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, 1, ?, ?)",
        (
            receipt_id,
            group_id,
            *owner.key,
            document.get("merchant"),
            document.get("purchaseDate"),
            document.get("status", "draft"),
            document.get("step", "capture"),
            document.get("statedSubtotalCents"),
            document.get("statedTotalCents"),
            int(document.get("tipCents") or 0),
            document.get("tipBasis", "subtotal"),
            receipt_total_cents(document),
            db.dumps(document),
            stamp,
            stamp,
        ),
    )
    return _row_to_receipt(db.query_one("SELECT * FROM receipt WHERE id = ?", (receipt_id,)))


@router.get("/receipts/{receipt_id}", response_model=Receipt)
def read_receipt(receipt_id: str, owner: auth.Owner = Depends(auth.current_owner)) -> Receipt:
    return _row_to_receipt(_fetch(receipt_id, owner))


@router.put("/receipts/{receipt_id}", response_model=Receipt)
def write_receipt(
    receipt_id: str, body: ReceiptWrite, owner: auth.Owner = Depends(auth.current_owner)
) -> Receipt:
    row = _fetch(receipt_id, owner)
    if body.version != row["version"]:
        # We return the current receipt: the client has what it needs to explain
        # and pick up again, rather than a bare refusal that would strand it.
        raise HTTPException(
            status_code=409,
            detail={
                "code": "version_conflict",
                "reason": "This receipt was modified elsewhere.",
                "current": _row_to_receipt(row).model_dump(),
            },
        )
    return _write(row, body.model_dump(exclude={"version"}))


@router.delete("/receipts/{receipt_id}", status_code=204)
def delete_receipt(receipt_id: str, owner: auth.Owner = Depends(auth.current_owner)) -> None:
    row = _fetch(receipt_id, owner)
    image = db.query_one("SELECT path FROM image WHERE receipt_id = ?", (receipt_id,))
    db.execute("DELETE FROM receipt WHERE id = ?", (receipt_id,))
    db.execute("DELETE FROM image WHERE receipt_id = ?", (receipt_id,))
    if image:
        # The photo follows the receipt: leaving it lying on disk would be a
        # silent leak of what the user believes they deleted.
        (config.IMAGES_DIR / image["path"]).unlink(missing_ok=True)
    logger.info("receipt %s deleted from group %s", receipt_id, row["group_id"])


# ── Photo ─────────────────────────────────────────────────────────────────────


@router.post("/receipts/{receipt_id}/image", response_model=Receipt)
async def upload_image(
    receipt_id: str,
    file: UploadFile = File(...),
    owner: auth.Owner = Depends(auth.current_owner),
) -> Receipt:
    row = _fetch(receipt_id, owner)

    content = await file.read()
    if len(content) == 0:
        raise HTTPException(
            status_code=400, detail={"code": "empty_image", "reason": "Empty image."}
        )
    if len(content) > config.MAX_IMAGE_BYTES:
        raise HTTPException(
            status_code=413,
            detail={"code": "image_too_large", "reason": "Photo too large."},
        )
    mime = (file.content_type or "image/jpeg").split(";")[0].strip().lower()
    if mime not in ALLOWED_IMAGE_TYPES:
        raise HTTPException(
            status_code=415,
            detail={"code": "unsupported_image", "reason": f"Unsupported format: {mime}."},
        )

    config.ensure_directories()
    image_id = secrets.token_urlsafe(16)
    path = f"{image_id}.bin"
    (config.IMAGES_DIR / path).write_bytes(content)

    previous = db.query_one("SELECT id, path FROM image WHERE receipt_id = ?", (receipt_id,))
    if previous:
        db.execute("DELETE FROM image WHERE id = ?", (previous["id"],))
        (config.IMAGES_DIR / previous["path"]).unlink(missing_ok=True)

    db.execute(
        "INSERT INTO image (id, group_id, receipt_id, path, mime, bytes, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (image_id, row["group_id"], receipt_id, path, mime, len(content), auth.now()),
    )
    return _write(row, db.loads(row["document"], {}), image_id=image_id)


@router.get("/receipts/{receipt_id}/image")
def read_image(receipt_id: str, owner: auth.Owner = Depends(auth.current_owner)) -> Response:
    _fetch(receipt_id, owner)
    record = db.query_one("SELECT path, mime FROM image WHERE receipt_id = ?", (receipt_id,))
    if record is None:
        raise HTTPException(
            status_code=404, detail={"code": "image_not_found", "reason": "No photo."}
        )
    blob = config.IMAGES_DIR / record["path"]
    if not blob.exists():
        raise HTTPException(
            status_code=404, detail={"code": "image_not_found", "reason": "Photo not found."}
        )
    return Response(
        content=blob.read_bytes(),
        media_type=record["mime"],
        headers={"cache-control": "private, max-age=86400"},
    )


# ── Reading the receipt ───────────────────────────────────────────────────────


@router.post("/receipts/{receipt_id}/scan", response_model=Receipt)
def scan_receipt(receipt_id: str, owner: auth.Owner = Depends(auth.current_owner)) -> Receipt:
    """
    Read the photo already uploaded and **write the result onto the receipt**.

    Persisting rather than merely answering changes something concrete: if the
    connection drops during the fifteen seconds the model takes, reopening the
    receipt shows the reading. The work is not lost with the request.
    """
    row = _fetch(receipt_id, owner)
    record = db.query_one("SELECT path, mime FROM image WHERE receipt_id = ?", (receipt_id,))
    if record is None:
        raise HTTPException(
            status_code=400,
            detail={"code": "image_missing", "reason": "No photo to read on this receipt."},
        )
    blob = config.IMAGES_DIR / record["path"]
    if not blob.exists():
        raise HTTPException(
            status_code=404,
            detail={"code": "image_not_found", "reason": "The photo is no longer available."},
        )

    try:
        result = gemini.extract(
            blob.read_bytes(), record["mime"], gemini_key_for(owner), model_for(owner)
        )
    except gemini.ExtractionError as error:
        raise HTTPException(
            status_code=400 if error.code == "no_gemini_key" else 502,
            detail={"code": error.code, "reason": error.reason, "retryable": error.retryable},
        ) from error

    document = db.loads(row["document"], {})
    document["lines"] = [
        {
            "id": auth.new_id(),
            "label": line.label,
            "description": line.description,
            "quantity": line.quantity,
            "unitPriceCents": line.unitPriceCents,
            "totalCents": line.totalCents,
            "taxCodes": line.taxCodes,
            "assignments": [],
            "confidence": line.confidence,
            "isManual": False,
        }
        for line in result.lines
    ]
    document["taxes"] = [
        {
            "id": auth.new_id(),
            "code": tax.code,
            "label": tax.label,
            "ratePercent": tax.ratePercent,
            "amountCents": tax.amountCents,
        }
        for tax in result.taxes
    ]
    # What the user already entered wins over what the model proposes.
    document["merchant"] = document.get("merchant") or result.merchant
    document["purchaseDate"] = document.get("purchaseDate") or result.purchaseDate
    document["statedSubtotalCents"] = result.statedSubtotalCents
    document["statedTotalCents"] = result.statedTotalCents
    document["step"] = "verify"

    return _write(row, document)


# ── Pushing to Tricount ───────────────────────────────────────────────────────


@router.post("/receipts/{receipt_id}/push", response_model=PushExpenseResponse)
def push_receipt(
    receipt_id: str,
    body: PushExpenseRequest,
    owner: auth.Owner = Depends(auth.current_owner),
) -> PushExpenseResponse:
    """Create the expense in the group's tricount, shares assigned by member uuid."""
    row = _fetch(receipt_id, owner)
    try:
        transaction_id = tricount_client.create_expense(
            code=row["group_id"],
            description=body.description,
            total_cents=body.totalCents,
            payer_uuid=body.payerMemberUuid,
            shares=[(share.memberUuid, share.amountCents) for share in body.shares],
            date=body.date,
        )
    except tricount_client.TricountError as error:
        raise HTTPException(
            status_code=error.status, detail={"code": error.code, "reason": error.reason}
        ) from error
    return PushExpenseResponse(transactionId=transaction_id)
