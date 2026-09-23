from app import db, logic
from app import main as app_main
from tests.conftest import regista


# ---------- autenticação ----------

def test_registo_e_lista(cliente):
    a = regista(cliente, "Ana")
    assert [u["nome"] for u in cliente.get("/api/utilizadores").json()] == ["Ana"]
    assert a.get("/api/eu").json()["utilizador"]["nome"] == "Ana"


def test_nome_duplicado_sem_distinguir_maiusculas(cliente):
    regista(cliente, "Ana")
    r = cliente.post("/api/registar", json={"nome": "ana", "pin": "0000"})
    assert r.status_code == 409
    assert "apelido" in r.json()["detail"]


def test_pin_tem_de_ter_4_digitos(cliente):
    for pin in ("123", "12345", "12a4"):
        assert cliente.post("/api/registar", json={"nome": "X" + pin, "pin": pin}).status_code == 400


def test_login_certo_e_errado(cliente):
    regista(cliente, "Ana", "4321")
    uid = cliente.get("/api/utilizadores").json()[0]["id"]
    assert cliente.post("/api/login", json={"utilizador_id": uid, "pin": "0000"}).status_code == 401
    assert cliente.get("/api/eu").status_code == 401
    assert cliente.post("/api/login", json={"utilizador_id": uid, "pin": "4321"}).status_code == 200
    assert cliente.get("/api/eu").status_code == 200


def test_bloqueio_apos_5_tentativas(cliente, relogio):
    regista(cliente, "Ana", "4321")
    uid = cliente.get("/api/utilizadores").json()[0]["id"]
    for _ in range(5):
        cliente.post("/api/login", json={"utilizador_id": uid, "pin": "0000"})
    # mesmo com o PIN certo, está bloqueado
    assert cliente.post("/api/login", json={"utilizador_id": uid, "pin": "4321"}).status_code == 429
    relogio.set(2026, 9, 11, 9, 2)
    assert cliente.post("/api/login", json={"utilizador_id": uid, "pin": "4321"}).status_code == 200


def test_logout(cliente):
    a = regista(cliente, "Ana")
    a.post("/api/logout")
    assert a.get("/api/eu").status_code == 401


def test_mudar_pin(cliente):
    a = regista(cliente, "Ana", "1111")
    assert a.put("/api/eu/pin", json={"pin_actual": "9999", "pin_novo": "2222"}).status_code == 401
    assert a.put("/api/eu/pin", json={"pin_actual": "1111", "pin_novo": "2222"}).status_code == 200
    uid = cliente.get("/api/utilizadores").json()[0]["id"]
    assert cliente.post("/api/login", json={"utilizador_id": uid, "pin": "2222"}).status_code == 200


def test_cabecalhos_de_seguranca(cliente):
    r = cliente.get("/api/utilizadores")
    assert r.headers["x-frame-options"] == "DENY"
    assert r.headers["x-content-type-options"] == "nosniff"
    assert "default-src 'self'" in r.headers["content-security-policy"]


def test_cookie_secure_atras_de_https(cliente):
    r = cliente.post("/api/registar", json={"nome": "Ana", "pin": "1234"},
                     headers={"x-forwarded-proto": "https"})
    assert "Secure" in r.headers["set-cookie"]
    r = cliente.post("/api/registar", json={"nome": "Bea", "pin": "1234"})
    assert "Secure" not in r.headers["set-cookie"]


# ---------- cafés e stock ----------

def test_marcar_e_apagar_cafe(cliente):
    a = regista(cliente, "Ana")
    a.post("/api/compras", json={"capsulas": 50, "custo_cent": 1250})
    a.post("/api/cafe"); a.post("/api/cafe")
    eu = a.get("/api/eu").json()
    assert eu["cafes"] == 2 and eu["valor_cent"] == 50 and eu["stock"]["stock"] == 48
    assert a.delete("/api/cafe/ultimo").status_code == 200
    assert a.get("/api/eu").json()["cafes"] == 1
    a.delete("/api/cafe/ultimo")
    assert a.delete("/api/cafe/ultimo").status_code == 404


def test_cafe_grava_mes_em_hora_local(cliente, relogio):
    a = regista(cliente, "Ana")
    relogio.set(2026, 8, 31, 23, 30)  # 00:30 de 1 de Setembro em Lisboa
    a.post("/api/cafe")
    relogio.set(2026, 9, 11, 9, 0)
    assert a.get("/api/eu").json()["cafes"] == 1


def test_estimativa_usa_previsao_declarada_no_inicio(cliente, relogio):
    relogio.set(2026, 9, 1, 9, 0)  # terça, 22 dias úteis no mês
    a = regista(cliente, "Ana", cafes_dia=2)
    a.post("/api/cafe")
    eu = a.get("/api/eu").json()
    assert eu["estimativa_cafes"] == 1 + 2 * 21
    assert eu["estimativa_cent"] == eu["estimativa_cafes"] * 25


def test_stock_baixo_e_data_em_que_acaba(cliente, relogio):
    relogio.set(2026, 9, 11, 9, 0)  # sexta
    a = regista(cliente, "Ana", cafes_dia=2)
    a.post("/api/compras", json={"capsulas": 10, "custo_cent": 300})
    s = a.get("/api/eu").json()["stock"]
    assert s["baixo"] is True and s["limiar"] == 16
    assert s["acaba_em"] == "2026-09-18" and s["chega_ao_fim_do_mes"] is False
    a.post("/api/compras", json={"capsulas": 100, "custo_cent": 2500})
    s = a.get("/api/eu").json()["stock"]
    assert s["baixo"] is False and s["chega_ao_fim_do_mes"] is True


def test_apagar_compra_so_quem_registou_e_sem_stock_negativo(cliente):
    a = regista(cliente, "Ana"); b = regista(cliente, "Bea")
    a.post("/api/compras", json={"capsulas": 2, "custo_cent": 50})
    cid = a.get("/api/escritorio").json()["compras"][0]["id"]
    assert b.delete(f"/api/compras/{cid}").status_code == 403
    a.post("/api/cafe")
    assert a.delete(f"/api/compras/{cid}").status_code == 409
    a.delete("/api/cafe/ultimo")
    assert a.delete(f"/api/compras/{cid}").status_code == 200
    assert a.get("/api/eu").json()["stock"]["stock"] == 0


def test_config_muda_o_limiar_e_ignora_o_preco(cliente):
    """O preço deixou de se editar: um formulário antigo em cache ainda manda
    os dois campos, e o limiar tem de ficar gravado na mesma."""
    a = regista(cliente, "Ana")
    assert a.put("/api/config", json={"preco_cent": 30, "stock_baixo": 5}).status_code == 200
    with db.conn() as c:
        assert db.get_config(c, "preco_cent") == "25"
    a.post("/api/cafe")
    eu = a.get("/api/eu").json()
    assert eu["preco_cent"] == 25 and eu["valor_cent"] == 25 and eu["stock"]["limiar"] == 5


# ---------- escritório ----------

def _mes_com_cafes(cliente, relogio):
    """Agosto: Ana 3 cafés, Bea 1. Setembro: Ana 1. Devolve (ana, bea)."""
    relogio.set(2026, 8, 10, 9, 0)
    a = regista(cliente, "Ana"); b = regista(cliente, "Bea")
    a.post("/api/compras", json={"capsulas": 50, "custo_cent": 1250})
    for _ in range(3):
        a.post("/api/cafe")
    b.post("/api/cafe")
    relogio.set(2026, 9, 11, 9, 0)
    a.post("/api/cafe")
    return a, b


def test_escritorio_por_mes(cliente, relogio):
    a, _ = _mes_com_cafes(cliente, relogio)
    e = a.get("/api/escritorio").json()
    assert e["mes"] == "2026-09" and e["total_cafes"] == 1 and e["meses"] == ["2026-09", "2026-08"]
    e = a.get("/api/escritorio", params={"mes": "2026-08"}).json()
    por_nome = {p["nome"]: p for p in e["pessoas"]}
    assert por_nome["Ana"]["cafes"] == 3 and por_nome["Ana"]["valor_cent"] == 75
    assert por_nome["Bea"]["cafes"] == 1 and e["total_cent"] == 100
    assert e["stock"]["stock"] == 45


def test_rotas_antigas_de_pagamentos_respondem_410(cliente, relogio):
    a, b = _mes_com_cafes(cliente, relogio)
    ids = {u["nome"]: u["id"] for u in cliente.get("/api/utilizadores").json()}
    detalhe = {"detail": "Os pagamentos mensais acabaram. Actualiza a app."}
    # o corpo que um cliente antigo manda não pode transformar o 410 num 422
    r = b.post("/api/pagamentos", json={"mes": "2026-08", "pagador_id": ids["Ana"]})
    assert r.status_code == 410 and r.json() == detalhe
    r = b.delete(f"/api/pagamentos/2026-08/{ids['Ana']}")
    assert r.status_code == 410 and r.json() == detalhe
    with db.conn() as c:
        assert c.execute("SELECT COUNT(*) AS n FROM transferencias").fetchone()["n"] == 0


def test_desfazer_cafe_num_mes_com_pagamentos_ja_nao_da_409(cliente, relogio):
    a, b = _mes_com_cafes(cliente, relogio)
    ids = {u["nome"]: u["id"] for u in cliente.get("/api/utilizadores").json()}
    assert b.post("/api/transferencias", json={"recebedor_id": ids["Ana"], "valor_cent": 25}).status_code == 201
    # o último café da Bea é de Agosto, um mês que antes ficaria congelado
    assert b.delete("/api/cafe/ultimo").status_code == 200
    linha = {p["nome"]: p for p in a.get("/api/escritorio", params={"mes": "2026-08"}).json()["pessoas"]}
    assert linha["Bea"]["cafes"] == 0 and linha["Bea"]["valor_cent"] == 0


def test_preco_gravado_no_cafe_nao_muda_com_compras_seguintes(cliente, relogio):
    """O preço é o do momento em que o café foi gravado: uma caixa mais cara
    depois não re-precifica os cafés antigos."""
    a, _ = _mes_com_cafes(cliente, relogio)
    a.post("/api/compras", json={"capsulas": 10, "custo_cent": 600})
    e = a.get("/api/escritorio", params={"mes": "2026-08"}).json()
    linha = {p["nome"]: p for p in e["pessoas"]}
    assert linha["Ana"]["valor_cent"] == 75 and linha["Bea"]["valor_cent"] == 25
    # 45 + 10 cápsulas, 1250 + 600 - 5 × 25 por recuperar: 1725 / 55 = 31,4
    assert e["preco_cent"] == 31
    a.post("/api/cafe")
    assert a.get("/api/eu").json()["valor_cent"] == 25 + 31


def test_escritorio_tem_pote_saldos_e_custos(cliente, relogio):
    a, b = _mes_com_cafes(cliente, relogio)
    e = b.get("/api/escritorio").json()
    assert e["pote"] == {"valor_cent": 1250 - 5 * 25, "capsulas": 45}
    assert {p["nome"]: p["saldo_cent"] for p in e["pessoas"]} == {"Ana": 1250 - 4 * 25, "Bea": -25}
    compra = e["compras"][0]
    assert compra["custo_cent"] == 1250 and compra["custo_estimado"] is False
    assert compra["pode_editar"] is False  # foi a Ana que registou; quem pergunta é a Bea
    assert a.get("/api/escritorio").json()["compras"][0]["pode_editar"] is True
    assert "pago" not in e["pessoas"][0] and "pagamento" not in e["pessoas"][0]


# ---------- sincronização offline (cliente_id / em) ----------

def test_sem_corpo_continua_a_funcionar(cliente):
    """Um cliente antigo em cache não manda corpo nenhum."""
    a = regista(cliente, "Ana")
    r = a.post("/api/cafe")
    assert r.status_code == 201
    corpo = r.json()
    assert corpo["ok"] is True and corpo["cliente_id"] is None and corpo["duplicado"] is False
    assert a.get("/api/eu").json()["cafes"] == 1


def test_cliente_id_repetido_da_um_so_cafe(cliente):
    a = regista(cliente, "Ana")
    corpo = {"cliente_id": "9f1c0b3e-5a7d-4c2e-9b11-2a3f4d5e6c70", "em": "2026-09-11T09:03:12.000Z"}
    r1 = a.post("/api/cafe", json=corpo)
    assert r1.status_code == 201
    d1 = r1.json()
    assert d1["cliente_id"] == corpo["cliente_id"] and d1["duplicado"] is False
    assert d1["em"] == "2026-09-11T09:03:12+00:00"

    r2 = a.post("/api/cafe", json=corpo)
    assert r2.status_code == 200
    d2 = r2.json()
    assert d2["cliente_id"] == corpo["cliente_id"] and d2["duplicado"] is True
    assert d2["em"] == d1["em"]

    assert a.get("/api/eu").json()["cafes"] == 1


def test_em_fora_da_janela_usa_agora(cliente, relogio):
    relogio.set(2026, 9, 11, 9, 0)
    a = regista(cliente, "Ana")
    r_passado = a.post("/api/cafe", json={"em": "2026-08-12T09:00:00.000Z"})  # 30 dias antes
    assert r_passado.json()["em"] == "2026-09-11T09:00:00+00:00"
    r_futuro = a.post("/api/cafe", json={"em": "2026-09-11T10:00:00.000Z"})  # 1h à frente
    assert r_futuro.json()["em"] == "2026-09-11T09:00:00+00:00"


def test_em_sem_fuso_ou_invalido_usa_agora_sem_rebentar(cliente, relogio):
    relogio.set(2026, 9, 11, 9, 0)
    a = regista(cliente, "Ana")
    r_ingenuo = a.post("/api/cafe", json={"em": "2026-09-05T03:30:00"})  # sem fuso
    assert r_ingenuo.status_code == 201
    assert r_ingenuo.json()["em"] == "2026-09-11T09:00:00+00:00"
    r_invalido = a.post("/api/cafe", json={"em": "isto-nao-e-uma-data"})
    assert r_invalido.status_code == 201
    assert r_invalido.json()["em"] == "2026-09-11T09:00:00+00:00"


def test_em_no_mes_anterior_e_aceite_mesmo_depois_de_pagamentos(cliente, relogio):
    """Sem fecho de mês, um café offline de dias antes fica no mês em que foi
    bebido, mesmo que a pessoa já tenha pago entretanto; leva o preço do
    momento em que chegou ao servidor."""
    relogio.set(2026, 8, 10, 9, 0)
    a = regista(cliente, "Ana")
    b = regista(cliente, "Bea")
    a.post("/api/cafe")  # café real de Agosto
    relogio.set(2026, 9, 1, 9, 0)
    ids = {u["nome"]: u["id"] for u in cliente.get("/api/utilizadores").json()}
    a.post("/api/transferencias", json={"recebedor_id": ids["Bea"], "valor_cent": 25})
    # 3 de Setembro: 28 de Agosto está dentro da janela dos 7 dias
    relogio.set(2026, 9, 3, 9, 0)
    r = a.post("/api/cafe", json={"em": "2026-08-28T09:00:00.000Z"})
    assert r.json()["em"] == "2026-08-28T09:00:00+00:00"
    linha = {p["nome"]: p for p in a.get("/api/escritorio", params={"mes": "2026-08"}).json()["pessoas"]}
    assert linha["Ana"]["cafes"] == 2 and linha["Ana"]["valor_cent"] == 50


def test_corrida_entre_select_e_insert_do_cliente_id_devolve_duplicado(cliente, monkeypatch):
    """Duas requisições com o mesmo cliente_id podem intercalar-se entre o
    SELECT que a deteção de duplicado faz e o INSERT que grava: uma ligação
    instável que retransmite um café cujo primeiro pedido ainda está em voo é
    exactamente o caso que a idempotência existe para cobrir. Isto força a
    corrida de forma determinística ao fazer _preco_corrente (chamada depois
    do SELECT e antes do INSERT) inserir a linha concorrente a meio, para que
    o INSERT de marcar_cafe perca a corrida e tenha de recuperar em vez de
    devolver 500."""
    a = regista(cliente, "Ana")
    ana_id = cliente.get("/api/utilizadores").json()[0]["id"]
    cliente_id = "corrida-1"

    original = app_main._preco_corrente
    inserida = {"feito": False}

    def _preco_que_insere_a_meio(c, *args):
        preco = original(c, *args)
        if not inserida["feito"]:
            inserida["feito"] = True
            # simula outra ligação a ganhar a corrida e a inserir primeiro
            em = logic.agora()
            c.execute(
                "INSERT INTO cafes (utilizador_id, em, mes, cliente_id, valor_cent) VALUES (?, ?, ?, ?, ?)",
                (ana_id, em.isoformat(), logic.mes_de(em), cliente_id, preco),
            )
        return preco

    monkeypatch.setattr(app_main, "_preco_corrente", _preco_que_insere_a_meio)
    r = a.post("/api/cafe", json={"cliente_id": cliente_id})

    assert r.status_code == 200, r.text
    corpo = r.json()
    assert corpo["duplicado"] is True
    assert corpo["cliente_id"] == cliente_id

    with db.conn() as c:
        n = c.execute(
            "SELECT COUNT(*) AS n FROM cafes WHERE cliente_id = ?", (cliente_id,)
        ).fetchone()["n"]
    assert n == 1
