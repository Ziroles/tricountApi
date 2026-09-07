"""
Access to Tricount, through the unofficial `tricount-api` client.

Tricount (bunq) publishes no programmable interface. This module delegates the
protocol rather than reimplementing it blindly, and accepts two consequences
unchanged from the old relay:
 - this use falls outside the service's terms of use;
 - the endpoints can disappear without notice, and every error must turn into a
   clean failure, never a half-created expense.

**One single device identity for the whole instance.** The client generates a
key pair on the first call and reuses it afterwards. Three consequences to keep
in mind:

 1. `list_tricounts()` would return the tricounts joined by *all* the users of
    the instance. It is never called: a user's list of groups comes from our
    `group_access` table, and from nowhere else.
 2. We prefer `get_tricount` over `join_tricount`: reading the members must not
    sign our bot up to someone else's tricount.
 3. The quota is shared. If bunq cuts this bot off, the whole instance goes down.
"""

from __future__ import annotations

import logging
import re
import threading
from datetime import datetime
from typing import Any

from tricount import Credentials, TricountAPI

from . import config

logger = logging.getLogger("splitticket.tricount")

# The invitation code as it appears in https://tricount.com/tXXXXXXXX.
SHARE_CODE = re.compile(r"^[A-Za-z0-9]{6,}$")
SHARE_URL = re.compile(r"tricount\.com/(?:t/)?([A-Za-z0-9]{6,})")

_lock = threading.Lock()
_client: TricountAPI | None = None


class TricountError(Exception):
    """Expected failure, turned into an "ok: false" response with a status."""

    def __init__(self, reason: str, status: int = 502, code: str = "tricount_failed") -> None:
        super().__init__(reason)
        self.reason = reason
        self.status = status
        self.code = code


def parse_share_code(raw: str) -> str | None:
    """
    Extract the invitation code from a Tricount link, or accept the bare code.

    Users paste whatever they have at hand: a full link shared from the app, or
    the code on its own. Both must work — refusing the bare code on the grounds
    that it is missing a domain would be pedantry, not validation.
    """
    text = (raw or "").strip()
    if text == "":
        return None
    match = SHARE_URL.search(text)
    if match:
        return match.group(1)
    return text if SHARE_CODE.match(text) else None


def get_client() -> TricountAPI:
    """Authenticated client, created once and then reused by the process."""
    global _client
    with _lock:
        if _client is None:
            config.ensure_directories()
            if config.CREDENTIALS_PATH.exists():
                credentials = Credentials.load(config.CREDENTIALS_PATH)
            else:
                credentials = Credentials.generate()
                credentials.save(config.CREDENTIALS_PATH)
            client = TricountAPI(credentials)
            client.authenticate()
            _client = client
        return _client


def forget_client() -> None:
    """Forget the session: the next attempt will start from an authentication."""
    global _client
    with _lock:
        _client = None


def _call(operation: str, action: Any, *args: Any, **kwargs: Any) -> Any:
    """Call Tricount, turning anything unexpected into a clean failure."""
    try:
        return action(*args, **kwargs)
    except TricountError:
        raise
    except Exception as error:  # noqa: BLE001 — the detail stays server-side
        logger.warning("%s failed: %s: %s", operation, type(error).__name__, error)
        forget_client()
        raise TricountError(
            "Tricount did not respond as expected. Try again, or copy the split manually."
        ) from error


def serialize_members(tricount: Any) -> list[dict[str, str]]:
    """
    Members exposed to the PWA. Deleted members are left out: we do not offer to
    assign a share to someone who has left the tricount.
    """
    members: list[dict[str, str]] = []
    for member in tricount.members:
        if str(member.status).upper().endswith("DELETED"):
            continue
        members.append(
            {
                "uuid": str(member.uuid),
                "displayName": str(member.display_name),
                "status": str(member.status),
            }
        )
    return members


def fetch_group(code: str) -> dict[str, Any]:
    """Read a tricount by its invitation code, without signing our bot up to it."""
    client = get_client()
    try:
        tricount = _call("get_tricount", client.get_tricount, code)
    except TricountError:
        # Some codes only open up for reading after a "join". We only fall back
        # to it as a second resort, so as not to leave a trace needlessly.
        logger.info("direct read refused for %s, trying join", code)
        try:
            tricount = _call("join_tricount", client.join_tricount, code, fetch_full=False)
        except TricountError as error:
            # Both routes failed. At this point the user has just pasted a link:
            # "this link leads nowhere" is almost always the right explanation,
            # and the only one they can act on. Saying "the service did not
            # respond" would send them retrying a wrong code forever.
            raise TricountError(
                "No tricount matches this link. Check it, and that it is actually shared.",
                status=404,
                code="group_not_found",
            ) from error

    if tricount is None:
        raise TricountError("This tricount cannot be found.", status=404, code="group_not_found")

    return {
        "id": code,
        "tricountUuid": str(getattr(tricount, "uuid", "") or ""),
        "title": str(getattr(tricount, "title", "") or "Tricount"),
        "currency": str(getattr(tricount, "currency", "") or "CAD"),
        "members": serialize_members(tricount),
    }


def parse_date(raw: Any) -> datetime:
    """Purchase date of the receipt; failing that, now."""
    if isinstance(raw, str) and raw:
        for pattern in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
            try:
                return datetime.strptime(raw[: len(datetime.now().strftime(pattern))], pattern)
            except ValueError:
                continue
    return datetime.now()


def create_expense(
    code: str,
    description: str,
    total_cents: int,
    payer_uuid: str,
    shares: list[tuple[str, int]],
    date: str | None,
) -> str:
    """
    Create a single expense, split according to the amounts provided.

    Shares arrive with **member uuids**, not names: the old matching by label —
    and its fragility to the slightest accent — is gone.

    Guardrail: better to send nothing than an expense that does not add up. The
    client already checks, but this is money.
    """
    if total_cents <= 0 or not shares:
        raise TricountError("Empty expense.", status=400, code="empty_expense")
    if sum(amount for _, amount in shares) != total_cents:
        raise TricountError(
            "The split does not match the total.", status=400, code="split_mismatch"
        )

    client = get_client()
    tricount = _call("join_tricount", client.join_tricount, code, fetch_full=False)
    by_uuid = {str(member.uuid): member for member in tricount.members}

    def member_of(uuid: str) -> Any:
        member = by_uuid.get(uuid)
        if member is None:
            raise TricountError(
                "A participant is no longer part of this tricount. Refresh the members.",
                status=409,
                code="member_gone",
            )
        return member

    payer = member_of(payer_uuid)
    allocations = [(member_of(uuid), amount / 100) for uuid, amount in shares]

    transaction_id = _call(
        "create_transaction_custom_split",
        client.create_transaction_custom_split,
        tricount=tricount,
        description=description or "Receipt",
        amount=total_cents / 100,
        payer=payer,
        allocations=allocations,
        date=parse_date(date),
    )
    return str(transaction_id)
