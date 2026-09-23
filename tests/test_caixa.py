"""A caixa, o preço fixo e o histórico de alterações.

Como em test_saldos.py, os efeitos lêem-se de volta (/api/eu,
/api/escritorio, /api/transferencias, /api/historico-alteracoes), nunca só
pelo código de estado da escrita.
"""
from tests.conftest import regista
from tests.test_saldos import _enviados, push  # noqa: F401 (push é uma fixture)

SEM_RESPONSAVEL = {"detail": "Ainda não há ninguém responsável pela caixa. Escolhe nas Definições."}


def _ids(cliente) -> dict[str, int]:
    return {u["nome"]: u["id"] for u in cliente.get("/api/utilizadores").json()}


def _saldo(sessao) -> int:
    return sessao.get("/api/eu").json()["saldo_cent"]


def _caixa(sessao) -> dict:
    return sessao.get("/api/escritorio").json()["caixa"]


def _guarda(sessao, utilizador_id):
    r = sessao.put("/api/config", json={"caixa_responsavel_id": utilizador_id})
    assert r.status_code == 200, r.text


def _a_caixa(sessao, valor_cent: int):
    return sessao.post("/api/transferencias",
                       json={"recebedor_id": None, "para_caixa": True, "valor_cent": valor_cent})


def _da_caixa(sessao, recebedor_id: int, valor_cent: int):
    return sessao.post("/api/transferencias",
                       json={"recebedor_id": recebedor_id, "de_caixa": True, "valor_cent": valor_cent})


def _paga(sessao, recebedor_id: int, valor_cent: int):
    return sessao.post("/api/transferencias", json={"recebedor_id": recebedor_id, "valor_cent": valor_cent})


def _lista(sessao) -> dict[int, dict]:
    return {t["id"]: t for t in sessao.get("/api/transferencias").json()["transferencias"]}


def _alteracoes(sessao, entidade: str, entidade_id: int | None = None) -> list[dict]:
    params = {"entidade": entidade} | ({"id": entidade_id} if entidade_id is not None else {})
    r = sessao.get("/api/historico-alteracoes", params=params)
    assert r.status_code == 200, r.text
    return r.json()["alteracoes"]


def _confere_invariante(sessao) -> dict:
    """Σ saldos das pessoas + saldo(Caixa) = −Fundo, com saldo(Caixa) = −dinheiro."""
    e = sessao.get("/api/escritorio").json()
    caixa = e["caixa"]
    assert sum(p["saldo_cent"] for p in e["pessoas"]) - caixa["dinheiro_cent"] == -caixa["fundo_cent"], e
    return e


# ---------- o caso real ----------

def test_o_caso_real_da_oferta_ate_a_caixa_fechar_no_fundo(cliente):
    """Oferta de 77 cápsulas (Faria), 95 cafés a 0,25 €, a caixa de 41 que a
    Ana pagou do bolso a 9,84 €, e a Ana a guardar a caixa. Toda a gente
    paga à caixa o que deve; a Ana reembolsa-se da caixa."""
    cafes = {"Ana": 20, "Júnior": 25, "João": 17, "Luís": 12, "Faria": 11, "Diogo M.": 10}
    s = {nome: regista(cliente, nome) for nome in cafes}
    ids = _ids(cliente)
    ana = s["Ana"]

    assert s["Faria"].post("/api/compras", json={"capsulas": 77, "custo_cent": 0}).status_code == 201
    for nome, n in cafes.items():
        for _ in range(n):
            assert s[nome].post("/api/cafe").status_code == 201
    assert ana.post("/api/compras", json={"capsulas": 41, "custo_cent": 984}).status_code == 201
    _guarda(ana, ids["Ana"])
    assert _saldo(ana) == 984 - 20 * 25

    for nome, sessao in s.items():
        if nome != "Ana":
            deve = -_saldo(sessao)
            assert sessao.get("/api/eu").json()["sugestao"] == {
                "para_caixa": True, "nome": "Caixa (Ana)", "valor_cent": deve,
            }
            assert _a_caixa(sessao, deve).status_code == 201
    por_confirmar = ana.get("/api/eu").json()["por_confirmar"]
    assert len(por_confirmar) == 5 and all(t["para_caixa"] for t in por_confirmar)
    for t in por_confirmar:
        assert ana.post(f"/api/transferencias/{t['id']}/confirmar").json() == {"ok": True}

    eu = ana.get("/api/eu").json()
    assert eu["caixa"] == {"responsavel_id": ids["Ana"], "responsavel": "Ana", "dinheiro_cent": 1875}
    assert eu["saldo_cent"] == 484 and eu["sugestao"] is None and eu["por_confirmar"] == []
    _confere_invariante(ana)

    r = _da_caixa(ana, ids["Ana"], 484)
    assert r.status_code == 201
    assert r.json()["pagador_id"] is None and r.json()["de_caixa"] is True
    assert ana.get("/api/eu").json()["por_confirmar"] == []
    reembolso = _lista(ana)[r.json()["id"]]
    assert reembolso["confirmada_em"] is not None
    assert (reembolso["pagador"], reembolso["recebedor"]) == ("Caixa", "Ana")
    assert reembolso["pode_confirmar"] is False and reembolso["pode_anular"] is False

    e = _confere_invariante(ana)
    assert e["caixa"] == {"responsavel_id": ids["Ana"], "responsavel": "Ana", "dinheiro_cent": 1391,
                          "por_receber_cent": 0, "fundo_cent": 1391}
    assert {p["nome"]: p["saldo_cent"] for p in e["pessoas"]} == {nome: 0 for nome in cafes}

    # Se a Ana corrigir o custo para os 11,04 € que pagou e se reembolsar da
    # diferença, a caixa fica com 12,71 €.
    compra = next(c for c in e["compras"] if c["capsulas"] == 41)
    assert ana.patch(f"/api/compras/{compra['id']}", json={"custo_cent": 1104}).status_code == 200
    assert _da_caixa(ana, ids["Ana"], _saldo(ana)).status_code == 201
    assert _caixa(ana)["dinheiro_cent"] == 1271
    _confere_invariante(ana)


def test_invariante_depois_de_uma_sequencia_mista(cliente):
    a = regista(cliente, "Ana")
    b = regista(cliente, "Bea")
    r = regista(cliente, "Rui")
    ids = _ids(cliente)
    _guarda(a, ids["Ana"])

    b.post("/api/compras", json={"capsulas": 50, "custo_cent": 1250})                           # do bolso
    for _ in range(6):
        a.post("/api/cafe"); b.post("/api/cafe"); r.post("/api/cafe")
    _confere_invariante(a)
    r.post("/api/compras", json={"capsulas": 20, "custo_cent": 0})                              # oferta
    t_caixa = _a_caixa(r, 400).json()["id"]
    _confere_invariante(a)
    _a_caixa(b, 100)
    a.post("/api/compras", json={"capsulas": 30, "custo_cent": 700, "paga_pela_caixa": True})   # pela caixa
    t_pessoa = _paga(r, ids["Bea"], 200).json()["id"]
    _confere_invariante(a)
    _da_caixa(a, ids["Bea"], 500)                                                               # reembolso
    assert r.patch(f"/api/transferencias/{t_caixa}", json={"valor_cent": 450}).status_code == 200
    assert b.post(f"/api/transferencias/{t_pessoa}/anular").status_code == 200
    e = _confere_invariante(a)
    assert e["caixa"]["dinheiro_cent"] == 450 + 100 - 700 - 500
    assert e["caixa"]["fundo_cent"] == 18 * 25 - 1250 - 700
    assert {p["nome"]: p["saldo_cent"] for p in e["pessoas"]} == {
        "Ana": -150, "Bea": 1250 - 150 + 100 - 500, "Rui": -150 + 450,
    }
    assert e["caixa"]["por_receber_cent"] == 150


# ---------- sem responsável ----------

def test_sem_responsavel_a_caixa_responde_409_e_nao_ha_sugestao(cliente):
    a = regista(cliente, "Ana")
    ids = _ids(cliente)
    a.post("/api/cafe")
    eu = a.get("/api/eu").json()
    assert eu["sugestao"] is None and eu["caixa"] is None

    for r in (
        _a_caixa(a, 100),
        _da_caixa(a, ids["Ana"], 100),
        a.post("/api/compras", json={"capsulas": 10, "custo_cent": 300, "paga_pela_caixa": True}),
    ):
        assert r.status_code == 409 and r.json() == SEM_RESPONSAVEL
    # uma oferta nunca é paga pela caixa, portanto passa
    assert a.post("/api/compras", json={"capsulas": 10, "custo_cent": 0, "paga_pela_caixa": True}).status_code == 201
    assert a.get("/api/transferencias").json() == {"transferencias": []}
    assert _caixa(a) == {"responsavel_id": None, "responsavel": None, "dinheiro_cent": 0,
                         "por_receber_cent": 25, "fundo_cent": 25}

    t = _paga(a, regista(cliente, "Bea").get("/api/eu").json()["utilizador"]["id"], 100).json()["id"]
    r = a.patch(f"/api/transferencias/{t}", json={"para_caixa": True, "recebedor_id": None})
    assert r.status_code == 409 and r.json() == SEM_RESPONSAVEL
    compra = a.get("/api/escritorio").json()["compras"][0]["id"]
    a.patch(f"/api/compras/{compra}", json={"custo_cent": 300})
    r = a.patch(f"/api/compras/{compra}", json={"paga_pela_caixa": True})
    assert r.status_code == 409 and r.json() == SEM_RESPONSAVEL


# ---------- registar ----------

def test_validacao_das_combinacoes_com_a_caixa(cliente):
    a = regista(cliente, "Ana")
    regista(cliente, "Bea")
    ids = _ids(cliente)
    _guarda(a, ids["Ana"])
    corpo = {"valor_cent": 100}
    assert a.post("/api/transferencias", json=corpo | {"para_caixa": True, "de_caixa": True,
                                                        "recebedor_id": ids["Bea"]}).status_code == 400
    assert a.post("/api/transferencias", json=corpo | {"para_caixa": True, "recebedor_id": ids["Bea"]}).status_code == 400
    assert a.post("/api/transferencias", json=corpo | {"de_caixa": True}).status_code == 400
    assert a.post("/api/transferencias", json=corpo | {"de_caixa": True, "recebedor_id": 9999}).status_code == 404
    assert a.post("/api/transferencias", json={"para_caixa": True, "valor_cent": 0}).status_code == 422
    assert a.get("/api/transferencias").json() == {"transferencias": []}


def test_so_quem_guarda_a_caixa_regista_saidas(cliente):
    a = regista(cliente, "Ana")
    b = regista(cliente, "Bea")
    ids = _ids(cliente)
    _guarda(a, ids["Ana"])
    assert _da_caixa(b, ids["Bea"], 300).status_code == 403
    r = _da_caixa(a, ids["Bea"], 300)
    assert r.status_code == 201
    assert r.json() == {"id": r.json()["id"], "pagador_id": None, "recebedor_id": ids["Bea"], "para_caixa": False,
                        "de_caixa": True, "valor_cent": 300, "em": "2026-09-11T09:00:00+00:00"}
    assert (_saldo(b), _caixa(a)["dinheiro_cent"]) == (-300, -300)
    # a Bea confirma o que recebeu da caixa; aparece-lhe como vindo da caixa
    pendente = b.get("/api/eu").json()["por_confirmar"]
    assert pendente == [{"id": r.json()["id"], "pagador_id": None, "pagador": "Caixa", "para_caixa": False,
                         "valor_cent": 300, "em": "2026-09-11T09:00:00+00:00"}]
    assert a.post(f"/api/transferencias/{r.json()['id']}/confirmar").status_code == 403
    assert b.post(f"/api/transferencias/{r.json()['id']}/confirmar").status_code == 200
    m = b.get("/api/movimentos").json()["transferencias"][0]
    assert (m["sentido"], m["outro_id"], m["outro"], m["de_caixa"]) == ("recebi", None, "Caixa", True)


def test_quem_guarda_confirma_e_anula_pela_caixa(cliente):
    a = regista(cliente, "Ana")
    b = regista(cliente, "Bea")
    r = regista(cliente, "Rui")
    ids = _ids(cliente)
    _guarda(a, ids["Ana"])
    t1 = _a_caixa(b, 500).json()["id"]
    t2 = _a_caixa(b, 200).json()["id"]
    assert [t["id"] for t in a.get("/api/eu").json()["por_confirmar"]] == [t2, t1]
    assert b.get("/api/eu").json()["por_confirmar"] == []
    assert r.post(f"/api/transferencias/{t1}/confirmar").status_code == 403
    assert b.post(f"/api/transferencias/{t1}/confirmar").status_code == 403
    assert r.post(f"/api/transferencias/{t1}/anular").status_code == 403
    assert a.post(f"/api/transferencias/{t1}/confirmar").status_code == 200
    assert a.post(f"/api/transferencias/{t2}/anular").status_code == 200  # não recebi
    lista = _lista(a)
    assert lista[t1]["confirmada_em"] is not None and lista[t2]["anulada_por"] == ids["Ana"]
    assert (_saldo(b), _caixa(a)["dinheiro_cent"]) == (500, 500)
    m = b.get("/api/movimentos").json()["transferencias"]
    assert {(t["sentido"], t["outro_id"], t["outro"], t["para_caixa"]) for t in m} == {("paguei", None, "Caixa", True)}
    # os pagamentos à caixa são da caixa, não de quem a guarda
    assert a.get("/api/movimentos").json()["transferencias"] == []


def test_push_da_caixa_so_ao_lado_de_quem_recebe(cliente, push):
    a = regista(cliente, "Ana")
    b = regista(cliente, "Bea")
    r = regista(cliente, "Rui")
    ids = _ids(cliente)
    for sessao, nome in ((a, "ana"), (b, "bea"), (r, "rui")):
        sessao.post("/api/push/subscricoes", json={"endpoint": f"https://push.example/{nome}", "p256dh": "p", "auth": "a"})
    _guarda(a, ids["Ana"])

    push.reset_mock()
    t = _a_caixa(b, 500).json()["id"]
    assert _enviados(push) == [("https://push.example/ana", {
        "titulo": "Pagamento", "corpo": "Bea registou 5,00 € pagos à caixa", "url": "/", "evento": "pagamento",
    })]
    push.reset_mock()
    _da_caixa(a, ids["Rui"], 250)
    assert [(e, d["corpo"]) for e, d in _enviados(push)] == [
        ("https://push.example/rui", "Ana registou 2,50 € da caixa para ti"),
    ]
    push.reset_mock()
    a.post(f"/api/transferencias/{t}/anular")
    assert [(e, d["corpo"]) for e, d in _enviados(push)] == [
        ("https://push.example/bea", "Ana disse que não recebeu os 5,00 €"),
    ]


def test_quem_guarda_a_caixa_nos_dois_lados_confirma_sozinho(cliente, push):
    a = regista(cliente, "Ana")
    ids = _ids(cliente)
    a.post("/api/push/subscricoes", json={"endpoint": "https://push.example/ana", "p256dh": "p", "auth": "a"})
    _guarda(a, ids["Ana"])
    push.reset_mock()
    t1 = _a_caixa(a, 1000).json()["id"]           # põe dinheiro seu na caixa
    t2 = _da_caixa(a, ids["Ana"], 300).json()["id"]  # reembolsa-se
    push.assert_not_called()
    assert a.get("/api/eu").json()["por_confirmar"] == []
    lista = _lista(a)
    assert lista[t1]["confirmada_em"] == lista[t2]["confirmada_em"] == "2026-09-11T09:00:00+00:00"
    assert (_saldo(a), _caixa(a)["dinheiro_cent"]) == (700, 700)
    # auto-confirmar não é uma confirmação de ninguém: sem linhas de histórico
    assert _alteracoes(a, "transferencia", t1) == []


# ---------- editar ----------

def test_editar_so_o_lado_de_quem_paga_e_enquanto_activo(cliente):
    a = regista(cliente, "Ana")
    b = regista(cliente, "Bea")
    r = regista(cliente, "Rui")
    ids = _ids(cliente)
    _guarda(a, ids["Ana"])
    t = _a_caixa(b, 500).json()["id"]
    assert a.patch(f"/api/transferencias/{t}", json={"valor_cent": 600}).status_code == 403  # recebe, não paga
    assert r.patch(f"/api/transferencias/{t}", json={"valor_cent": 600}).status_code == 403
    assert b.patch("/api/transferencias/9999", json={"valor_cent": 600}).status_code == 404
    assert b.patch(f"/api/transferencias/{t}", json={"valor_cent": 500}).status_code == 400  # nada muda
    assert b.patch(f"/api/transferencias/{t}", json={}).status_code == 400
    assert b.patch(f"/api/transferencias/{t}", json={"valor_cent": 0}).status_code == 422
    assert b.patch(f"/api/transferencias/{t}", json={"valor_cent": None}).status_code == 422
    assert b.patch(f"/api/transferencias/{t}", json={"para_caixa": False}).status_code == 400  # sem destino
    assert b.patch(f"/api/transferencias/{t}", json={"recebedor_id": ids["Rui"]}).status_code == 400
    assert b.patch(f"/api/transferencias/{t}",
                   json={"para_caixa": False, "recebedor_id": ids["Bea"]}).status_code == 400  # a si própria
    assert b.patch(f"/api/transferencias/{t}", json={"para_caixa": False, "recebedor_id": 9999}).status_code == 404
    assert _alteracoes(b, "transferencia", t) == []
    assert _lista(b)[t]["pode_editar"] is True and _lista(a)[t]["pode_editar"] is False

    # a saída da caixa edita-a quem guarda a caixa
    s = _da_caixa(a, ids["Rui"], 200).json()["id"]
    assert r.patch(f"/api/transferencias/{s}", json={"valor_cent": 250}).status_code == 403
    assert a.patch(f"/api/transferencias/{s}", json={"para_caixa": True, "recebedor_id": None}).status_code == 400
    assert a.patch(f"/api/transferencias/{s}", json={"valor_cent": 250, "recebedor_id": ids["Bea"]}).status_code == 200
    assert (_saldo(r), _saldo(b), _caixa(a)["dinheiro_cent"]) == (0, 500 - 250, 500 - 250)

    assert b.post(f"/api/transferencias/{t}/anular").status_code == 200
    assert b.patch(f"/api/transferencias/{t}", json={"valor_cent": 600}).status_code == 409
    assert _lista(b)[t]["pode_editar"] is False


def test_editar_um_confirmado_volta_a_por_confirmar_com_historico(cliente, relogio, push):
    a = regista(cliente, "Ana")
    b = regista(cliente, "Bea")
    r = regista(cliente, "Rui")
    ids = _ids(cliente)
    for sessao, nome in ((a, "ana"), (b, "bea"), (r, "rui")):
        sessao.post("/api/push/subscricoes", json={"endpoint": f"https://push.example/{nome}", "p256dh": "p", "auth": "a"})
    _guarda(a, ids["Ana"])
    t = _a_caixa(b, 500).json()["id"]
    relogio.set(2026, 9, 11, 10, 0)
    a.post(f"/api/transferencias/{t}/confirmar")
    assert _lista(b)[t]["editada"] is False  # confirmar não é editar

    push.reset_mock()
    relogio.set(2026, 9, 11, 11, 0)
    assert b.patch(f"/api/transferencias/{t}", json={"valor_cent": 600}).json() == {"ok": True}
    assert [(e, d["corpo"]) for e, d in _enviados(push)] == [
        ("https://push.example/ana", "Bea alterou um pagamento: 5,00 € → 6,00 €"),
    ]
    linha = _lista(b)[t]
    assert linha["confirmada_em"] is None and linha["editada"] is True and linha["valor_cent"] == 600
    assert [p["id"] for p in a.get("/api/eu").json()["por_confirmar"]] == [t]
    assert (_saldo(b), _caixa(a)["dinheiro_cent"]) == (600, 600)

    # da caixa para o Rui: avisam-se o lado novo e o antigo
    push.reset_mock()
    relogio.set(2026, 9, 11, 12, 0)
    assert b.patch(f"/api/transferencias/{t}", json={"para_caixa": False, "recebedor_id": ids["Rui"]}).status_code == 200
    assert sorted((e, d["corpo"]) for e, d in _enviados(push)) == [
        ("https://push.example/ana", "Bea alterou um pagamento: 6,00 € → 6,00 €"),
        ("https://push.example/rui", "Bea alterou um pagamento: 6,00 € → 6,00 €"),
    ]
    assert (_saldo(r), _caixa(a)["dinheiro_cent"]) == (-600, 0)
    assert a.get("/api/eu").json()["por_confirmar"] == []

    assert _alteracoes(r, "transferencia", t) == [
        {"campo": "confirmada", "antes": "0", "depois": "1", "utilizador": "Ana", "em": "2026-09-11T10:00:00+00:00"},
        {"campo": "valor_cent", "antes": "500", "depois": "600", "utilizador": "Bea",
         "em": "2026-09-11T11:00:00+00:00"},
        {"campo": "confirmada", "antes": "1", "depois": "0", "utilizador": "Bea", "em": "2026-09-11T11:00:00+00:00"},
        {"campo": "recebedor_id", "antes": None, "depois": str(ids["Rui"]), "utilizador": "Bea",
         "em": "2026-09-11T12:00:00+00:00"},
        {"campo": "para_caixa", "antes": "1", "depois": "0", "utilizador": "Bea", "em": "2026-09-11T12:00:00+00:00"},
    ]
    m = b.get("/api/movimentos").json()["transferencias"][0]
    assert (m["outro"], m["editada"]) == ("Rui", True)


def test_anular_grava_historico_mas_nao_marca_editada(cliente):
    a = regista(cliente, "Ana")
    regista(cliente, "Bea")
    ids = _ids(cliente)
    t = _paga(a, ids["Bea"], 300).json()["id"]
    a.post(f"/api/transferencias/{t}/anular")
    assert [(h["campo"], h["antes"], h["depois"], h["utilizador"]) for h in _alteracoes(a, "transferencia", t)] == [
        ("anulada", "0", "1", "Ana"),
    ]
    assert _lista(a)[t]["editada"] is False


def test_lista_de_pagamentos_de_toda_a_gente(cliente, relogio):
    a = regista(cliente, "Ana")
    b = regista(cliente, "Bea")
    r = regista(cliente, "Rui")
    ids = _ids(cliente)
    _guarda(a, ids["Ana"])
    t1 = _paga(b, ids["Rui"], 100).json()["id"]
    relogio.set(2026, 9, 11, 10, 0)
    t2 = _a_caixa(b, 500).json()["id"]

    lista = r.get("/api/transferencias").json()["transferencias"]
    assert [t["id"] for t in lista] == [t2, t1]
    assert lista[0] == {
        "id": t2, "pagador_id": ids["Bea"], "pagador": "Bea", "recebedor_id": None, "recebedor": "Caixa",
        "para_caixa": True, "de_caixa": False, "valor_cent": 500, "em": "2026-09-11T10:00:00+00:00",
        "confirmada_em": None, "anulada_em": None, "anulada_por": None,
        "editada": False, "pode_editar": False, "pode_confirmar": False, "pode_anular": False,
    }
    assert {k: lista[1][k] for k in ("pode_editar", "pode_confirmar", "pode_anular")} == {
        "pode_editar": False, "pode_confirmar": True, "pode_anular": True,
    }
    de_ana = _lista(a)[t2]
    assert (de_ana["pode_editar"], de_ana["pode_confirmar"], de_ana["pode_anular"]) == (False, True, True)
    de_bea = _lista(b)[t2]
    assert (de_bea["pode_editar"], de_bea["pode_confirmar"], de_bea["pode_anular"]) == (True, False, True)


# ---------- compras ----------

def test_a_oferta_por_patch_poe_paga_pela_caixa_a_zero_com_historico(cliente):
    a = regista(cliente, "Ana")
    ids = _ids(cliente)
    _guarda(a, ids["Ana"])
    a.post("/api/compras", json={"capsulas": 77, "custo_cent": 1925})
    compra = a.get("/api/escritorio").json()["compras"][0]["id"]
    assert _saldo(a) == 1925
    assert a.patch(f"/api/compras/{compra}", json={"custo_cent": 0}).json() == {"ok": True}
    linha = a.get("/api/escritorio").json()["compras"][0]
    assert (linha["custo_cent"], linha["paga_pela_caixa"], linha["editada"]) == (0, False, True)
    assert [(h["campo"], h["antes"], h["depois"]) for h in _alteracoes(a, "compra", compra)] == [
        ("custo_cent", "1925", "0"),
    ]
    assert _saldo(a) == 0

    # pela caixa, e depois oferta: as duas colunas mudam e ficam no histórico
    a.post("/api/compras", json={"capsulas": 10, "custo_cent": 300, "paga_pela_caixa": True})
    outra = a.get("/api/escritorio").json()["compras"][0]
    assert outra["paga_pela_caixa"] is True and _caixa(a)["dinheiro_cent"] == -300
    a.patch(f"/api/compras/{outra['id']}", json={"custo_cent": 0, "paga_pela_caixa": True})
    assert [(h["campo"], h["antes"], h["depois"]) for h in _alteracoes(a, "compra", outra["id"])] == [
        ("custo_cent", "300", "0"), ("paga_pela_caixa", "1", "0"),
    ]
    assert _caixa(a)["dinheiro_cent"] == 0


def test_alterar_capsulas_e_quem_pagou_uma_compra(cliente):
    a = regista(cliente, "Ana")
    b = regista(cliente, "Bea")
    ids = _ids(cliente)
    _guarda(b, ids["Bea"])
    a.post("/api/compras", json={"capsulas": 10, "custo_cent": 250})
    compra = a.get("/api/escritorio").json()["compras"][0]["id"]
    for _ in range(4):
        a.post("/api/cafe")
    assert a.patch(f"/api/compras/{compra}", json={"capsulas": 3}).status_code == 409  # stock -1
    assert b.patch(f"/api/compras/{compra}", json={"capsulas": 12}).status_code == 403
    assert a.patch(f"/api/compras/{compra}", json={"capsulas": 4, "paga_pela_caixa": True}).status_code == 200
    e = a.get("/api/escritorio").json()
    assert e["stock"]["stock"] == 0 and e["compras"][0]["paga_pela_caixa"] is True
    assert (_saldo(a), e["caixa"]["dinheiro_cent"]) == (-100, -250)
    assert [(h["campo"], h["antes"], h["depois"], h["utilizador"]) for h in _alteracoes(b, "compra", compra)] == [
        ("capsulas", "10", "4", "Ana"), ("paga_pela_caixa", "0", "1", "Ana"),
    ]
    m = a.get("/api/movimentos").json()["compras"][0]
    assert (m["paga_pela_caixa"], m["editada"]) == (True, True)


def test_apagar_compra_fica_no_historico_e_o_proximo_id_nao_a_herda(cliente):
    """AUTOINCREMENT em compras.id: apagar a mais recente não deixa a
    seguinte herdar o id nem o histórico de quem a apagou."""
    a = regista(cliente, "Ana")
    a.post("/api/compras", json={"capsulas": 10, "custo_cent": 250})
    primeira = a.get("/api/escritorio").json()["compras"][0]["id"]
    a.patch(f"/api/compras/{primeira}", json={"custo_cent": 300})

    assert a.delete(f"/api/compras/{primeira}").json() == {"ok": True}

    a.post("/api/compras", json={"capsulas": 5, "custo_cent": 125})
    segunda = a.get("/api/escritorio").json()["compras"][0]
    assert segunda["id"] != primeira
    assert segunda["editada"] is False

    alteracoes = _alteracoes(a, "compra", primeira)
    assert [(h["campo"], h["antes"], h["depois"]) for h in alteracoes] == [
        ("custo_cent", "250", "300"),
        ("apagada", "0", "1"),
    ]
    assert alteracoes[-1]["utilizador"] == "Ana"


def test_com_o_stock_ja_negativo_corrigir_o_custo_passa(cliente):
    """Como em produção: cafés marcados antes de registada a caixa deixam o
    stock abaixo de zero, e a oferta corrige-se na app na mesma."""
    a = regista(cliente, "Ana")
    a.post("/api/compras", json={"capsulas": 2, "custo_cent": 1})
    for _ in range(3):
        a.post("/api/cafe")
    compra = a.get("/api/escritorio").json()["compras"][0]["id"]
    assert a.get("/api/eu").json()["stock"]["stock"] == -1
    assert a.patch(f"/api/compras/{compra}", json={"custo_cent": 0}).status_code == 200
    assert [(h["campo"], h["antes"], h["depois"]) for h in _alteracoes(a, "compra", compra)] == [
        ("custo_cent", "1", "0"),
    ]
    assert a.patch(f"/api/compras/{compra}", json={"capsulas": 3}).status_code == 200
    assert a.patch(f"/api/compras/{compra}", json={"capsulas": 2}).status_code == 409


# ---------- definições ----------

def test_definicoes_com_historico_por_chave(cliente, relogio):
    a = regista(cliente, "Ana")
    b = regista(cliente, "Bea")
    ids = _ids(cliente)
    assert a.get("/api/config").json() == {
        "preco_cent": 25, "stock_baixo": 16, "caixa_responsavel_id": None, "editada": False,
    }
    assert a.put("/api/config", json={"caixa_responsavel_id": 9999}).status_code == 404
    assert a.put("/api/config", json={"preco_cent": 25, "stock_baixo": 16}).status_code == 200  # nada muda
    assert _alteracoes(a, "config") == []

    a.put("/api/config", json={"caixa_responsavel_id": ids["Ana"], "stock_baixo": 16})
    relogio.set(2026, 9, 11, 10, 0)
    b.put("/api/config", json={"preco_cent": 30, "caixa_responsavel_id": None})
    assert b.get("/api/config").json() == {
        "preco_cent": 30, "stock_baixo": 16, "caixa_responsavel_id": None, "editada": True,
    }
    assert _alteracoes(b, "config") == [
        {"campo": "caixa_responsavel_id", "antes": None, "depois": str(ids["Ana"]), "utilizador": "Ana",
         "em": "2026-09-11T09:00:00+00:00"},
        {"campo": "preco_cent", "antes": "25", "depois": "30", "utilizador": "Bea", "em": "2026-09-11T10:00:00+00:00"},
        {"campo": "caixa_responsavel_id", "antes": str(ids["Ana"]), "depois": None, "utilizador": "Bea",
         "em": "2026-09-11T10:00:00+00:00"},
    ]
    assert b.get("/api/eu").json()["caixa"] is None


def test_historico_alteracoes_pede_entidade_e_id(cliente):
    a = regista(cliente, "Ana")
    assert a.get("/api/historico-alteracoes").status_code == 422
    assert a.get("/api/historico-alteracoes", params={"entidade": "pessoa", "id": 1}).status_code == 422
    assert a.get("/api/historico-alteracoes", params={"entidade": "compra"}).status_code == 422
    assert a.get("/api/historico-alteracoes", params={"entidade": "cafe", "id": 1}).json() == {"alteracoes": []}
