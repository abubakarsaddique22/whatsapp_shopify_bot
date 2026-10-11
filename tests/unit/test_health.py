from fastapi.testclient import TestClient

from app.api import health
from app.main import create_app


def test_healthz_ok_and_correlation_header():
    client = TestClient(create_app())
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}
    assert r.headers["X-Correlation-Id"]


def test_readyz_503_when_db_down(monkeypatch):
    async def down() -> bool:
        return False

    monkeypatch.setattr(health, "check_db", down)
    assert TestClient(create_app()).get("/readyz").status_code == 503


def test_readyz_200_when_db_up(monkeypatch):
    async def up() -> bool:
        return True

    monkeypatch.setattr(health, "check_db", up)
    assert TestClient(create_app()).get("/readyz").status_code == 200
