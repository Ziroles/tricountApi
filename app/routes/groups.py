"""
Groups: a group **is** a tricount, designated by its invitation code.

Joining amounts to pasting a link. The API then reads the members from Tricount
and caches them: that is the whole "less setup" we were after — nobody types the
list of participants in by hand any more.

Scope rule, without exception: a user's list of groups comes from `group_access`,
never from `list_tricounts()`. The Tricount bot is shared by the whole instance;
querying it would amount to showing everyone everybody else's groups.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from .. import auth, db, tricount_client
from ..models import Group, GroupSummary, JoinGroupRequest, Member

router = APIRouter(prefix="/v1/groups", tags=["groups"])


def require_access(group_id: str, owner: auth.Owner) -> None:
    """
    Gatekeeper for every group-related route.

    Answers 404, not 403: having no access and not existing must be
    indistinguishable, otherwise the response would confirm the existence of a
    group to someone with no business knowing about it.
    """
    row = db.query_one(
        "SELECT 1 FROM group_access WHERE group_id = ? AND owner_type = ? AND owner_id = ?",
        (group_id, *owner.key),
    )
    if row is None:
        raise HTTPException(
            status_code=404, detail={"code": "group_not_found", "reason": "Group not found."}
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
            status_code=404, detail={"code": "group_not_found", "reason": "Group not found."}
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
    """Write or refresh the snapshot of a tricount."""
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
    # The group where something happened recently comes to the top.
    summaries.sort(key=lambda entry: entry.lastActivityAt or "", reverse=True)
    return summaries


@router.post("", response_model=Group, status_code=201)
def join_group(body: JoinGroupRequest, owner: auth.Owner = Depends(auth.current_owner)) -> Group:
    """Join a group from a Tricount share link, or from the bare code."""
    code = tricount_client.parse_share_code(body.shareUrl)
    if code is None:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "invalid_share_url",
                "reason": "Invalid share link. Paste a tricount.com link or its code.",
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
    """Resynchronise the members: someone joined the tricount, or changed their name."""
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
    Remove access for the caller only. The group and its receipts stay for the
    other members: leaving is not deleting everyone else's work.
    """
    require_access(group_id, owner)
    db.execute(
        "DELETE FROM group_access WHERE group_id = ? AND owner_type = ? AND owner_id = ?",
        (group_id, *owner.key),
    )
