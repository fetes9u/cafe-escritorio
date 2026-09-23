"""Saldos corridos e pagamentos por MB WAY entre pessoas.

Os efeitos verificam-se sempre lendo de volta (/api/eu, /api/movimentos,
/api/escritorio), nunca só pelo código de estado da escrita. A caixa tem os
seus testes em tests/test_caixa.py.
"""
import json
from unittest.mock import MagicMock

import pytest

from app import db
from app import main as app_main
from tests.conftest import regista


@pytest.fixture
def push(monkeypatch):
    """VAPID configurado sem chaves reais; webpush() é um duplo de teste."""
    monkeypatch.setenv("CAFE_VAPID_PRIVATE", "chave-privada-de-teste")
    monkeypatch.setenv("CAFE_VAPID_PUBLIC", "chave-publica-de-teste")
    monkeypatch.setenv("CAFE_VAPID_CONTACTO", "mailto:teste@exemplo.pt")
    monkeypatch.setattr(app_main, "_vapid_instance", lambda: object())
    mock = MagicMock()
    monkeypatch.setattr(app_main, "webpush", mock)
    return mock


def _enviados(mock: MagicMock) -> list[tuple[str, dict]]:
    return [
        (c.kwargs["subscription_info"]["endpoint"], json.loads(c.kwargs["data"]))
        for c in mock.call_args_list
    ]


def _ids(cliente) -> dict[str, int]:
    return {u["nome"]: u["id"] for u in cliente.get("/api/utilizadores").json()}


def _saldo(sessao) -> int:
    return sessao.get("/api/eu").json()["saldo_cent"]


def _paga(sessao, recebedor_id: int, valor_cent: int):
    return sessao.post("/api/transferencias", json={"recebedor_id": recebedor_id, "valor_cent": valor_cent})


# ---------- saldo ----------

def test_exemplo_do_pedido_16_cafes_a_25_e_paga_5_euros(cliente):
    a = regista(cliente, "Ana")
    b = regista(cliente, "Bea")
    assert b.post("/api/compras", json={"capsulas": 100, "custo_cent": 2500}).status_code == 201
    for _ in range(16):
        assert a.post("/api/cafe").status_code == 201
    eu = a.get("/api/eu").json()
    assert eu["cafes"] == 16 and eu["valor_cent"] == 400 and eu["saldo_cent"] == -400

    r = _paga(a, _ids(cliente)["Bea"], 500)
    assert r.status_code == 201

    assert _saldo(a) == 100
    assert _saldo(b) == 2500 - 500


def test_pagamento_mexe_nos_dois_e_anular_devolve(cliente):
    a = regista(cliente, "Ana")
    b = regista(cliente, "Bea")
    ids = _ids(cliente)
    corpo = _paga(a, ids["Bea"], 700).json()
    assert corpo == {
        "id": corpo["id"], "pagador_id": ids["Ana"], "recebedor_id": ids["Bea"], "para_caixa": False,
        "de_caixa": False, "valor_cent": 700, "em": "2026-09-11T09:00:00+00:00",
    }
    assert (_saldo(a), _saldo(b)) == (700, -700)

    assert a.post(f"/api/transferencias/{corpo['id']}/anular").json() == {"ok": True}
    assert (_saldo(a), _saldo(b)) == (0, 0)
    t = a.get("/api/movimentos").json()["transferencias"][0]
    assert t["anulada_em"] == "2026-09-11T09:00:00+00:00" and t["anulada_por"] == ids["Ana"]


def test_nao_recebi_anula_pelo_recebedor(cliente):
    a = regista(cliente, "Ana")
    b = regista(cliente, "Bea")
    ids = _ids(cliente)
    tid = _paga(a, ids["Bea"], 300).json()["id"]
    assert b.post(f"/api/transferencias/{tid}/anular").status_code == 200
    assert (_saldo(a), _saldo(b)) == (0, 0)
    t = b.get("/api/movimentos").json()["transferencias"][0]
    assert t["sentido"] == "recebi" and t["anulada_por"] == ids["Bea"]
    # anulado não volta atrás
    assert b.post(f"/api/transferencias/{tid}/confirmar").status_code == 409
    assert a.post(f"/api/transferencias/{tid}/anular").status_code == 409


def test_confirmar_e_depois_anular_da_409(cliente, relogio):
    a = regista(cliente, "Ana")
    b = regista(cliente, "Bea")
    ids = _ids(cliente)
    tid = _paga(a, ids["Bea"], 500).json()["id"]
    assert b.get("/api/eu").json()["por_confirmar"][0]["id"] == tid

    relogio.set(2026, 9, 11, 10, 0)
    assert b.post(f"/api/transferencias/{tid}/confirmar").json() == {"ok": True}
    assert b.get("/api/eu").json()["por_confirmar"] == []
    assert b.get("/api/movimentos").json()["transferencias"][0]["confirmada_em"] == "2026-09-11T10:00:00+00:00"

    assert a.post(f"/api/transferencias/{tid}/anular").status_code == 409
    assert b.post(f"/api/transferencias/{tid}/anular").status_code == 409
    assert b.post(f"/api/transferencias/{tid}/confirmar").status_code == 409
    # confirmado continua a contar
    assert (_saldo(a), _saldo(b)) == (500, -500)


def test_terceiro_nao_confirma_nem_anula(cliente):
    a = regista(cliente, "Ana")
    regista(cliente, "Bea")
    r = regista(cliente, "Rui")
    ids = _ids(cliente)
    tid = _paga(a, ids["Bea"], 500).json()["id"]
    assert r.post(f"/api/transferencias/{tid}/anular").status_code == 403
    assert r.post(f"/api/transferencias/{tid}/confirmar").status_code == 403
    # quem pagou também não confirma: só quem recebeu
    assert a.post(f"/api/transferencias/{tid}/confirmar").status_code == 403
    assert a.get("/api/movimentos").json()["transferencias"][0]["anulada_em"] is None
    assert a.post("/api/transferencias/9999/anular").status_code == 404
    assert a.post("/api/transferencias/9999/confirmar").status_code == 404


def test_validacao_do_pagamento(cliente):
    a = regista(cliente, "Ana")
    regista(cliente, "Bea")
    ids = _ids(cliente)
    assert _paga(a, ids["Ana"], 500).status_code == 400
    assert a.post("/api/transferencias", json={"valor_cent": 500}).status_code == 400  # a quem?
    assert _paga(a, 9999, 500).status_code == 404
    assert _paga(a, ids["Bea"], 0).status_code == 422
    assert _paga(a, ids["Bea"], 100_001).status_code == 422
    assert a.get("/api/movimentos").json()["transferencias"] == []
    assert _paga(a, ids["Bea"], 100_000).status_code == 201
    assert _paga(a, ids["Bea"], 1).status_code == 201
    assert _saldo(a) == 100_001


def test_sem_responsavel_nao_ha_sugestao_nem_caixa(cliente):
    """A sugestão aponta só para a caixa: quem adiantou dinheiro não é
    sugerido, mesmo com saldo a favor."""
    a = regista(cliente, "Ana")
    b = regista(cliente, "Bea")
    b.post("/api/compras", json={"capsulas": 20, "custo_cent": 500})
    a.post("/api/cafe")
    eu = a.get("/api/eu").json()
    assert eu["saldo_cent"] == -25 and eu["sugestao"] is None and eu["caixa"] is None


# ---------- soma dos saldos ----------

def test_soma_dos_saldos_e_menos_o_fundo_so_entre_pessoas(cliente, relogio):
    """Sem responsável a caixa fica a zero, e a soma dos saldos é o que as
    compras custaram menos o que os cafés cobraram."""
    a = regista(cliente, "Ana")
    b = regista(cliente, "Bea")
    r = regista(cliente, "Rui")
    ids = _ids(cliente)

    def confere():
        e = a.get("/api/escritorio").json()
        caixa = e["caixa"]
        assert sum(p["saldo_cent"] for p in e["pessoas"]) - caixa["dinheiro_cent"] == -caixa["fundo_cent"], e
        return e

    a.post("/api/compras", json={"capsulas": 100, "custo_cent": 2500})
    for _ in range(7):
        a.post("/api/cafe"); b.post("/api/cafe"); r.post("/api/cafe")
    confere()
    b.post("/api/compras", json={"capsulas": 10, "custo_cent": 600})
    for _ in range(3):
        r.post("/api/cafe")
    t1 = _paga(r, ids["Ana"], 500).json()["id"]
    t2 = _paga(b, ids["Rui"], 120).json()["id"]
    t3 = _paga(b, ids["Ana"], 1000).json()["id"]
    confere()
    a.post(f"/api/transferencias/{t1}/confirmar")
    r.post(f"/api/transferencias/{t2}/anular")
    a.post(f"/api/transferencias/{t3}/anular")
    confere()
    cid = next(c["id"] for c in b.get("/api/escritorio").json()["compras"] if c["capsulas"] == 10)
    assert b.patch(f"/api/compras/{cid}", json={"custo_cent": 450}).status_code == 200
    for _ in range(2):
        a.post("/api/cafe")
    b.delete("/api/cafe/ultimo")
    e = confere()
    assert e["caixa"]["dinheiro_cent"] == 0
    assert e["caixa"]["fundo_cent"] == 25 * (21 + 3 + 2 - 1) - (2500 + 450)
    assert e["stock"]["stock"] == 110 - (21 + 3 + 2 - 1)


# ---------- notificações ----------

def test_push_de_pagamento_so_ao_recebedor_e_ao_anular_so_a_outra_parte(cliente, push):
    a = regista(cliente, "Ana")
    b = regista(cliente, "Bea")
    r = regista(cliente, "Rui")
    ids = _ids(cliente)
    for sessao, nome in ((a, "ana"), (b, "bea"), (r, "rui")):
        sessao.post("/api/push/subscricoes", json={"endpoint": f"https://push.example/{nome}", "p256dh": "p", "auth": "a"})
    push.reset_mock()

    t1 = _paga(a, ids["Bea"], 500).json()["id"]
    assert _enviados(push) == [("https://push.example/bea", {
        "titulo": "Pagamento", "corpo": "Ana registou 5,00 € pagos a ti", "url": "/", "evento": "pagamento",
    })]

    push.reset_mock()
    a.post(f"/api/transferencias/{t1}/anular")
    assert [(e, d["corpo"]) for e, d in _enviados(push)] == [
        ("https://push.example/bea", "Ana anulou o pagamento de 5,00 €"),
    ]

    t2 = _paga(a, ids["Bea"], 500).json()["id"]
    push.reset_mock()
    b.post(f"/api/transferencias/{t2}/anular")
    assert [(e, d["corpo"]) for e, d in _enviados(push)] == [
        ("https://push.example/ana", "Bea disse que não recebeu os 5,00 €"),
    ]

    t3 = _paga(a, ids["Bea"], 500).json()["id"]
    push.reset_mock()
    b.post(f"/api/transferencias/{t3}/confirmar")
    push.assert_not_called()


# ---------- compras e preço ----------

def test_compra_com_custo_zero_e_uma_oferta(cliente):
    a = regista(cliente, "Ana")
    assert a.post("/api/compras", json={"capsulas": 50}).status_code == 422
    assert a.post("/api/compras", json={"capsulas": 50, "custo_cent": -1}).status_code == 422
    assert a.post("/api/compras", json={"capsulas": 50, "custo_cent": 1_000_001}).status_code == 422
    assert a.get("/api/escritorio").json()["compras"] == []
    assert a.post("/api/compras", json={"capsulas": 50, "custo_cent": 1500}).status_code == 201
    assert a.post("/api/compras", json={"capsulas": 77, "custo_cent": 0}).status_code == 201
    compras = {c["capsulas"]: c for c in a.get("/api/escritorio").json()["compras"]}
    assert compras[50]["custo_cent"] == 1500 and compras[50]["custo_estimado"] is False
    assert compras[77]["custo_cent"] == 0 and compras[77]["paga_pela_caixa"] is False
    assert _saldo(a) == 1500


def test_corrigir_custo_so_por_quem_pode(cliente):
    a = regista(cliente, "Ana")
    b = regista(cliente, "Bea")
    a.post("/api/compras", json={"capsulas": 50, "custo_cent": 1500})
    with db.conn() as c:
        # uma compra convertida, de custo estimado, e uma sem autor
        c.execute("UPDATE compras SET custo_estimado = 1")
        c.execute(
            "INSERT INTO compras (utilizador_id, capsulas, custo_cent, custo_estimado, em) "
            "VALUES (NULL, 10, 250, 1, '2026-09-01T08:00:00+00:00')"
        )
    compras = {c["capsulas"]: c for c in b.get("/api/escritorio").json()["compras"]}
    assert compras[50]["pode_editar"] is False and compras[10]["pode_editar"] is True

    assert b.patch(f"/api/compras/{compras[50]['id']}", json={"custo_cent": 1400}).status_code == 403
    assert {c["capsulas"]: c for c in b.get("/api/escritorio").json()["compras"]}[50]["custo_cent"] == 1500

    assert a.patch(f"/api/compras/{compras[50]['id']}", json={"custo_cent": 1450}).json() == {"ok": True}
    assert b.patch(f"/api/compras/{compras[10]['id']}", json={"custo_cent": 300}).status_code == 200
    depois = {c["capsulas"]: c for c in b.get("/api/escritorio").json()["compras"]}
    assert depois[50]["custo_cent"] == 1450 and depois[50]["custo_estimado"] is False
    assert depois[10]["custo_cent"] == 300 and depois[10]["custo_estimado"] is False
    assert a.patch("/api/compras/9999", json={"custo_cent": 300}).status_code == 404
    assert a.patch(f"/api/compras/{compras[50]['id']}", json={"custo_cent": -1}).status_code == 422


def test_o_preco_medio_saiu(cliente):
    a = regista(cliente, "Ana")
    assert a.get("/api/preco/simular", params={"capsulas": 10, "custo_cent": 600}).status_code == 404


def test_preco_de_eu_e_o_que_o_cafe_seguinte_leva(cliente):
    """Sem compras e com compras, antes e depois de mudar o preço: o
    preco_cent de /api/eu é sempre exactamente o valor gravado no café
    seguinte."""
    a = regista(cliente, "Ana")
    for capsulas, custo, preco in ((0, 0, None), (7, 300, 31)):
        if capsulas:
            a.post("/api/compras", json={"capsulas": capsulas, "custo_cent": custo})
            a.put("/api/config", json={"preco_cent": preco})
        for _ in range(4):
            eu = a.get("/api/eu").json()
            a.post("/api/cafe")
            assert a.get("/api/eu").json()["valor_cent"] - eu["valor_cent"] == eu["preco_cent"]
    assert a.get("/api/eu").json()["valor_cent"] == 4 * 25 + 4 * 31


# ---------- o que /api/eu e /api/movimentos devolvem ----------

def test_eu_tem_os_campos_novos_e_nao_o_mes_anterior(cliente, relogio):
    a = regista(cliente, "Ana")
    b = regista(cliente, "Bea")
    r = regista(cliente, "Rui")
    ids = _ids(cliente)
    _paga(b, ids["Ana"], 500)
    relogio.set(2026, 9, 11, 10, 0)
    _paga(r, ids["Ana"], 200)
    t = _paga(r, ids["Ana"], 100).json()["id"]
    r.post(f"/api/transferencias/{t}/anular")
    _paga(a, ids["Bea"], 50)  # eu sou o pagador: não é para eu confirmar

    eu = a.get("/api/eu").json()
    assert "mes_anterior" not in eu
    assert eu["saldo_cent"] == -500 - 200 + 50
    assert eu["por_confirmar"] == [
        {"id": eu["por_confirmar"][0]["id"], "pagador_id": ids["Rui"], "pagador": "Rui", "para_caixa": False,
         "valor_cent": 200, "em": "2026-09-11T10:00:00+00:00"},
        {"id": eu["por_confirmar"][1]["id"], "pagador_id": ids["Bea"], "pagador": "Bea", "para_caixa": False,
         "valor_cent": 500, "em": "2026-09-11T09:00:00+00:00"},
    ]


def test_estimativa_soma_o_ja_bebido_ao_preco_corrente(cliente, relogio):
    relogio.set(2026, 9, 1, 9, 0)  # terça, 22 dias úteis no mês
    a = regista(cliente, "Ana", cafes_dia=2)
    a.post("/api/cafe")  # 25
    a.put("/api/config", json={"preco_cent": 43})
    eu = a.get("/api/eu").json()
    assert eu["preco_cent"] == 43 and eu["valor_cent"] == 25
    assert eu["estimativa_cafes"] == 1 + 2 * 21
    assert eu["estimativa_cent"] == 25 + 2 * 21 * 43


def test_movimentos(cliente, relogio):
    a = regista(cliente, "Ana")
    b = regista(cliente, "Bea")
    ids = _ids(cliente)
    relogio.set(2026, 8, 20, 9, 0)
    a.post("/api/compras", json={"capsulas": 50, "custo_cent": 1500})
    a.post("/api/cafe")
    relogio.set(2026, 9, 10, 9, 0)
    t1 = _paga(a, ids["Bea"], 500).json()["id"]
    a.post(f"/api/transferencias/{t1}/anular")
    relogio.set(2026, 9, 11, 9, 0)
    t2 = _paga(b, ids["Ana"], 200).json()["id"]
    a.post("/api/cafe"); a.post("/api/cafe")

    m = a.get("/api/movimentos").json()
    assert m["saldo_cent"] == _saldo(a) == 1500 - 3 * 25 - 200
    assert m["transferencias"] == [
        {"id": t2, "sentido": "recebi", "outro_id": ids["Bea"], "outro": "Bea", "valor_cent": 200,
         "em": "2026-09-11T09:00:00+00:00", "confirmada_em": None, "anulada_em": None, "anulada_por": None,
         "para_caixa": False, "de_caixa": False, "editada": False},
        {"id": t1, "sentido": "paguei", "outro_id": ids["Bea"], "outro": "Bea", "valor_cent": 500,
         "em": "2026-09-10T09:00:00+00:00", "confirmada_em": None,
         "anulada_em": "2026-09-10T09:00:00+00:00", "anulada_por": ids["Ana"],
         "para_caixa": False, "de_caixa": False, "editada": False},
    ]
    assert m["compras"] == [
        {"id": m["compras"][0]["id"], "capsulas": 50, "custo_cent": 1500, "custo_estimado": False,
         "paga_pela_caixa": False, "editada": False, "em": "2026-08-20T09:00:00+00:00"},
    ]
    assert m["meses"] == [
        {"mes": "2026-09", "cafes": 2, "valor_cent": 50},
        {"mes": "2026-08", "cafes": 1, "valor_cent": 25},
    ]
    assert b.get("/api/movimentos").json()["compras"] == []
