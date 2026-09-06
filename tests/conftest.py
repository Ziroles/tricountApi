"""
Montage des tests d'API.

Deux tiers sont simulés, et pour deux raisons distinctes : Tricount parce qu'il
n'existe pas d'environnement de test et qu'on ne va pas créer de vraies dépenses
pour vérifier une route ; Gemini parce qu'une suite de tests ne doit ni coûter
d'argent ni dépendre du réseau.

La base et le dossier d'images sont recréés à chaque test : l'isolation prime
sur la vitesse, et à cette échelle elle ne coûte rien.
"""

from __future__ import annotations

import importlib
from pathlib import Path
from typing import Any, Iterator

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    monkeypatch.setenv("SPLITTICKET_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SPLITTICKET_DB_PATH", str(tmp_path / "test.sqlite3"))
    monkeypatch.setenv("SPLITTICKET_IMAGES_DIR", str(tmp_path / "images"))
    monkeypatch.setenv("SPLITTICKET_SECRET_KEY", "clef-de-test-pour-le-chiffrement")
    monkeypatch.setenv("SPLITTICKET_ALLOWED_ORIGINS", "*")
    monkeypatch.delenv("SPLITTICKET_SIGNUP_KEY", raising=False)
    monkeypatch.delenv("TRICOUNT_RELAY_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)

    from app import config, db

    importlib.reload(config)
    db.reset_connection()
    # `db` et les routes ont capturé `config` à l'import : on les recharge pour
    # qu'ils voient les chemins du test, pas ceux du module chargé en premier.
    importlib.reload(db)
    from app import auth, crypto, tricount_client
    from app.routes import groups, identity, receipts

    for module in (crypto, auth, tricount_client, identity, groups, receipts):
        importlib.reload(module)
    from app import main

    importlib.reload(main)
    yield main
    db.reset_connection()


class FakeMember:
    def __init__(self, uuid: str, name: str, status: str = "ACTIVE") -> None:
        self.uuid = uuid
        self.display_name = name
        self.status = status
        self.id = abs(hash(uuid)) % 100000


class FakeTricount:
    def __init__(self) -> None:
        self.uuid = "tricount-uuid-1"
        self.title = "Colocation"
        self.currency = "CAD"
        self.members = [
            FakeMember("m-lea", "Léa"),
            FakeMember("m-mathieu", "Mathieu"),
            FakeMember("m-parti", "Ancien", status="DELETED"),
        ]


@pytest.fixture
def tricount(env: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    """Remplace le client Tricount et retient les dépenses créées."""
    from app import tricount_client

    created: list[dict[str, Any]] = []

    class FakeClient:
        def get_tricount(self, code: str) -> FakeTricount:
            if code == "tINCONNU":
                raise RuntimeError("introuvable")
            return FakeTricount()

        def join_tricount(self, code: str, fetch_full: bool = True) -> FakeTricount:
            return self.get_tricount(code)

        def create_transaction_custom_split(self, **kwargs: Any) -> int:
            created.append(kwargs)
            return 987654

    monkeypatch.setattr(tricount_client, "get_client", FakeClient)
    tricount_client.created = created  # type: ignore[attr-defined]
    return tricount_client


@pytest.fixture
def client(env: Any, tricount: Any) -> Iterator[TestClient]:
    with TestClient(env.app) as test_client:
        yield test_client


@pytest.fixture
def device(client: TestClient) -> dict[str, str]:
    """Un appareil enrôlé, avec l'en-tête d'autorisation prêt à l'emploi."""
    response = client.post("/v1/devices")
    assert response.status_code == 201, response.text
    token = response.json()["token"]
    return {"authorization": f"Bearer {token}"}


@pytest.fixture
def other_device(client: TestClient) -> dict[str, str]:
    """Un second appareil, pour vérifier qu'il ne voit rien de ce qui ne le regarde pas."""
    response = client.post("/v1/devices")
    return {"authorization": f"Bearer {response.json()['token']}"}


@pytest.fixture
def group(client: TestClient, device: dict[str, str]) -> str:
    response = client.post(
        "/v1/groups", json={"shareUrl": "https://tricount.com/tABC123456"}, headers=device
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]
