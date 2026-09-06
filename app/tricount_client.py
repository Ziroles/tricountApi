"""
Accès à Tricount, via le client non officiel `tricount-api`.

Tricount (bunq) ne publie aucune interface programmable. Ce module délègue le
protocole plutôt que de le réimplémenter à l'aveugle, et assume deux
conséquences inchangées depuis l'ancien relais :
 - l'usage sort des conditions d'utilisation du service ;
 - les points d'entrée peuvent disparaître sans préavis, et toute erreur doit se
   traduire par un échec propre, jamais par une dépense à moitié créée.

**Une seule identité d'appareil pour toute l'instance.** Le client génère au
premier appel une paire de clefs qu'il réutilise ensuite. Trois conséquences à
garder en tête :

 1. `list_tricounts()` renverrait les tricounts rejoints par *tous* les
    utilisateurs de l'instance. Il n'est jamais appelé : la liste des groupes
    d'un utilisateur vient de notre table `group_access`, et d'elle seule.
 2. On préfère `get_tricount` à `join_tricount` : lire les membres ne doit pas
    inscrire notre robot dans le tricount de quelqu'un.
 3. Le quota est mutualisé. Si bunq coupe ce robot, l'instance entière tombe.
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

# Le code d'invitation tel qu'il apparaît dans https://tricount.com/tXXXXXXXX.
SHARE_CODE = re.compile(r"^[A-Za-z0-9]{6,}$")
SHARE_URL = re.compile(r"tricount\.com/(?:t/)?([A-Za-z0-9]{6,})")

_lock = threading.Lock()
_client: TricountAPI | None = None


class TricountError(Exception):
    """Échec attendu, converti en réponse « ok: false » avec un statut."""

    def __init__(self, reason: str, status: int = 502, code: str = "tricount_failed") -> None:
        super().__init__(reason)
        self.reason = reason
        self.status = status
        self.code = code


def parse_share_code(raw: str) -> str | None:
    """
    Extrait le code d'invitation d'un lien Tricount, ou accepte le code nu.

    L'utilisateur colle ce qu'il a sous la main : un lien complet partagé depuis
    l'application, ou le code seul. Les deux doivent marcher — refuser le code nu
    au motif qu'il manque un domaine serait une chicane, pas une validation.
    """
    text = (raw or "").strip()
    if text == "":
        return None
    match = SHARE_URL.search(text)
    if match:
        return match.group(1)
    return text if SHARE_CODE.match(text) else None


def get_client() -> TricountAPI:
    """Client authentifié, créé une fois puis réutilisé par le processus."""
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
    """Oublie la session : la prochaine tentative repartira d'une authentification."""
    global _client
    with _lock:
        _client = None


def _call(operation: str, action: Any, *args: Any, **kwargs: Any) -> Any:
    """Appelle Tricount en convertissant tout imprévu en échec propre."""
    try:
        return action(*args, **kwargs)
    except TricountError:
        raise
    except Exception as error:  # noqa: BLE001 — le détail reste côté serveur
        logger.warning("%s a échoué : %s: %s", operation, type(error).__name__, error)
        forget_client()
        raise TricountError(
            "Tricount n'a pas répondu comme attendu. Réessayez, ou utilisez la copie manuelle."
        ) from error


def serialize_members(tricount: Any) -> list[dict[str, str]]:
    """
    Membres exposés à la PWA. Les membres supprimés sont écartés : on ne propose
    pas d'attribuer une part à quelqu'un qui a quitté le tricount.
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
    """Lit un tricount par son code d'invitation, sans y inscrire notre robot."""
    client = get_client()
    try:
        tricount = _call("get_tricount", client.get_tricount, code)
    except TricountError:
        # Certains codes n'ouvrent la lecture qu'après un « join ». On n'y vient
        # qu'en second recours, pour ne pas laisser de trace inutilement.
        logger.info("lecture directe refusée pour %s, tentative de join", code)
        try:
            tricount = _call("join_tricount", client.join_tricount, code, fetch_full=False)
        except TricountError as error:
            # Les deux voies ont échoué. À cette étape, l'utilisateur vient de
            # coller un lien : « ce lien ne mène nulle part » est presque
            # toujours la bonne explication, et c'est la seule sur laquelle il
            # puisse agir. Dire « le service n'a pas répondu » l'enverrait
            # réessayer indéfiniment un code erroné.
            raise TricountError(
                "Aucun tricount ne correspond à ce lien. Vérifiez-le, et qu'il est bien partagé.",
                status=404,
                code="group_not_found",
            ) from error

    if tricount is None:
        raise TricountError("Ce tricount est introuvable.", status=404, code="group_not_found")

    return {
        "id": code,
        "tricountUuid": str(getattr(tricount, "uuid", "") or ""),
        "title": str(getattr(tricount, "title", "") or "Tricount"),
        "currency": str(getattr(tricount, "currency", "") or "CAD"),
        "members": serialize_members(tricount),
    }


def parse_date(raw: Any) -> datetime:
    """Date d'achat du ticket ; à défaut, maintenant."""
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
    Crée une dépense unique, répartie selon les montants fournis.

    Les parts arrivent avec des **uuid de membres**, plus des noms : l'ancien
    appariement par libellé — et sa fragilité au moindre accent — n'existe plus.

    Garde-fou : mieux vaut ne rien envoyer qu'une dépense qui ne tombe pas
    juste. Le client vérifie déjà, mais il s'agit d'argent.
    """
    if total_cents <= 0 or not shares:
        raise TricountError("Dépense vide.", status=400, code="empty_expense")
    if sum(amount for _, amount in shares) != total_cents:
        raise TricountError(
            "La répartition ne correspond pas au total.", status=400, code="split_mismatch"
        )

    client = get_client()
    tricount = _call("join_tricount", client.join_tricount, code, fetch_full=False)
    by_uuid = {str(member.uuid): member for member in tricount.members}

    def member_of(uuid: str) -> Any:
        member = by_uuid.get(uuid)
        if member is None:
            raise TricountError(
                "Un participant ne fait plus partie de ce tricount. Rafraîchissez les membres.",
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
        description=description or "Ticket",
        amount=total_cents / 100,
        payer=payer,
        allocations=allocations,
        date=parse_date(date),
    )
    return str(transaction_id)
