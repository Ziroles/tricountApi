"""
Point d'entrée de l'API SplitTicket.

    uvicorn app.main:app --host 0.0.0.0 --port 8787

Le service porte désormais quatre choses que le client faisait, ou ne faisait
pas du tout : la lecture OCR des tickets, les groupes adossés à Tricount, le
stockage partagé des tickets, et l'envoi de la dépense.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from . import config, db, maintenance
from .routes import groups, identity, receipts

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s %(message)s"
)
logger = logging.getLogger("splitticket")


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    """
    Prépare la base au démarrage, et dit tout de suite ce que l'instance ne
    saura pas faire. Une capacité manquante doit se voir au lancement, pas se
    découvrir au premier utilisateur qui bute dessus.
    """
    config.ensure_directories()
    db.connect()
    if config.SECRET_KEY == "":
        logger.warning(
            "SPLITTICKET_SECRET_KEY absente : les utilisateurs ne pourront pas enregistrer "
            "leur clé Gemini (on refuse de l'écrire en clair)."
        )
    if config.GEMINI_API_KEY == "":
        logger.info("Aucune clé Gemini d'instance : chaque utilisateur apportera la sienne.")
    if config.SIGNUP_KEY == "":
        logger.warning("SPLITTICKET_SIGNUP_KEY absente : l'enrôlement d'appareil est ouvert.")

    # L'entretien tourne en tâche de fond, et s'arrête avec le service.
    housekeeping = asyncio.create_task(maintenance.run_periodically())
    try:
        yield
    finally:
        housekeeping.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await housekeeping


app = FastAPI(
    title="SplitTicket API",
    version="1.0.0",
    description="Lecture de tickets, groupes Tricount et répartition partagée.",
    lifespan=lifespan,
)

# L'authentification est un jeton porté, pas un cookie : il n'y a pas de CSRF à
# craindre, et `allow_credentials` resterait sans objet. On ne l'active donc pas,
# ce qui laisse « * » utilisable pour un déploiement personnel.
app.add_middleware(
    CORSMiddleware,
    allow_origins=config.ALLOWED_ORIGINS,
    allow_credentials=False,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["authorization", "content-type", "x-signup-key"],
    max_age=86400,
)

app.include_router(identity.router)
app.include_router(groups.router)
app.include_router(receipts.router)


@app.exception_handler(Exception)
def unhandled(request: Request, error: Exception) -> JSONResponse:
    """
    Filet de sécurité : le détail reste dans le journal du serveur, l'appelant
    reçoit un message utilisable. Une trace de pile n'a jamais aidé personne à
    répartir un ticket.
    """
    logger.exception("erreur non gérée sur %s %s", request.method, request.url.path)
    return JSONResponse(
        status_code=500,
        content={"code": "internal_error", "reason": "Une erreur inattendue est survenue."},
    )


@app.get("/health", tags=["service"])
def health() -> dict[str, object]:
    return {
        "ok": True,
        "contractVersion": config.CONTRACT_VERSION,
        "serverHasGeminiKey": config.GEMINI_API_KEY != "",
        "signupKeyRequired": config.SIGNUP_KEY != "",
        "canStoreUserKeys": config.SECRET_KEY != "",
        "imageRetentionDays": config.IMAGE_RETENTION_DAYS,
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=config.HOST, port=config.PORT)
