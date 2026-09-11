"""Tests for push subscriptions, notification preferences and event dispatch.

Real cryptography and real push services are never touched here: webpush()
and the VAPID key construction are mocked, as the spec for this suite
requires, so a test failure always points at our dispatch logic, not at
network flakiness or key material.
"""
import json
from unittest.mock import MagicMock

import pytest
from pywebpush import WebPushException

from app import db
from app import main as app_main
from tests.conftest import regista


@pytest.fixture
def push_configurado(monkeypatch):
    """Simulates a fully configured VAPID deployment without ever generating
    or parsing real key material: the instance builder and webpush() itself
    are replaced with test doubles."""
    monkeypatch.setenv("CAFE_VAPID_PRIVATE", "chave-privada-de-teste")
    monkeypatch.setenv("CAFE_VAPID_PUBLIC", "chave-publica-de-teste")
    monkeypatch.setenv("CAFE_VAPID_CONTACTO", "mailto:teste@exemplo.pt")
    monkeypatch.setattr(app_main, "_vapid_instance", lambda: object())
    mock = MagicMock()
    monkeypatch.setattr(app_main, "webpush", mock)
    return mock


def _endpoints_avisados(mock: MagicMock) -> set[str]:
    return {c.kwargs["subscription_info"]["endpoint"] for c in mock.call_args_list}


# ---------- arranque sem VAPID ----------

def test_arranca_e_serve_pedidos_sem_variaveis_vapid(cliente, monkeypatch):
    monkeypatch.delenv("CAFE_VAPID_PRIVATE", raising=False)
    monkeypatch.delenv("CAFE_VAPID_PUBLIC", raising=False)
    monkeypatch.delenv("CAFE_VAPID_CONTACTO", raising=False)
    a = regista(cliente, "Ana")
    assert a.post("/api/cafe").status_code == 201
    assert cliente.get("/api/push/chave").status_code == 503


# ---------- chave pública ----------

def test_chave_sem_vapid_devolve_503(cliente, monkeypatch):
    monkeypatch.delenv("CAFE_VAPID_PUBLIC", raising=False)
    assert cliente.get("/api/push/chave").status_code == 503


def test_chave_com_vapid_devolve_a_chave_publica(cliente, monkeypatch):
    monkeypatch.setenv("CAFE_VAPID_PUBLIC", "chave-publica-de-teste")
    r = cliente.get("/api/push/chave")
    assert r.status_code == 200
    assert r.json()["chave_publica"] == "chave-publica-de-teste"


# ---------- subscrições ----------

def test_subscrever_duas_vezes_o_mesmo_endpoint_nao_duplica(cliente):
    a = regista(cliente, "Ana")
    corpo = {"endpoint": "https://push.example/dev1", "p256dh": "p1", "auth": "a1"}
    assert a.post("/api/push/subscricoes", json=corpo).status_code == 201
    assert a.post("/api/push/subscricoes", json=corpo).status_code == 201
    with db.conn() as c:
        n = c.execute("SELECT COUNT(*) AS n FROM subscricoes").fetchone()["n"]
    assert n == 1


def test_apagar_subscricao_remove_a_linha(cliente):
    a = regista(cliente, "Ana")
    corpo = {"endpoint": "https://push.example/dev1", "p256dh": "p1", "auth": "a1"}
    a.post("/api/push/subscricoes", json=corpo)
    r = a.request("DELETE", "/api/push/subscricoes", json={"endpoint": corpo["endpoint"]})
    assert r.status_code == 200
    with db.conn() as c:
        n = c.execute("SELECT COUNT(*) AS n FROM subscricoes").fetchone()["n"]
    assert n == 0


def test_um_410_do_push_service_apaga_a_subscricao(cliente, push_configurado):
    a = regista(cliente, "Ana")
    r = regista(cliente, "Rui")
    corpo = {"endpoint": "https://push.example/rui", "p256dh": "p", "auth": "a"}
    r.post("/api/push/subscricoes", json=corpo)
    resposta_falsa = MagicMock(status_code=410)
    push_configurado.side_effect = WebPushException("gone", response=resposta_falsa)
    assert a.post("/api/cafe").status_code == 201
    with db.conn() as c:
        n = c.execute(
            "SELECT COUNT(*) AS n FROM subscricoes WHERE endpoint = ?", (corpo["endpoint"],)
        ).fetchone()["n"]
    assert n == 0


def test_um_404_do_push_service_apaga_a_subscricao(cliente, push_configurado):
    a = regista(cliente, "Ana")
    r = regista(cliente, "Rui")
    corpo = {"endpoint": "https://push.example/rui", "p256dh": "p", "auth": "a"}
    r.post("/api/push/subscricoes", json=corpo)
    resposta_falsa = MagicMock(status_code=404)
    push_configurado.side_effect = WebPushException("not found", response=resposta_falsa)
    assert a.post("/api/cafe").status_code == 201
    with db.conn() as c:
        n = c.execute(
            "SELECT COUNT(*) AS n FROM subscricoes WHERE endpoint = ?", (corpo["endpoint"],)
        ).fetchone()["n"]
    assert n == 0


# ---------- preferências ----------

def test_preferencias_por_omissao_tudo_ligado_e_pode_desligar(cliente):
    a = regista(cliente, "Ana")
    assert a.get("/api/notificacoes/preferencias").json() == {
        "cafe": True, "compra": True, "pagamento": True, "stock_baixo": True, "registo": True,
    }
    r = a.put("/api/notificacoes/preferencias", json={"cafe": False, "registo": False})
    assert r.status_code == 200
    assert a.get("/api/notificacoes/preferencias").json() == {
        "cafe": False, "compra": True, "pagamento": True, "stock_baixo": True, "registo": False,
    }


def test_evento_desconhecido_nas_preferencias_e_rejeitado(cliente):
    a = regista(cliente, "Ana")
    assert a.put("/api/notificacoes/preferencias", json={"nao_existe": False}).status_code == 400


# ---------- regras de destinatários ----------

def test_quem_desligou_um_evento_nao_e_destinatario_desse_evento(cliente, push_configurado):
    a = regista(cliente, "Ana")
    r = regista(cliente, "Rui")
    m = regista(cliente, "Marta")
    r.post("/api/push/subscricoes", json={"endpoint": "https://push.example/rui", "p256dh": "p", "auth": "a"})
    m.post("/api/push/subscricoes", json={"endpoint": "https://push.example/marta", "p256dh": "p", "auth": "a"})
    r.put("/api/notificacoes/preferencias", json={"cafe": False})

    assert a.post("/api/cafe").status_code == 201

    assert _endpoints_avisados(push_configurado) == {"https://push.example/marta"}


def test_autor_do_acto_nunca_e_destinatario_do_proprio_acto(cliente, push_configurado):
    a = regista(cliente, "Ana")
    a.post("/api/push/subscricoes", json={"endpoint": "https://push.example/ana", "p256dh": "p", "auth": "a"})

    assert a.post("/api/cafe").status_code == 201

    push_configurado.assert_not_called()


def test_compra_notifica_outros_utilizadores(cliente, push_configurado):
    a = regista(cliente, "Ana")
    r = regista(cliente, "Rui")
    r.post("/api/push/subscricoes", json={"endpoint": "https://push.example/rui", "p256dh": "p", "auth": "a"})

    assert a.post("/api/compras", json={"capsulas": 20}).status_code == 201

    push_configurado.assert_called_once()
    assert _endpoints_avisados(push_configurado) == {"https://push.example/rui"}


def test_registo_notifica_utilizadores_existentes(cliente, push_configurado):
    a = regista(cliente, "Ana")
    a.post("/api/push/subscricoes", json={"endpoint": "https://push.example/ana", "p256dh": "p", "auth": "a"})

    assert regista(cliente, "Rui")

    push_configurado.assert_called_once()
    assert _endpoints_avisados(push_configurado) == {"https://push.example/ana"}


def test_pagamento_notifica_o_pagador(cliente, push_configurado, relogio):
    a = regista(cliente, "Ana")
    r = regista(cliente, "Rui")
    rui_id = next(x["id"] for x in cliente.get("/api/utilizadores").json() if x["nome"] == "Rui")
    r.post("/api/push/subscricoes", json={"endpoint": "https://push.example/rui", "p256dh": "p", "auth": "a"})
    assert r.post("/api/cafe").status_code == 201

    relogio.set(2026, 10, 1, 9, 0)
    assert a.post("/api/pagamentos", json={"mes": "2026-09", "pagador_id": rui_id}).status_code == 201

    push_configurado.assert_called_once()
    assert _endpoints_avisados(push_configurado) == {"https://push.example/rui"}
    mensagem = json.loads(push_configurado.call_args.kwargs["data"])["mensagem"]
    assert "Rui" in mensagem and "Ana" in mensagem


# ---------- stock_baixo ----------

def test_stock_baixo_dispara_na_transicao_e_nao_se_repete_ate_repor(cliente, push_configurado):
    a = regista(cliente, "Ana")
    r = regista(cliente, "Rui")
    r.post("/api/push/subscricoes", json={"endpoint": "https://push.example/rui", "p256dh": "p", "auth": "a"})
    r.put("/api/notificacoes/preferencias", json={"cafe": False})

    assert a.post("/api/compras", json={"capsulas": 6}).status_code == 201
    assert a.put("/api/config", json={"stock_baixo": 3}).status_code == 200
    push_configurado.reset_mock()  # ignore the "compra" notification from the setup above

    for _ in range(6):
        assert a.post("/api/cafe").status_code == 201

    mensagens = [json.loads(c.kwargs["data"])["mensagem"] for c in push_configurado.call_args_list]
    # stock: 6,5,4,3,2,1,0 -- cruza o limiar (3) ao 3o cafe, chega a zero ao 6o.
    assert len(mensagens) == 2
    assert "3" in mensagens[0]
    assert "0" in mensagens[1]


def test_stock_baixo_nao_dispara_enquanto_se_mantem_acima_do_limiar(cliente, push_configurado):
    a = regista(cliente, "Ana")
    r = regista(cliente, "Rui")
    r.post("/api/push/subscricoes", json={"endpoint": "https://push.example/rui", "p256dh": "p", "auth": "a"})
    r.put("/api/notificacoes/preferencias", json={"cafe": False})

    assert a.post("/api/compras", json={"capsulas": 20}).status_code == 201
    assert a.put("/api/config", json={"stock_baixo": 3}).status_code == 200
    push_configurado.reset_mock()  # ignore the "compra" notification from the setup above

    for _ in range(5):
        assert a.post("/api/cafe").status_code == 201

    push_configurado.assert_not_called()
