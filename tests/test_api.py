"""
Route tests.

Three properties deserve to be held by tests rather than by vigilance: **scope**
(a device never sees another one's group), **concurrency** (two simultaneous
edits do not overwrite each other silently), and **secrecy** (a saved Gemini key
never comes back out in full).
"""

from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient


# ── Identity ──────────────────────────────────────────────────────────────────


def test_a_device_enrols_without_asking_for_anything(client: TestClient) -> None:
    response = client.post("/v1/devices")
    assert response.status_code == 201
    assert response.json()["token"]


def test_nothing_is_reachable_without_a_token(client: TestClient) -> None:
    assert client.get("/v1/groups").status_code == 401
    assert client.get("/v1/me").status_code == 401


def test_a_made_up_token_is_refused(client: TestClient) -> None:
    response = client.get("/v1/me", headers={"authorization": "Bearer anything"})
    assert response.status_code == 401


def test_the_instance_key_closes_enrolment(env: Any, monkeypatch: Any) -> None:
    import importlib

    monkeypatch.setenv("SPLITTICKET_SIGNUP_KEY", "x" * 32)
    from app import auth, config

    importlib.reload(config)
    importlib.reload(auth)
    from app.routes import identity

    importlib.reload(identity)
    from app import main

    importlib.reload(main)

    with TestClient(main.app) as client:
        assert client.post("/v1/devices").status_code == 401
        assert client.post("/v1/devices", headers={"x-signup-key": "x" * 32}).status_code == 201


# ── Gemini key ────────────────────────────────────────────────────────────────


def test_a_saved_key_never_comes_back_out_in_full(
    client: TestClient, device: dict[str, str]
) -> None:
    client.put("/v1/me/settings", json={"geminiApiKey": "AIzaSyTOPSECRET12345"}, headers=device)
    body = client.get("/v1/me", headers=device).json()

    assert body["settings"]["hasGeminiKey"] is True
    assert body["settings"]["geminiKeyHint"] == "AIza…345"
    # The full key must not appear anywhere in the response.
    assert "AIzaSyTOPSECRET12345" not in client.get("/v1/me", headers=device).text


def test_updating_the_model_does_not_clear_the_key(
    client: TestClient, device: dict[str, str]
) -> None:
    client.put("/v1/me/settings", json={"geminiApiKey": "AIzaSyABCDEFGH"}, headers=device)
    client.put("/v1/me/settings", json={"geminiModel": "gemini-3-flash"}, headers=device)

    settings = client.get("/v1/me", headers=device).json()["settings"]
    assert settings["hasGeminiKey"] is True
    assert settings["geminiModel"] == "gemini-3-flash"


def test_an_empty_string_clears_the_key(client: TestClient, device: dict[str, str]) -> None:
    client.put("/v1/me/settings", json={"geminiApiKey": "AIzaSyABCDEFGH"}, headers=device)
    client.put("/v1/me/settings", json={"geminiApiKey": ""}, headers=device)
    assert client.get("/v1/me", headers=device).json()["settings"]["hasGeminiKey"] is False


def test_the_key_is_encrypted_on_disk(
    client: TestClient, device: dict[str, str], env: Any
) -> None:
    client.put("/v1/me/settings", json={"geminiApiKey": "AIzaSyTOPSECRET12345"}, headers=device)
    from app import config

    contents = config.DB_PATH.read_bytes()
    assert b"AIzaSyTOPSECRET12345" not in contents


# ── Groups ────────────────────────────────────────────────────────────────────


def test_joining_a_group_brings_back_the_tricount_members(
    client: TestClient, device: dict[str, str]
) -> None:
    response = client.post(
        "/v1/groups", json={"shareUrl": "https://tricount.com/tABC123456"}, headers=device
    )
    body = response.json()

    assert response.status_code == 201
    assert body["id"] == "tABC123456"
    assert body["title"] == "Flatshare"
    # Members deleted from the tricount are not offered for assignment.
    assert [member["displayName"] for member in body["members"]] == ["Léa", "Mathieu"]


def test_the_bare_code_works_as_well_as_the_link(
    client: TestClient, device: dict[str, str]
) -> None:
    assert client.post("/v1/groups", json={"shareUrl": "tABC123456"}, headers=device).status_code == 201


def test_something_that_is_not_a_link_is_refused(
    client: TestClient, device: dict[str, str]
) -> None:
    response = client.post("/v1/groups", json={"shareUrl": "hello there!"}, headers=device)
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "invalid_share_url"


def test_joining_twice_does_not_duplicate(
    client: TestClient, device: dict[str, str], group: str
) -> None:
    client.post("/v1/groups", json={"shareUrl": "tABC123456"}, headers=device)
    assert len(client.get("/v1/groups", headers=device).json()) == 1


def test_a_device_does_not_see_another_ones_group(
    client: TestClient, group: str, other_device: dict[str, str]
) -> None:
    assert client.get("/v1/groups", headers=other_device).json() == []
    # 404 and not 403: having no access and not existing are indistinguishable.
    assert client.get(f"/v1/groups/{group}", headers=other_device).status_code == 404


def test_leaving_a_group_only_removes_it_for_yourself(
    client: TestClient, device: dict[str, str], other_device: dict[str, str], group: str
) -> None:
    client.post("/v1/groups", json={"shareUrl": group}, headers=other_device)
    assert client.delete(f"/v1/groups/{group}", headers=device).status_code == 204

    assert client.get("/v1/groups", headers=device).json() == []
    assert len(client.get("/v1/groups", headers=other_device).json()) == 1


# ── Receipts ──────────────────────────────────────────────────────────────────


def _new_receipt(client: TestClient, headers: dict[str, str], group: str) -> dict[str, Any]:
    response = client.post(f"/v1/groups/{group}/receipts", headers=headers)
    assert response.status_code == 201, response.text
    return response.json()


def test_a_receipt_is_born_empty_and_in_draft(
    client: TestClient, device: dict[str, str], group: str
) -> None:
    receipt = _new_receipt(client, device, group)
    assert receipt["status"] == "draft"
    assert receipt["version"] == 1
    assert receipt["lines"] == []


def test_writing_a_receipt_increments_its_version(
    client: TestClient, device: dict[str, str], group: str
) -> None:
    receipt = _new_receipt(client, device, group)
    receipt["merchant"] = "IGA"
    receipt["lines"] = [
        {"id": "l1", "label": "BREAD", "quantity": 1, "unitPriceCents": 349, "totalCents": 349}
    ]

    response = client.put(f"/v1/receipts/{receipt['id']}", json=receipt, headers=device)
    updated = response.json()

    assert response.status_code == 200
    assert updated["version"] == 2
    assert updated["merchant"] == "IGA"
    assert updated["lines"][0]["totalCents"] == 349


def test_two_simultaneous_edits_do_not_overwrite_each_other(
    client: TestClient, device: dict[str, str], other_device: dict[str, str], group: str
) -> None:
    client.post("/v1/groups", json={"shareUrl": group}, headers=other_device)
    receipt = _new_receipt(client, device, group)

    # Both devices start from the same version.
    first, second = dict(receipt), dict(receipt)
    first["merchant"] = "Written first"
    second["merchant"] = "Written second"

    assert client.put(f"/v1/receipts/{receipt['id']}", json=first, headers=device).status_code == 200

    conflict = client.put(f"/v1/receipts/{receipt['id']}", json=second, headers=other_device)
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["code"] == "version_conflict"
    # The current receipt comes with the refusal: the client can explain and resume.
    assert conflict.json()["detail"]["current"]["merchant"] == "Written first"


def test_the_receipt_total_follows_lines_taxes_and_adjustments(
    client: TestClient, device: dict[str, str], group: str
) -> None:
    receipt = _new_receipt(client, device, group)
    receipt["lines"] = [
        {"id": "l1", "label": "A", "quantity": 1, "unitPriceCents": 1000, "totalCents": 1000}
    ]
    receipt["taxes"] = [{"id": "t1", "label": "TPS", "code": "TPS", "amountCents": 50}]
    receipt["adjustments"] = [
        {"id": "a1", "label": "Discount", "amountCents": -100, "mode": "proportional"}
    ]
    client.put(f"/v1/receipts/{receipt['id']}", json=receipt, headers=device)

    summary = client.get(f"/v1/groups/{group}/receipts", headers=device).json()[0]
    # The tip is not part of this total: it does not appear on the receipt.
    assert summary["totalCents"] == 950


def test_a_receipt_is_shared_by_the_whole_group(
    client: TestClient, device: dict[str, str], other_device: dict[str, str], group: str
) -> None:
    receipt = _new_receipt(client, device, group)
    client.post("/v1/groups", json={"shareUrl": group}, headers=other_device)

    assert client.get(f"/v1/receipts/{receipt['id']}", headers=other_device).status_code == 200


def test_a_receipt_stays_invisible_outside_the_group(
    client: TestClient, device: dict[str, str], other_device: dict[str, str], group: str
) -> None:
    receipt = _new_receipt(client, device, group)
    assert client.get(f"/v1/receipts/{receipt['id']}", headers=other_device).status_code == 404


def test_deleting_a_receipt_takes_its_photo_with_it(
    client: TestClient, device: dict[str, str], group: str, env: Any
) -> None:
    from app import config

    receipt = _new_receipt(client, device, group)
    client.post(
        f"/v1/receipts/{receipt['id']}/image",
        files={"file": ("receipt.jpg", b"\xff\xd8\xff-photo", "image/jpeg")},
        headers=device,
    )
    assert len(list(config.IMAGES_DIR.iterdir())) == 1

    client.delete(f"/v1/receipts/{receipt['id']}", headers=device)
    assert list(config.IMAGES_DIR.iterdir()) == []


def test_a_replaced_photo_does_not_leave_the_old_one_behind(
    client: TestClient, device: dict[str, str], group: str, env: Any
) -> None:
    from app import config

    receipt = _new_receipt(client, device, group)
    for _ in range(3):
        client.post(
            f"/v1/receipts/{receipt['id']}/image",
            files={"file": ("t.jpg", b"photo", "image/jpeg")},
            headers=device,
        )
    assert len(list(config.IMAGES_DIR.iterdir())) == 1


def test_an_unsupported_image_format_is_refused(
    client: TestClient, device: dict[str, str], group: str
) -> None:
    receipt = _new_receipt(client, device, group)
    response = client.post(
        f"/v1/receipts/{receipt['id']}/image",
        files={"file": ("receipt.pdf", b"%PDF-1.4", "application/pdf")},
        headers=device,
    )
    assert response.status_code == 415


def test_scanning_without_a_photo_says_so_clearly(
    client: TestClient, device: dict[str, str], group: str
) -> None:
    receipt = _new_receipt(client, device, group)
    response = client.post(f"/v1/receipts/{receipt['id']}/scan", headers=device)
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "image_missing"


def test_without_any_gemini_key_the_message_invites_setting_one(
    client: TestClient, device: dict[str, str], group: str
) -> None:
    receipt = _new_receipt(client, device, group)
    client.post(
        f"/v1/receipts/{receipt['id']}/image",
        files={"file": ("t.jpg", b"photo", "image/jpeg")},
        headers=device,
    )
    response = client.post(f"/v1/receipts/{receipt['id']}/scan", headers=device)

    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "no_gemini_key"
    assert "settings" in response.json()["detail"]["reason"]


# ── Pushing to Tricount ───────────────────────────────────────────────────────


def test_the_push_assigns_shares_by_member_uuid(
    client: TestClient, device: dict[str, str], group: str, tricount: Any
) -> None:
    receipt = _new_receipt(client, device, group)
    response = client.post(
        f"/v1/receipts/{receipt['id']}/push",
        json={
            "description": "IGA — 14/03/2026",
            "totalCents": 3000,
            "payerMemberUuid": "m-lea",
            "shares": [
                {"memberUuid": "m-lea", "amountCents": 2000},
                {"memberUuid": "m-mathieu", "amountCents": 1000},
            ],
            "date": "2026-03-14",
        },
        headers=device,
    )

    assert response.status_code == 200
    assert response.json()["transactionId"] == "987654"

    sent = tricount.created[-1]
    assert sent["payer"].uuid == "m-lea"
    assert [(member.uuid, amount) for member, amount in sent["allocations"]] == [
        ("m-lea", 20.0),
        ("m-mathieu", 10.0),
    ]


def test_a_split_that_does_not_add_up_is_not_sent(
    client: TestClient, device: dict[str, str], group: str, tricount: Any
) -> None:
    receipt = _new_receipt(client, device, group)
    response = client.post(
        f"/v1/receipts/{receipt['id']}/push",
        json={
            "totalCents": 3000,
            "payerMemberUuid": "m-lea",
            # One cent is missing: better to send nothing.
            "shares": [
                {"memberUuid": "m-lea", "amountCents": 2000},
                {"memberUuid": "m-mathieu", "amountCents": 999},
            ],
        },
        headers=device,
    )

    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "split_mismatch"
    assert tricount.created == []


def test_a_member_gone_from_the_tricount_stops_the_push(
    client: TestClient, device: dict[str, str], group: str, tricount: Any
) -> None:
    receipt = _new_receipt(client, device, group)
    response = client.post(
        f"/v1/receipts/{receipt['id']}/push",
        json={
            "totalCents": 1000,
            "payerMemberUuid": "m-lea",
            "shares": [{"memberUuid": "m-ghost", "amountCents": 1000}],
        },
        headers=device,
    )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "member_gone"
    assert tricount.created == []


# ── Optional accounts ─────────────────────────────────────────────────────────


def test_an_account_finds_its_groups_again_from_another_device(
    client: TestClient, device: dict[str, str], other_device: dict[str, str], group: str
) -> None:
    credentials = {"email": "Ziroles@example.com", "password": "a-strong-password"}
    assert client.post("/v1/accounts", json=credentials, headers=device).status_code == 201

    # The group joined before the account was created must have followed.
    assert [g["id"] for g in client.get("/v1/groups", headers=device).json()] == [group]

    assert client.post("/v1/sessions", json=credentials, headers=other_device).status_code == 200
    assert [g["id"] for g in client.get("/v1/groups", headers=other_device).json()] == [group]


def test_a_wrong_password_attaches_nothing(
    client: TestClient, device: dict[str, str], other_device: dict[str, str]
) -> None:
    client.post(
        "/v1/accounts",
        json={"email": "a@example.com", "password": "a-strong-password"},
        headers=device,
    )
    response = client.post(
        "/v1/sessions", json={"email": "a@example.com", "password": "not-the-right-one"}, headers=other_device
    )
    assert response.status_code == 401


def test_an_address_already_taken_is_refused(
    client: TestClient, device: dict[str, str], other_device: dict[str, str]
) -> None:
    credentials = {"email": "a@example.com", "password": "a-strong-password"}
    client.post("/v1/accounts", json=credentials, headers=device)
    assert client.post("/v1/accounts", json=credentials, headers=other_device).status_code == 409


def test_health_announces_what_the_instance_can_do(client: TestClient) -> None:
    body = client.get("/health").json()
    assert body["ok"] is True
    assert body["serverHasGeminiKey"] is False
    assert body["canStoreUserKeys"] is True


def test_an_unknown_code_talks_about_the_link_not_the_service(
    client: TestClient, device: dict[str, str]
) -> None:
    """`tUNKNOWN` has the shape of a code but leads nowhere. The message must send
    the user to check their link, not make them retry endlessly."""
    response = client.post("/v1/groups", json={"shareUrl": "tUNKNOWN"}, headers=device)

    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "group_not_found"
    assert "link" in response.json()["detail"]["reason"]


# ── Housekeeping ──────────────────────────────────────────────────────────────


def test_the_purge_deletes_the_photo_but_keeps_the_receipt(
    client: TestClient, device: dict[str, str], group: str, env: Any
) -> None:
    from app import config, db, maintenance

    receipt = _new_receipt(client, device, group)
    client.post(
        f"/v1/receipts/{receipt['id']}/image",
        files={"file": ("t.jpg", b"photo", "image/jpeg")},
        headers=device,
    )
    # Age the photo by six months.
    db.execute("UPDATE image SET created_at = '2020-01-01T00:00:00+00:00'")

    assert maintenance.purge_old_images(days=90) == 1

    assert list(config.IMAGES_DIR.iterdir()) == []
    # The receipt survives, and knows it has no photo any more.
    body = client.get(f"/v1/receipts/{receipt['id']}", headers=device).json()
    assert body["imageId"] is None
    assert client.get(f"/v1/receipts/{receipt['id']}/image", headers=device).status_code == 404


def test_the_purge_spares_recent_photos(
    client: TestClient, device: dict[str, str], group: str
) -> None:
    from app import maintenance

    receipt = _new_receipt(client, device, group)
    client.post(
        f"/v1/receipts/{receipt['id']}/image",
        files={"file": ("t.jpg", b"photo", "image/jpeg")},
        headers=device,
    )
    assert maintenance.purge_old_images(days=90) == 0
    assert client.get(f"/v1/receipts/{receipt['id']}", headers=device).json()["imageId"] is not None


def test_a_zero_retention_disables_the_purge(
    client: TestClient, device: dict[str, str], group: str
) -> None:
    """Never purging is a hosting choice, not a value to correct."""
    from app import db, maintenance

    receipt = _new_receipt(client, device, group)
    client.post(
        f"/v1/receipts/{receipt['id']}/image",
        files={"file": ("t.jpg", b"photo", "image/jpeg")},
        headers=device,
    )
    db.execute("UPDATE image SET created_at = '2020-01-01T00:00:00+00:00'")

    assert maintenance.purge_old_images(days=0) == 0
    assert client.get(f"/v1/receipts/{receipt['id']}", headers=device).json()["imageId"] is not None


def test_the_purge_collects_orphan_files(
    client: TestClient, device: dict[str, str], group: str, env: Any
) -> None:
    """An upload cut off between the file and the database leaves a file nothing names."""
    from app import config, maintenance

    config.ensure_directories()
    (config.IMAGES_DIR / "orphan.bin").write_bytes(b"lost")

    assert maintenance.purge_orphan_files() == 1
    assert list(config.IMAGES_DIR.iterdir()) == []


def test_health_announces_the_contract_version(client: TestClient) -> None:
    body = client.get("/health").json()
    assert body["contractVersion"] == "1"
    assert body["imageRetentionDays"] == 90
