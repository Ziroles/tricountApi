"""
Tests des routes.

Trois propriétés méritent d'être tenues par des tests plutôt que par la
vigilance : **la portée** (un appareil ne voit jamais le groupe d'un autre), **la
concurrence** (deux éditions simultanées ne s'écrasent pas en silence), et **le
secret** (une clef Gemini enregistrée ne ressort jamais entière).
"""

from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient


# ── Identité ──────────────────────────────────────────────────────────────────


def test_un_appareil_s_enrole_sans_rien_demander(client: TestClient) -> None:
    response = client.post("/v1/devices")
    assert response.status_code == 201
    assert response.json()["token"]


def test_sans_jeton_rien_n_est_accessible(client: TestClient) -> None:
    assert client.get("/v1/groups").status_code == 401
    assert client.get("/v1/me").status_code == 401


def test_un_jeton_inventé_est_refusé(client: TestClient) -> None:
    response = client.get("/v1/me", headers={"authorization": "Bearer nimportequoi"})
    assert response.status_code == 401


def test_la_clef_d_instance_ferme_l_enrolement(env: Any, monkeypatch: Any) -> None:
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


# ── Clef Gemini ───────────────────────────────────────────────────────────────


def test_une_clef_enregistree_ne_ressort_jamais_entiere(
    client: TestClient, device: dict[str, str]
) -> None:
    client.put("/v1/me/settings", json={"geminiApiKey": "AIzaSyTOPSECRET12345"}, headers=device)
    body = client.get("/v1/me", headers=device).json()

    assert body["settings"]["hasGeminiKey"] is True
    assert body["settings"]["geminiKeyHint"] == "AIza…345"
    # La clef complète ne doit apparaître nulle part dans la réponse.
    assert "AIzaSyTOPSECRET12345" not in client.get("/v1/me", headers=device).text


def test_mettre_a_jour_le_modele_n_efface_pas_la_clef(
    client: TestClient, device: dict[str, str]
) -> None:
    client.put("/v1/me/settings", json={"geminiApiKey": "AIzaSyABCDEFGH"}, headers=device)
    client.put("/v1/me/settings", json={"geminiModel": "gemini-3-flash"}, headers=device)

    settings = client.get("/v1/me", headers=device).json()["settings"]
    assert settings["hasGeminiKey"] is True
    assert settings["geminiModel"] == "gemini-3-flash"


def test_une_chaine_vide_efface_la_clef(client: TestClient, device: dict[str, str]) -> None:
    client.put("/v1/me/settings", json={"geminiApiKey": "AIzaSyABCDEFGH"}, headers=device)
    client.put("/v1/me/settings", json={"geminiApiKey": ""}, headers=device)
    assert client.get("/v1/me", headers=device).json()["settings"]["hasGeminiKey"] is False


def test_la_clef_est_chiffree_sur_le_disque(
    client: TestClient, device: dict[str, str], env: Any
) -> None:
    client.put("/v1/me/settings", json={"geminiApiKey": "AIzaSyTOPSECRET12345"}, headers=device)
    from app import config

    contents = config.DB_PATH.read_bytes()
    assert b"AIzaSyTOPSECRET12345" not in contents


# ── Groupes ───────────────────────────────────────────────────────────────────


def test_rejoindre_un_groupe_ramene_les_membres_du_tricount(
    client: TestClient, device: dict[str, str]
) -> None:
    response = client.post(
        "/v1/groups", json={"shareUrl": "https://tricount.com/tABC123456"}, headers=device
    )
    body = response.json()

    assert response.status_code == 201
    assert body["id"] == "tABC123456"
    assert body["title"] == "Colocation"
    # Les membres supprimés du tricount ne sont pas proposés à l'attribution.
    assert [member["displayName"] for member in body["members"]] == ["Léa", "Mathieu"]


def test_le_code_nu_marche_autant_que_le_lien(
    client: TestClient, device: dict[str, str]
) -> None:
    assert client.post("/v1/groups", json={"shareUrl": "tABC123456"}, headers=device).status_code == 201


def test_un_lien_qui_n_en_est_pas_un_est_refuse(
    client: TestClient, device: dict[str, str]
) -> None:
    response = client.post("/v1/groups", json={"shareUrl": "bonjour !"}, headers=device)
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "invalid_share_url"


def test_rejoindre_deux_fois_ne_duplique_pas(
    client: TestClient, device: dict[str, str], group: str
) -> None:
    client.post("/v1/groups", json={"shareUrl": "tABC123456"}, headers=device)
    assert len(client.get("/v1/groups", headers=device).json()) == 1


def test_un_appareil_ne_voit_pas_le_groupe_d_un_autre(
    client: TestClient, group: str, other_device: dict[str, str]
) -> None:
    assert client.get("/v1/groups", headers=other_device).json() == []
    # 404 et non 403 : ne pas y avoir accès et ne pas exister sont indiscernables.
    assert client.get(f"/v1/groups/{group}", headers=other_device).status_code == 404


def test_quitter_un_groupe_ne_le_supprime_que_pour_soi(
    client: TestClient, device: dict[str, str], other_device: dict[str, str], group: str
) -> None:
    client.post("/v1/groups", json={"shareUrl": group}, headers=other_device)
    assert client.delete(f"/v1/groups/{group}", headers=device).status_code == 204

    assert client.get("/v1/groups", headers=device).json() == []
    assert len(client.get("/v1/groups", headers=other_device).json()) == 1


# ── Tickets ───────────────────────────────────────────────────────────────────


def _new_receipt(client: TestClient, headers: dict[str, str], group: str) -> dict[str, Any]:
    response = client.post(f"/v1/groups/{group}/receipts", headers=headers)
    assert response.status_code == 201, response.text
    return response.json()


def test_un_ticket_naît_vide_et_en_brouillon(
    client: TestClient, device: dict[str, str], group: str
) -> None:
    receipt = _new_receipt(client, device, group)
    assert receipt["status"] == "draft"
    assert receipt["version"] == 1
    assert receipt["lines"] == []


def test_ecrire_un_ticket_incremente_sa_version(
    client: TestClient, device: dict[str, str], group: str
) -> None:
    receipt = _new_receipt(client, device, group)
    receipt["merchant"] = "IGA"
    receipt["lines"] = [
        {"id": "l1", "label": "PAIN", "quantity": 1, "unitPriceCents": 349, "totalCents": 349}
    ]

    response = client.put(f"/v1/receipts/{receipt['id']}", json=receipt, headers=device)
    updated = response.json()

    assert response.status_code == 200
    assert updated["version"] == 2
    assert updated["merchant"] == "IGA"
    assert updated["lines"][0]["totalCents"] == 349


def test_deux_editions_simultanees_ne_s_ecrasent_pas(
    client: TestClient, device: dict[str, str], other_device: dict[str, str], group: str
) -> None:
    client.post("/v1/groups", json={"shareUrl": group}, headers=other_device)
    receipt = _new_receipt(client, device, group)

    # Les deux appareils partent de la même version.
    first, second = dict(receipt), dict(receipt)
    first["merchant"] = "Écrit en premier"
    second["merchant"] = "Écrit en second"

    assert client.put(f"/v1/receipts/{receipt['id']}", json=first, headers=device).status_code == 200

    conflict = client.put(f"/v1/receipts/{receipt['id']}", json=second, headers=other_device)
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["code"] == "version_conflict"
    # Le ticket courant est joint au refus : le client peut expliquer et reprendre.
    assert conflict.json()["detail"]["current"]["merchant"] == "Écrit en premier"


def test_le_total_du_ticket_suit_lignes_taxes_et_ajustements(
    client: TestClient, device: dict[str, str], group: str
) -> None:
    receipt = _new_receipt(client, device, group)
    receipt["lines"] = [
        {"id": "l1", "label": "A", "quantity": 1, "unitPriceCents": 1000, "totalCents": 1000}
    ]
    receipt["taxes"] = [{"id": "t1", "label": "TPS", "code": "TPS", "amountCents": 50}]
    receipt["adjustments"] = [
        {"id": "a1", "label": "Remise", "amountCents": -100, "mode": "proportional"}
    ]
    client.put(f"/v1/receipts/{receipt['id']}", json=receipt, headers=device)

    summary = client.get(f"/v1/groups/{group}/receipts", headers=device).json()[0]
    # Le pourboire n'entre pas dans ce total : il ne figure pas sur le ticket.
    assert summary["totalCents"] == 950


def test_un_ticket_est_partage_par_tout_le_groupe(
    client: TestClient, device: dict[str, str], other_device: dict[str, str], group: str
) -> None:
    receipt = _new_receipt(client, device, group)
    client.post("/v1/groups", json={"shareUrl": group}, headers=other_device)

    assert client.get(f"/v1/receipts/{receipt['id']}", headers=other_device).status_code == 200


def test_un_ticket_reste_invisible_hors_du_groupe(
    client: TestClient, device: dict[str, str], other_device: dict[str, str], group: str
) -> None:
    receipt = _new_receipt(client, device, group)
    assert client.get(f"/v1/receipts/{receipt['id']}", headers=other_device).status_code == 404


def test_supprimer_un_ticket_emporte_sa_photo(
    client: TestClient, device: dict[str, str], group: str, env: Any
) -> None:
    from app import config

    receipt = _new_receipt(client, device, group)
    client.post(
        f"/v1/receipts/{receipt['id']}/image",
        files={"file": ("ticket.jpg", b"\xff\xd8\xff-photo", "image/jpeg")},
        headers=device,
    )
    assert len(list(config.IMAGES_DIR.iterdir())) == 1

    client.delete(f"/v1/receipts/{receipt['id']}", headers=device)
    assert list(config.IMAGES_DIR.iterdir()) == []


def test_une_photo_remplacee_ne_laisse_pas_l_ancienne_derriere(
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


def test_un_format_d_image_non_supporte_est_refuse(
    client: TestClient, device: dict[str, str], group: str
) -> None:
    receipt = _new_receipt(client, device, group)
    response = client.post(
        f"/v1/receipts/{receipt['id']}/image",
        files={"file": ("ticket.pdf", b"%PDF-1.4", "application/pdf")},
        headers=device,
    )
    assert response.status_code == 415


def test_lire_sans_photo_le_dit_clairement(
    client: TestClient, device: dict[str, str], group: str
) -> None:
    receipt = _new_receipt(client, device, group)
    response = client.post(f"/v1/receipts/{receipt['id']}/scan", headers=device)
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "image_missing"


def test_sans_aucune_clef_gemini_le_message_invite_a_en_definir_une(
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
    assert "réglages" in response.json()["detail"]["reason"]


# ── Envoi vers Tricount ───────────────────────────────────────────────────────


def test_l_envoi_attribue_les_parts_par_uuid_de_membre(
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


def test_une_repartition_qui_ne_tombe_pas_juste_n_est_pas_envoyee(
    client: TestClient, device: dict[str, str], group: str, tricount: Any
) -> None:
    receipt = _new_receipt(client, device, group)
    response = client.post(
        f"/v1/receipts/{receipt['id']}/push",
        json={
            "totalCents": 3000,
            "payerMemberUuid": "m-lea",
            # Il manque un cent : mieux vaut ne rien envoyer.
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


def test_un_membre_disparu_du_tricount_arrete_l_envoi(
    client: TestClient, device: dict[str, str], group: str, tricount: Any
) -> None:
    receipt = _new_receipt(client, device, group)
    response = client.post(
        f"/v1/receipts/{receipt['id']}/push",
        json={
            "totalCents": 1000,
            "payerMemberUuid": "m-lea",
            "shares": [{"memberUuid": "m-fantome", "amountCents": 1000}],
        },
        headers=device,
    )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "member_gone"
    assert tricount.created == []


# ── Comptes optionnels ────────────────────────────────────────────────────────


def test_un_compte_retrouve_ses_groupes_depuis_un_autre_appareil(
    client: TestClient, device: dict[str, str], other_device: dict[str, str], group: str
) -> None:
    credentials = {"email": "Ziroles@example.com", "password": "un-mot-de-passe-solide"}
    assert client.post("/v1/accounts", json=credentials, headers=device).status_code == 201

    # Le groupe rejoint avant la création du compte doit avoir suivi.
    assert [g["id"] for g in client.get("/v1/groups", headers=device).json()] == [group]

    assert client.post("/v1/sessions", json=credentials, headers=other_device).status_code == 200
    assert [g["id"] for g in client.get("/v1/groups", headers=other_device).json()] == [group]


def test_un_mot_de_passe_faux_ne_rattache_rien(
    client: TestClient, device: dict[str, str], other_device: dict[str, str]
) -> None:
    client.post(
        "/v1/accounts",
        json={"email": "a@example.com", "password": "un-mot-de-passe-solide"},
        headers=device,
    )
    response = client.post(
        "/v1/sessions", json={"email": "a@example.com", "password": "pas-le-bon"}, headers=other_device
    )
    assert response.status_code == 401


def test_une_adresse_deja_prise_est_refusee(
    client: TestClient, device: dict[str, str], other_device: dict[str, str]
) -> None:
    credentials = {"email": "a@example.com", "password": "un-mot-de-passe-solide"}
    client.post("/v1/accounts", json=credentials, headers=device)
    assert client.post("/v1/accounts", json=credentials, headers=other_device).status_code == 409


def test_health_annonce_ce_que_l_instance_sait_faire(client: TestClient) -> None:
    body = client.get("/health").json()
    assert body["ok"] is True
    assert body["serverHasGeminiKey"] is False
    assert body["canStoreUserKeys"] is True


def test_un_code_introuvable_parle_du_lien_pas_du_service(
    client: TestClient, device: dict[str, str]
) -> None:
    """« tINCONNU » a la forme d'un code mais ne mène nulle part. Le message doit
    envoyer l'utilisateur vérifier son lien, pas le faire réessayer sans fin."""
    response = client.post("/v1/groups", json={"shareUrl": "tINCONNU"}, headers=device)

    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "group_not_found"
    assert "lien" in response.json()["detail"]["reason"]


# ── Entretien ─────────────────────────────────────────────────────────────────


def test_la_purge_efface_la_photo_mais_garde_le_ticket(
    client: TestClient, device: dict[str, str], group: str, env: Any
) -> None:
    from app import config, db, maintenance

    receipt = _new_receipt(client, device, group)
    client.post(
        f"/v1/receipts/{receipt['id']}/image",
        files={"file": ("t.jpg", b"photo", "image/jpeg")},
        headers=device,
    )
    # On vieillit la photo de six mois.
    db.execute("UPDATE image SET created_at = '2020-01-01T00:00:00+00:00'")

    assert maintenance.purge_old_images(days=90) == 1

    assert list(config.IMAGES_DIR.iterdir()) == []
    # Le ticket survit, et sait qu'il n'a plus de photo.
    body = client.get(f"/v1/receipts/{receipt['id']}", headers=device).json()
    assert body["imageId"] is None
    assert client.get(f"/v1/receipts/{receipt['id']}/image", headers=device).status_code == 404


def test_la_purge_epargne_les_photos_recentes(
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


def test_une_retention_nulle_desactive_la_purge(
    client: TestClient, device: dict[str, str], group: str
) -> None:
    """Ne jamais purger est un choix d'hébergeur, pas une valeur à corriger."""
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


def test_la_purge_ramasse_les_fichiers_orphelins(
    client: TestClient, device: dict[str, str], group: str, env: Any
) -> None:
    """Un envoi coupé entre le fichier et la base laisse un fichier que rien ne nomme."""
    from app import config, maintenance

    config.ensure_directories()
    (config.IMAGES_DIR / "orphelin.bin").write_bytes(b"perdu")

    assert maintenance.purge_orphan_files() == 1
    assert list(config.IMAGES_DIR.iterdir()) == []


def test_health_annonce_la_version_du_contrat(client: TestClient) -> None:
    body = client.get("/health").json()
    assert body["contractVersion"] == "1"
    assert body["imageRetentionDays"] == 90
