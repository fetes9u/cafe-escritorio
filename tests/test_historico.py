from fastapi.testclient import TestClient

from app.main import app
from tests.conftest import regista


def test_agrupa_por_dia_mais_recente_primeiro(cliente, relogio):
    a = regista(cliente, "Ana")
    relogio.set(2026, 9, 10, 8, 45)
    a.post("/api/cafe")
    relogio.set(2026, 9, 10, 11, 10)
    a.post("/api/cafe")
    relogio.set(2026, 9, 15, 10, 2)
    a.post("/api/cafe")

    r = a.get("/api/historico", params={"mes": "2026-09"})
    assert r.status_code == 200
    dias = r.json()["dias"]
    assert [d["dia"] for d in dias] == ["2026-09-15", "2026-09-10"]
    assert dias[0]["n"] == 1
    assert dias[0]["horas"] == ["11:02"]
    assert dias[1]["n"] == 2
    assert dias[1]["horas"] == ["09:45", "12:10"]


def test_fronteira_da_meia_noite_local(cliente, relogio):
    a = regista(cliente, "Ana")
    relogio.set(2026, 9, 15, 23, 30)  # 00:30 de 16 de Setembro em Lisboa (verão)
    a.post("/api/cafe")

    r = a.get("/api/historico", params={"mes": "2026-09"})
    dias = r.json()["dias"]
    assert len(dias) == 1
    assert dias[0]["dia"] == "2026-09-16"
    assert dias[0]["horas"] == ["00:30"]


def test_mes_sem_cafes(cliente, relogio):
    a = regista(cliente, "Ana")
    r = a.get("/api/historico", params={"mes": "2026-01"})
    assert r.status_code == 200
    assert r.json() == {"mes": "2026-01", "hoje": "2026-09-11", "dias": []}


def test_mes_invalido_da_422_e_sem_sessao_da_401(cliente):
    a = regista(cliente, "Ana")
    assert a.get("/api/historico", params={"mes": "2026-9"}).status_code == 422
    assert TestClient(app).get("/api/historico").status_code == 401


def test_historico_de_a_nao_contem_cafes_de_b(cliente, relogio):
    a = regista(cliente, "Ana")
    b = regista(cliente, "Bea")
    relogio.set(2026, 9, 11, 9, 0)
    a.post("/api/cafe")
    a.post("/api/cafe")
    b.post("/api/cafe")

    hist_a = a.get("/api/historico", params={"mes": "2026-09"}).json()
    hist_b = b.get("/api/historico", params={"mes": "2026-09"}).json()
    assert sum(d["n"] for d in hist_a["dias"]) == 2
    assert sum(d["n"] for d in hist_b["dias"]) == 1


def test_mes_omitido_usa_o_mes_e_hoje_do_relogio(cliente, relogio):
    a = regista(cliente, "Ana")
    r = a.get("/api/historico")
    assert r.status_code == 200
    corpo = r.json()
    assert corpo["mes"] == "2026-09"
    assert corpo["hoje"] == "2026-09-11"
