"""
Enrôlement d'appareil, comptes optionnels, réglages du propriétaire.

Le parcours nominal ne demande rien à l'utilisateur : la PWA appelle
`POST /v1/devices` à son premier lancement, garde le jeton, et c'est tout. Le
compte n'entre en jeu que si l'on veut retrouver ses groupes ailleurs.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Header, HTTPException

from .. import auth, config, crypto, db
from ..extraction import gemini
from ..models import (
    AccountCredentials,
    DeviceCreated,
    Me,
    OwnerSettings,
    SettingsUpdate,
)

router = APIRouter(prefix="/v1", tags=["identité"])


def settings_of(owner: auth.Owner) -> OwnerSettings:
    row = db.query_one(
        "SELECT gemini_api_key_encrypted, gemini_key_hint, gemini_model"
        " FROM owner_settings WHERE owner_type = ? AND owner_id = ?",
        owner.key,
    )
    return OwnerSettings(
        hasGeminiKey=bool(row and row["gemini_api_key_encrypted"]),
        geminiKeyHint=row["gemini_key_hint"] if row else None,
        serverHasGeminiKey=config.GEMINI_API_KEY != "",
        geminiModel=(row["gemini_model"] if row and row["gemini_model"] else config.DEFAULT_GEMINI_MODEL),
    )


def gemini_key_for(owner: auth.Owner) -> str:
    """
    Clef effective : celle de l'utilisateur d'abord, celle de l'instance ensuite.

    L'ordre compte. Quelqu'un qui a pris la peine d'enregistrer sa clef veut que
    ses lectures soient débitées chez lui, pas sur le quota partagé.
    """
    row = db.query_one(
        "SELECT gemini_api_key_encrypted FROM owner_settings WHERE owner_type = ? AND owner_id = ?",
        owner.key,
    )
    personal = crypto.decrypt(row["gemini_api_key_encrypted"]) if row else None
    return personal or config.GEMINI_API_KEY


def model_for(owner: auth.Owner) -> str:
    row = db.query_one(
        "SELECT gemini_model FROM owner_settings WHERE owner_type = ? AND owner_id = ?", owner.key
    )
    return (row["gemini_model"] if row and row["gemini_model"] else None) or config.DEFAULT_GEMINI_MODEL


@router.post("/devices", response_model=DeviceCreated, status_code=201)
def create_device(x_signup_key: str | None = Header(default=None)) -> DeviceCreated:
    """Enrôle un appareil. Le jeton n'est renvoyé qu'ici, et jamais relu."""
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
    Met à jour les réglages. `geminiApiKey` absent laisse la clef en place ;
    une chaîne vide l'efface. Distinguer les deux évite qu'un client qui ne
    renvoie que le modèle ne fasse disparaître la clef au passage.
    """
    db.execute(
        "INSERT OR IGNORE INTO owner_settings (owner_type, owner_id, updated_at) VALUES (?, ?, ?)",
        (*owner.key, auth.now()),
    )

    if body.geminiApiKey is not None:
        key = body.geminiApiKey.strip()
        if key == "":
            db.execute(
                "UPDATE owner_settings SET gemini_api_key_encrypted = NULL, gemini_key_hint = NULL,"
                " updated_at = ? WHERE owner_type = ? AND owner_id = ?",
                (auth.now(), *owner.key),
            )
        else:
            try:
                encrypted = crypto.encrypt(key)
            except crypto.SecretUnavailable as error:
                # On refuse plutôt que d'écrire la clef d'un tiers en clair.
                raise HTTPException(
                    status_code=503,
                    detail={"code": "secret_key_missing", "reason": str(error)},
                ) from error
            db.execute(
                "UPDATE owner_settings SET gemini_api_key_encrypted = ?, gemini_key_hint = ?,"
                " updated_at = ? WHERE owner_type = ? AND owner_id = ?",
                (encrypted, crypto.hint(key), auth.now(), *owner.key),
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
    """Modèles lisibles avec la clef effective — sert à valider la clef saisie."""
    try:
        return gemini.list_models(gemini_key_for(owner))
    except gemini.ExtractionError as error:
        raise HTTPException(
            status_code=400 if error.code == "no_gemini_key" else 502,
            detail={"code": error.code, "reason": error.reason},
        ) from error


# ── Comptes optionnels ────────────────────────────────────────────────────────


def _attach(owner: auth.Owner, account_id: str) -> None:
    """
    Rattache l'appareil au compte et fait suivre ses accès aux groupes.

    Sans ce transfert, quelqu'un qui crée un compte après avoir rejoint des
    groupes les verrait disparaître : ils resteraient rangés sous l'appareil,
    tandis que la lecture se ferait désormais sous le compte.
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


@router.post("/accounts", response_model=Me, status_code=201)
def create_account(
    body: AccountCredentials, owner: auth.Owner = Depends(auth.current_owner)
) -> Me:
    email = body.email.strip().lower()
    if db.query_one("SELECT id FROM account WHERE email = ?", (email,)) is not None:
        raise HTTPException(
            status_code=409,
            detail={"code": "email_taken", "reason": "Cette adresse est déjà utilisée."},
        )
    account_id = auth.new_id()
    db.execute(
        "INSERT INTO account (id, email, password_hash, created_at) VALUES (?, ?, ?, ?)",
        (account_id, email, auth.hash_password(body.password), auth.now()),
    )
    _attach(owner, account_id)
    return read_me(auth.owner_of(owner.device_id, account_id))


@router.post("/sessions", response_model=Me)
def open_session(body: AccountCredentials, owner: auth.Owner = Depends(auth.current_owner)) -> Me:
    """Rattache cet appareil à un compte existant, pour y retrouver ses groupes."""
    row = db.query_one(
        "SELECT id, password_hash FROM account WHERE email = ?", (body.email.strip().lower(),)
    )
    if row is None or not auth.verify_password(body.password, row["password_hash"]):
        raise HTTPException(
            status_code=401,
            detail={"code": "bad_credentials", "reason": "Adresse ou mot de passe incorrect."},
        )
    _attach(owner, row["id"])
    return read_me(auth.owner_of(owner.device_id, row["id"]))
