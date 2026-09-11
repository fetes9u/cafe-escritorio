from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app import db, logic
from app.main import app


@pytest.fixture
def relogio(monkeypatch):
    """Relógio injectável: relogio.set(ano, mes, dia, hora) fixa o 'agora' em UTC."""
    estado = {"agora": datetime(2026, 9, 11, 9, 0, tzinfo=timezone.utc)}

    class Relogio:
        def set(self, *args):
            estado["agora"] = datetime(*args, tzinfo=timezone.utc)

    monkeypatch.setattr(logic, "agora", lambda: estado["agora"])
    return Relogio()


@pytest.fixture
def cliente(tmp_path, relogio):
    db.init(str(tmp_path / "t.db"))
    with TestClient(app) as c:
        yield c


def regista(cliente, nome, pin="1234", cafes_dia=1):
    """Regista e devolve um cliente novo autenticado como essa pessoa."""
    c = TestClient(app)
    r = c.post("/api/registar", json={"nome": nome, "pin": pin, "cafes_dia": cafes_dia})
    assert r.status_code == 201, r.text
    return c
