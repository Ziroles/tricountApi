"""
Entry point of the SplitTicket API.

    uvicorn app.main:app --host 0.0.0.0 --port 8787

The service now carries four things the client used to do, or did not do at all:
OCR reading of receipts, groups backed by Tricount, shared storage of receipts,
and pushing the expense.
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
    Prepare the database at startup, and say right away what this instance will
    not be able to do. A missing capability must be visible at launch, not
    discovered by the first user who runs into it.
    """
    config.ensure_directories()
    db.connect()
    if config.SECRET_KEY == "":
        logger.warning(
            "SPLITTICKET_SECRET_KEY is missing: users will not be able to save their "
            "Gemini key (we refuse to write it in the clear)."
        )
    if config.GEMINI_API_KEY == "":
        logger.info("No instance Gemini key: every user will bring their own.")
    if config.SIGNUP_KEY == "":
        logger.warning("SPLITTICKET_SIGNUP_KEY is missing: device enrolment is open.")

    # Housekeeping runs in the background, and stops with the service.
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
    description="Receipt reading, Tricount groups and shared splitting.",
    lifespan=lifespan,
)

# Authentication is a bearer token, not a cookie: there is no CSRF to worry
# about, and `allow_credentials` would be pointless. We therefore leave it off,
# which keeps "*" usable for a personal deployment.
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
    Safety net: the detail stays in the server log, the caller gets a usable
    message. A stack trace has never helped anyone split a receipt.
    """
    logger.exception("unhandled error on %s %s", request.method, request.url.path)
    return JSONResponse(
        status_code=500,
        content={"code": "internal_error", "reason": "An unexpected error occurred."},
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
