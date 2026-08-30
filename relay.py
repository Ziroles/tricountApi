"""
Relais HTTP pour l'envoi vers Tricount (§9).

Tricount (bunq) ne publie aucune interface programmable. Ce relais délègue le
protocole à `tricount-api`, un client non officiel rétro-conçu depuis
l'application Android, plutôt que de le réimplémenter à l'aveugle.

Deux conséquences assumées, inchangées :
 - l'usage sort des conditions d'utilisation du service ;
 - les points d'entrée peuvent disparaître sans préavis, et toute erreur doit
   se traduire par un échec propre, jamais par une dépense à moitié créée.

Aucune clé applicative n'est nécessaire côté Tricount : le client génère au
premier appel une paire de clés et un identifiant d'appareil, puis les
réutilise. Rejoindre un tricount ne demande que son code de partage.

Le relais lui-même est protégé par une clef statique de 32 caractères,
`TRICOUNT_RELAY_KEY`, que l'appelant présente à chaque requête. Il n'écoute que
la boucle locale, mais tout programme de la machine peut l'atteindre : la clef
distingue l'appelant légitime, sans session ni compte à gérer.

Le relais est un serveur HTTP autonome, sans cadriciel ni plateforme :
`python3 api/relay.py`, lancé automatiquement par `npm run dev`.

Le fichier ne peut pas s'appeler `tricount.py` : Python place le dossier du
script en tête de `sys.path`, où il masquerait la librairie qu'il importe.
"""

from __future__ import annotations

import hmac
import json
import os
import re
import secrets
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

from tricount import Credentials, TricountAPI

# Un code de partage tel qu'il apparaît dans https://tricount.com/tXXXXXXXX.
SHARE_CODE = re.compile(r"^[A-Za-z0-9]{6,}$")

# Les identifiants d'appareil sont réutilisables : les conserver évite d'en
# réenregistrer un à chaque démarrage.
CREDENTIALS_PATH = Path(
    os.environ.get("TRICOUNT_CREDENTIALS_PATH")
    or Path(__file__).resolve().parent.parent / ".tricount-credentials.json"
)

# Clef d'accès au relais : longueur fixe, comparée telle quelle. Trente-deux
# caractères tirés au hasard suffisent largement face à un appelant local.
RELAY_KEY_LENGTH = 32
RELAY_KEY = (os.environ.get("TRICOUNT_RELAY_KEY") or "").strip()

_lock = threading.Lock()
_client: TricountAPI | None = None


class RelayError(Exception):
    """Échec attendu, converti en réponse « ok: false » avec un statut."""

    def __init__(self, reason: str, status: int = 502) -> None:
        super().__init__(reason)
        self.reason = reason
        self.status = status


def presented_key(headers: Any) -> str:
    """Clef portée par la requête : « authorization: Bearer … » ou « x-api-key »."""
    scheme, _, value = (headers.get("authorization") or "").partition(" ")
    if scheme.lower() == "bearer" and value.strip():
        return value.strip()
    return (headers.get("x-api-key") or "").strip()


def check_key(presented: str) -> None:
    """
    Vérifie la clef présentée. Le relais refuse de servir tant qu'aucune clef
    correcte n'est configurée : mieux vaut un service muet qu'un service ouvert.
    La comparaison est à temps constant, par principe.
    """
    if len(RELAY_KEY) != RELAY_KEY_LENGTH:
        raise RelayError("Relais non configuré : clef d'accès absente.", 503)
    if not hmac.compare_digest(presented, RELAY_KEY):
        raise RelayError("Clef d'accès invalide.", 401)


def get_client() -> TricountAPI:
    """Client authentifié, créé une fois puis réutilisé par le processus."""
    global _client
    with _lock:
        if _client is None:
            if CREDENTIALS_PATH.exists():
                credentials = Credentials.load(CREDENTIALS_PATH)
            else:
                credentials = Credentials.generate()
                CREDENTIALS_PATH.parent.mkdir(parents=True, exist_ok=True)
                credentials.save(CREDENTIALS_PATH)
            client = TricountAPI(credentials)
            client.authenticate()
            _client = client
        return _client


def parse_date(raw: Any) -> datetime:
    """Date d'achat du ticket ; à défaut, maintenant."""
    if isinstance(raw, str) and raw:
        for pattern in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S"):
            try:
                return datetime.strptime(raw[: len(datetime.now().strftime(pattern))], pattern)
            except ValueError:
                continue
    return datetime.now()


def find_member(tricount: Any, name: str) -> Any:
    """
    Membre portant ce nom. Les participants de l'application et ceux du
    tricount sont saisis par la même personne : on s'appuie sur cette
    correspondance plutôt que sur une association manuelle. La comparaison
    ignore la casse et les espaces de bord, seule tolérance raisonnable.
    """
    wanted = name.strip().casefold()
    for member in tricount.members:
        if str(member.status) == "DELETED":
            continue
        if member.display_name.strip().casefold() == wanted:
            return member
    raise RelayError(f"Aucun membre nommé « {name} » dans ce tricount.", 400)


def action_expense(code: str, body: dict[str, Any]) -> dict[str, str]:
    """Crée une dépense unique, répartie selon les montants fournis."""
    total_cents = int(body.get("totalCents") or 0)
    shares = body.get("shares") or []
    if total_cents <= 0 or not shares:
        raise RelayError("Dépense vide.", 400)

    # Garde-fou : mieux vaut ne rien envoyer qu'une dépense qui ne tombe pas
    # juste. Le client vérifie déjà, mais il s'agit d'argent.
    if sum(int(share.get("amountCents") or 0) for share in shares) != total_cents:
        raise RelayError("La répartition ne correspond pas au total.", 400)

    client = get_client()
    tricount = client.join_tricount(code, fetch_full=False)

    payer = find_member(tricount, str(body.get("payerName") or ""))
    allocations = [
        (find_member(tricount, str(share.get("name") or "")), int(share["amountCents"]) / 100)
        for share in shares
    ]

    transaction_id = client.create_transaction_custom_split(
        tricount=tricount,
        description=str(body.get("description") or "Ticket"),
        amount=total_cents / 100,
        payer=payer,
        allocations=allocations,
        date=parse_date(body.get("date")),
    )
    return {"id": str(transaction_id)}


def dispatch(body: dict[str, Any]) -> dict[str, Any]:
    """Route une requête déjà décodée vers l'action demandée."""
    code = str(body.get("code") or "")
    if not SHARE_CODE.match(code):
        raise RelayError("Code de partage invalide.", 400)

    action = body.get("action")
    if action == "expense":
        return {"ok": True, "value": action_expense(code, body)}
    raise RelayError("Action inconnue.", 400)


def handle(raw: bytes) -> tuple[int, dict[str, Any]]:
    """Cœur du relais, indépendant du transport : octets reçus → réponse."""
    try:
        body = json.loads(raw or b"{}")
    except ValueError:
        return 400, {"ok": False, "reason": "Requête illisible."}
    if not isinstance(body, dict):
        return 400, {"ok": False, "reason": "Requête illisible."}

    try:
        return 200, dispatch(body)
    except RelayError as error:
        return error.status, {"ok": False, "reason": error.reason}
    except Exception as error:  # noqa: BLE001 — le détail reste côté serveur
        # La session a pu expirer : la prochaine tentative repartira à zéro.
        global _client
        with _lock:
            _client = None
        print(f"[tricount] {type(error).__name__}: {error}", flush=True)
        return 502, {"ok": False, "reason": "L'envoi automatique n'a pas fonctionné."}


class RelayHandler(BaseHTTPRequestHandler):
    """Transport HTTP : décode la requête, délègue à `handle`, répond."""

    def _send(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("cache-control", "no-store")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:  # noqa: N802 — nom imposé par BaseHTTPRequestHandler
        # Le corps est lu dans tous les cas : la requête doit être consommée
        # entièrement avant la réponse, refus compris.
        length = int(self.headers.get("content-length") or 0)
        raw = self.rfile.read(length)
        try:
            check_key(presented_key(self.headers))
        except RelayError as error:
            self._send(error.status, {"ok": False, "reason": error.reason})
            return
        status, payload = handle(raw)
        self._send(status, payload)

    def do_GET(self) -> None:  # noqa: N802
        self._send(405, {"ok": False, "reason": "Méthode non autorisée."})

    def log_message(self, format: str, *args: Any) -> None:
        """Journal concis : le relais parle déjà via ses propres messages."""
        return


if __name__ == "__main__":
    if len(RELAY_KEY) != RELAY_KEY_LENGTH:
        # Rien ne serait servi de toute façon : autant le dire au démarrage, et
        # proposer une clef valable plutôt que de laisser en inventer une.
        raise SystemExit(
            f"[tricount] TRICOUNT_RELAY_KEY doit faire {RELAY_KEY_LENGTH} caractères.\n"
            f"[tricount] par exemple : TRICOUNT_RELAY_KEY={secrets.token_urlsafe(24)[:RELAY_KEY_LENGTH]}"
        )

    port = int(os.environ.get("TRICOUNT_RELAY_PORT") or 8787)
    print(f"[tricount] relais local sur http://{host}:{port}", flush=True)
    HTTPServer((host, port), RelayHandler).serve_forever()
