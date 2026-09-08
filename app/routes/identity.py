"""
Device enrolment, optional accounts, owner settings.

The happy path asks the user for nothing: the PWA calls `POST /v1/devices` on
its first launch, keeps the token, and that is all. An account only comes into
play if you want to find your groups again elsewhere.

Two things this module deliberately does **not** know:

 1. **The user's password.** `POST /v1/sessions` receives a proof derived from
    it, not the password. The other branch of that derivation stays in the
    browser and unlocks the Gemini key.
 2. **The user's Gemini key.** It arrives encrypted, leaves encrypted, and the
    means to read it never exists on this side.
"""

from __future__ import annotations

import hashlib
import hmac

from fastapi import APIRouter, Depends, Header, HTTPException

from .. import auth, config, db
from ..extraction import gemini
from ..models import (
    AccountCreate,
    AccountLogin,
    DeviceCreated,
    Me,
    OwnerSettings,
    SaltRequest,
    SaltResponse,
    SettingsUpdate,
)

router = APIRouter(prefix="/v1", tags=["identity"])


# ── Settings ──────────────────────────────────────────────────────────────────


def settings_of(owner: auth.Owner) -> OwnerSettings:
    row = db.query_one(
        "SELECT gemini_key_blob, gemini_model FROM owner_settings"
        " WHERE owner_type = ? AND owner_id = ?",
        owner.key,
    )
    return OwnerSettings(
        geminiKeyBlob=row["gemini_key_blob"] if row else None,
        serverHasGeminiKey=config.GEMINI_API_KEY != "",
        geminiModel=(row["gemini_model"] if row and row["gemini_model"] else config.DEFAULT_GEMINI_MODEL),
    )


def model_for(owner: auth.Owner) -> str:
    row = db.query_one(
        "SELECT gemini_model FROM owner_settings WHERE owner_type = ? AND owner_id = ?", owner.key
    )
    return (row["gemini_model"] if row and row["gemini_model"] else None) or config.DEFAULT_GEMINI_MODEL


# ── Enrolment ─────────────────────────────────────────────────────────────────


@router.post("/devices", response_model=DeviceCreated, status_code=201)
def create_device(x_signup_key: str | None = Header(default=None)) -> DeviceCreated:
    """Enrol a device. The token is returned here only, and never read back."""
    auth.check_signup_key(x_signup_key)
    device_id, token = auth.enrol_device()
    return DeviceCreated(deviceId=device_id, token=token)


@router.get("/me", response_model=Me)
def read_me(owner: auth.Owner = Depends(auth.current_owner)) -> Me:
    email = None
    if owner.account_id:
        row = db.query_one("SELECT email FROM account WHERE id = ?", (owner.account_id,))
        email = row["email"] if row else None
    return Me(deviceId=owner.device_id, accountEmail=email, settings=settings_of(owner))


@router.put("/me/settings", response_model=OwnerSettings)
def update_settings(
    body: SettingsUpdate, owner: auth.Owner = Depends(auth.current_owner)
) -> OwnerSettings:
    """
    Update the settings. A missing `geminiKeyBlob` leaves the blob in place; an
    empty string clears it. Telling the two apart keeps a client that only sends
    the model from wiping the key along the way.

    The blob is opaque here. We do not parse it, we do not validate its shape
    beyond a size cap, and we could not tell a real one from noise — storing
    something we cannot read is the feature.
    """
    if body.geminiKeyBlob not in (None, "") and owner.type != "account":
        # No account means no password, and no password means nothing to derive
        # a key from. Rather than store a blob whose owner could never unlock it
        # on another device, we say so.
        raise HTTPException(
            status_code=400,
            detail={
                "code": "account_required",
                "reason": "Create an account to store your key and find it again elsewhere.",
            },
        )

    db.execute(
        "INSERT OR IGNORE INTO owner_settings (owner_type, owner_id, updated_at) VALUES (?, ?, ?)",
        (*owner.key, auth.now()),
    )

    if body.geminiKeyBlob is not None:
        db.execute(
            "UPDATE owner_settings SET gemini_key_blob = ?, updated_at = ?"
            " WHERE owner_type = ? AND owner_id = ?",
            (body.geminiKeyBlob or None, auth.now(), *owner.key),
        )

    if body.geminiModel is not None:
        db.execute(
            "UPDATE owner_settings SET gemini_model = ?, updated_at = ?"
            " WHERE owner_type = ? AND owner_id = ?",
            (body.geminiModel.strip() or None, auth.now(), *owner.key),
        )

    return settings_of(owner)


@router.get("/models")
def available_models(owner: auth.Owner = Depends(auth.current_owner)) -> list[dict[str, str]]:
    """
    Models readable with the **instance** key.

    A user with their own key does not come through here: their browser asks
    Google directly, with a key this server never sees.
    """
    try:
        return gemini.list_models(config.GEMINI_API_KEY)
    except gemini.ExtractionError as error:
        raise HTTPException(
            status_code=400 if error.code == "no_gemini_key" else 502,
            detail={"code": error.code, "reason": error.reason},
        ) from error


# ── Optional accounts ─────────────────────────────────────────────────────────


SALT_PEPPER_KEY = "kdf_salt_pepper"


def _decoy_salt(email: str) -> str:
    """
    A stable, plausible salt for an e-mail with no account.

    Without it, the salt lookup that has to precede any login would answer
    "this address is registered here" to anyone who asks. The decoy is
    deterministic — asking twice gives the same answer, as a real one would —
    and derived from a per-instance value nobody has to configure.
    """
    pepper = db.instance_value(SALT_PEPPER_KEY)
    return hmac.new(
        bytes.fromhex(pepper), email.strip().lower().encode("utf-8"), hashlib.sha256
    ).hexdigest()[:32]


def _attach(owner: auth.Owner, account_id: str) -> None:
    """
    Attach the device to the account and carry its group access over.

    Without this transfer, someone who creates an account after joining groups
    would see them disappear: they would stay filed under the device, while
    reads would now happen under the account.
    """
    with db.transaction() as connection:
        connection.execute("UPDATE device SET account_id = ? WHERE id = ?", (account_id, owner.device_id))
        connection.execute(
            "UPDATE OR IGNORE group_access SET owner_type = 'account', owner_id = ?"
            " WHERE owner_type = 'device' AND owner_id = ?",
            (account_id, owner.device_id),
        )
        connection.execute(
            "DELETE FROM group_access WHERE owner_type = 'device' AND owner_id = ?",
            (owner.device_id,),
        )


@router.post("/accounts/salt", response_model=SaltResponse)
def read_salt(body: SaltRequest, owner: auth.Owner = Depends(auth.current_owner)) -> SaltResponse:
    """
    The salt for an address, so the client can derive its proof before logging in.

    POST rather than GET on purpose: an e-mail in a query string ends up in
    access logs and browser history. An unknown address gets a decoy rather than
    a 404 — see `_decoy_salt`.
    """
    email = body.email.strip().lower()
    row = db.query_one("SELECT kdf_salt FROM account WHERE email = ?", (email,))
    if row is not None and row["kdf_salt"]:
        return SaltResponse(kdfSalt=row["kdf_salt"])
    return SaltResponse(kdfSalt=_decoy_salt(email))


@router.post("/accounts", response_model=Me, status_code=201)
def create_account(body: AccountCreate, owner: auth.Owner = Depends(auth.current_owner)) -> Me:
    email = body.email.strip().lower()
    if db.query_one("SELECT id FROM account WHERE email = ?", (email,)) is not None:
        raise HTTPException(
            status_code=409,
            detail={"code": "email_taken", "reason": "This address is already in use."},
        )
    account_id = auth.new_id()
    db.execute(
        "INSERT INTO account (id, email, password_hash, kdf_salt, created_at)"
        " VALUES (?, ?, ?, ?, ?)",
        (account_id, email, auth.hash_password(body.proof), body.kdfSalt, auth.now()),
    )
    _attach(owner, account_id)
    return read_me(auth.owner_of(owner.device_id, account_id))


@router.post("/sessions", response_model=Me)
def open_session(body: AccountLogin, owner: auth.Owner = Depends(auth.current_owner)) -> Me:
    """Attach this device to an existing account, to find its groups there."""
    row = db.query_one(
        "SELECT id, password_hash FROM account WHERE email = ?", (body.email.strip().lower(),)
    )
    if row is None or not auth.verify_password(body.proof, row["password_hash"]):
        raise HTTPException(
            status_code=401,
            detail={"code": "bad_credentials", "reason": "Incorrect address or password."},
        )
    _attach(owner, row["id"])
    return read_me(auth.owner_of(owner.device_id, row["id"]))
