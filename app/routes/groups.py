"""
Groupes : un groupe **est** un tricount, désigné par son code d'invitation.

Rejoindre revient à coller un lien. L'API lit alors les membres chez Tricount et
les met en cache : c'est là tout le « moins de setup » recherché — plus personne
ne ressaisit à la main la liste des participants.

Règle de portée, sans exception : la liste des groupes d'un utilisateur vient de
`group_access`, jamais de `list_tricounts()`. Le robot Tricount est partagé par
toute l'instance ; l'interroger reviendrait à montrer à chacun les groupes de
tous les autres.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from .. import auth, db, tricount_client
from ..models import Group, GroupSummary, JoinGroupRequest, Member

router = APIRouter(prefix="/v1/groups", tags=["groupes"])


def require_access(group_id: str, owner: auth.Owner) -> None:
    """
    Garde-barrière de toutes les routes liées à un groupe.

    Répond 404, pas 403 : ne pas y avoir accès et ne pas exister doivent être
    indiscernables, faute de quoi la réponse confirmerait l'existence d'un
    groupe à qui n'y a rien à faire.
    """
    row = db.query_one(
        "SELECT 1 FROM group_access WHERE group_id = ? AND owner_type = ? AND owner_id = ?",
        (group_id, *owner.key),
    )
    if row is None:
        raise HTTPException(
            status_code=404, detail={"code": "group_not_found", "reason": "Groupe introuvable."}
        )


def _stats(group_id: str) -> tuple[int, str | None]:
    row = db.query_one(
        "SELECT COUNT(*) AS count, MAX(updated_at) AS last FROM receipt WHERE group_id = ?",
        (group_id,),
    )
    return (row["count"] if row else 0), (row["last"] if row else None)


def _load(group_id: str) -> Group:
    row = db.query_one("SELECT * FROM tricount_group WHERE id = ?", (group_id,))
    if row is None:
        raise HTTPException(
            status_code=404, detail={"code": "group_not_found", "reason": "Groupe introuvable."}
        )
    count, last = _stats(group_id)
    return Group(
        id=row["id"],
        title=row["title"],
        currency=row["currency"],
        members=[Member(**member) for member in db.loads(row["members_json"], [])],
        membersSyncedAt=row["members_synced_at"],
        receiptCount=count,
        lastActivityAt=last,
    )


def _persist(snapshot: dict, stamp: str) -> None:
    """Écrit ou rafraîchit l'instantané d'un tricount."""
    db.execute(
        "INSERT INTO tricount_group (id, tricount_uuid, title, currency, members_json,"
        " members_synced_at, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)"
        " ON CONFLICT(id) DO UPDATE SET tricount_uuid = excluded.tricount_uuid,"
        " title = excluded.title, currency = excluded.currency,"
        " members_json = excluded.members_json, members_synced_at = excluded.members_synced_at",
        (
            snapshot["id"],
            snapshot["tricountUuid"],
            snapshot["title"],
            snapshot["currency"],
            db.dumps(snapshot["members"]),
            stamp,
            stamp,
        ),
    )


@router.get("", response_model=list[GroupSummary])
def list_groups(owner: auth.Owner = Depends(auth.current_owner)) -> list[GroupSummary]:
    rows = db.query(
        "SELECT g.* FROM tricount_group g"
        " JOIN group_access a ON a.group_id = g.id"
        " WHERE a.owner_type = ? AND a.owner_id = ?"
        " ORDER BY a.joined_at DESC",
        owner.key,
    )
    summaries: list[GroupSummary] = []
    for row in rows:
        count, last = _stats(row["id"])
        summaries.append(
            GroupSummary(
                id=row["id"],
                title=row["title"],
                currency=row["currency"],
                memberCount=len(db.loads(row["members_json"], [])),
                receiptCount=count,
                lastActivityAt=last,
            )
        )
    # Le groupe où il s'est passé quelque chose récemment remonte en tête.
    summaries.sort(key=lambda entry: entry.lastActivityAt or "", reverse=True)
    return summaries


@router.post("", response_model=Group, status_code=201)
def join_group(body: JoinGroupRequest, owner: auth.Owner = Depends(auth.current_owner)) -> Group:
    """Rejoint un groupe depuis un lien de partage Tricount, ou depuis le code nu."""
    code = tricount_client.parse_share_code(body.shareUrl)
    if code is None:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "invalid_share_url",
                "reason": "Lien de partage invalide. Collez un lien tricount.com ou son code.",
            },
        )

    try:
        snapshot = tricount_client.fetch_group(code)
    except tricount_client.TricountError as error:
        raise HTTPException(
            status_code=error.status, detail={"code": error.code, "reason": error.reason}
        ) from error

    stamp = auth.now()
    _persist(snapshot, stamp)
    db.execute(
        "INSERT OR IGNORE INTO group_access (group_id, owner_type, owner_id, joined_at)"
        " VALUES (?, ?, ?, ?)",
        (code, *owner.key, stamp),
    )
    return _load(code)


@router.get("/{group_id}", response_model=Group)
def read_group(group_id: str, owner: auth.Owner = Depends(auth.current_owner)) -> Group:
    require_access(group_id, owner)
    return _load(group_id)


@router.post("/{group_id}/members/refresh", response_model=Group)
def refresh_members(group_id: str, owner: auth.Owner = Depends(auth.current_owner)) -> Group:
    """Resynchronise les membres : quelqu'un a rejoint le tricount, ou changé de nom."""
    require_access(group_id, owner)
    try:
        snapshot = tricount_client.fetch_group(group_id)
    except tricount_client.TricountError as error:
        raise HTTPException(
            status_code=error.status, detail={"code": error.code, "reason": error.reason}
        ) from error
    _persist(snapshot, auth.now())
    return _load(group_id)


@router.delete("/{group_id}", status_code=204)
def leave_group(group_id: str, owner: auth.Owner = Depends(auth.current_owner)) -> None:
    """
    Retire l'accès du seul appelant. Le groupe et ses tickets restent pour les
    autres membres : quitter n'est pas supprimer le travail de tout le monde.
    """
    require_access(group_id, owner)
    db.execute(
        "DELETE FROM group_access WHERE group_id = ? AND owner_type = ? AND owner_id = ?",
        (group_id, *owner.key),
    )
