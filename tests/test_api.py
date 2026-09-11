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
    a.post("/api/compras", json={"capsulas": 50})
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
    a.post("/api/compras", json={"capsulas": 10})
    s = a.get("/api/eu").json()["stock"]
    assert s["baixo"] is True and s["limiar"] == 16
    assert s["acaba_em"] == "2026-09-18" and s["chega_ao_fim_do_mes"] is False
    a.post("/api/compras", json={"capsulas": 100})
    s = a.get("/api/eu").json()["stock"]
    assert s["baixo"] is False and s["chega_ao_fim_do_mes"] is True


def test_apagar_compra_so_quem_registou_e_sem_stock_negativo(cliente):
    a = regista(cliente, "Ana"); b = regista(cliente, "Bea")
    a.post("/api/compras", json={"capsulas": 2})
    cid = a.get("/api/escritorio").json()["compras"][0]["id"]
    assert b.delete(f"/api/compras/{cid}").status_code == 403
    a.post("/api/cafe")
    assert a.delete(f"/api/compras/{cid}").status_code == 409
    a.delete("/api/cafe/ultimo")
    assert a.delete(f"/api/compras/{cid}").status_code == 200
    assert a.get("/api/eu").json()["stock"]["stock"] == 0


def test_config(cliente):
    a = regista(cliente, "Ana")
    a.put("/api/config", json={"preco_cent": 30, "stock_baixo": 5})
    a.post("/api/cafe")
    eu = a.get("/api/eu").json()
    assert eu["preco_cent"] == 30 and eu["valor_cent"] == 30 and eu["stock"]["limiar"] == 5


# ---------- escritório e fecho de mês ----------

def _mes_com_cafes(cliente, relogio):
    """Agosto: Ana 3 cafés, Bea 1. Setembro: Ana 1. Devolve (ana, bea)."""
    relogio.set(2026, 8, 10, 9, 0)
    a = regista(cliente, "Ana"); b = regista(cliente, "Bea")
    a.post("/api/compras", json={"capsulas": 50})
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


def test_pagamento_registado_por_quem_recebe(cliente, relogio):
    a, b = _mes_com_cafes(cliente, relogio)
    ids = {u["nome"]: u["id"] for u in cliente.get("/api/utilizadores").json()}
    # Bea recebe o dinheiro da Ana
    r = b.post("/api/pagamentos", json={"mes": "2026-08", "pagador_id": ids["Ana"]})
    assert r.status_code == 201 and r.json()["valor_cent"] == 75
    ant = a.get("/api/eu").json()["mes_anterior"]
    assert ant["pago"] is True and ant["pagamento"]["recebedor"] == "Bea" and ant["valor_cent"] == 75
    # duplicado
    assert b.post("/api/pagamentos", json={"mes": "2026-08", "pagador_id": ids["Ana"]}).status_code == 409
    # mês corrente não se fecha
    assert b.post("/api/pagamentos", json={"mes": "2026-09", "pagador_id": ids["Ana"]}).status_code == 400
    # Bea marca-se a si própria
    assert b.post("/api/pagamentos", json={"mes": "2026-08", "pagador_id": ids["Bea"]}).status_code == 201
    linha = {p["nome"]: p for p in a.get("/api/escritorio", params={"mes": "2026-08"}).json()["pessoas"]}
    assert linha["Bea"]["pago"] and linha["Bea"]["pagamento"]["recebedor_id"] == ids["Bea"]


def test_anular_pagamento_so_pelo_recebedor(cliente, relogio):
    a, b = _mes_com_cafes(cliente, relogio)
    ids = {u["nome"]: u["id"] for u in cliente.get("/api/utilizadores").json()}
    b.post("/api/pagamentos", json={"mes": "2026-08", "pagador_id": ids["Ana"]})
    assert a.delete(f"/api/pagamentos/2026-08/{ids['Ana']}").status_code == 403
    assert b.delete(f"/api/pagamentos/2026-08/{ids['Ana']}").status_code == 200
    assert a.get("/api/eu").json()["mes_anterior"]["pago"] is False


def test_mes_pago_fica_congelado(cliente, relogio):
    a, b = _mes_com_cafes(cliente, relogio)
    ids = {u["nome"]: u["id"] for u in cliente.get("/api/utilizadores").json()}
    b.post("/api/pagamentos", json={"mes": "2026-08", "pagador_id": ids["Ana"]})
    # a Bea tenta apagar o café dela de Agosto: recusa
    assert b.delete("/api/cafe/ultimo").status_code == 409
    # a Ana apaga o de Setembro: pode (o último dela é de Setembro)
    assert a.delete("/api/cafe/ultimo").status_code == 200
    # o preço muda depois do pagamento: o snapshot não muda
    a.put("/api/config", json={"preco_cent": 100})
    assert a.get("/api/eu").json()["mes_anterior"]["valor_cent"] == 75
    linha = {p["nome"]: p for p in a.get("/api/escritorio", params={"mes": "2026-08"}).json()["pessoas"]}
    assert linha["Ana"]["valor_cent"] == 75 and linha["Bea"]["valor_cent"] == 100  # Bea ainda não pagou


def test_cafe_novo_nunca_cai_em_mes_pago(cliente, relogio):
    a, b = _mes_com_cafes(cliente, relogio)
    ids = {u["nome"]: u["id"] for u in cliente.get("/api/utilizadores").json()}
    b.post("/api/pagamentos", json={"mes": "2026-08", "pagador_id": ids["Ana"]})
    a.post("/api/cafe")
    linha = {p["nome"]: p for p in a.get("/api/escritorio", params={"mes": "2026-08"}).json()["pessoas"]}
    assert linha["Ana"]["cafes"] == 3


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


def test_em_em_mes_ja_pago_usa_agora(cliente, relogio):
    relogio.set(2026, 8, 10, 9, 0)
    a = regista(cliente, "Ana")
    b = regista(cliente, "Bea")
    a.post("/api/cafe")  # café real de Agosto
    relogio.set(2026, 9, 1, 9, 0)
    ids = {u["nome"]: u["id"] for u in cliente.get("/api/utilizadores").json()}
    b.post("/api/pagamentos", json={"mes": "2026-08", "pagador_id": ids["Ana"]})
    # 3 de Setembro: 28 de Agosto está dentro da janela dos 7 dias, mas cai num mês já pago
    relogio.set(2026, 9, 3, 9, 0)
    r = a.post("/api/cafe", json={"em": "2026-08-28T09:00:00.000Z"})
    assert r.json()["em"] == "2026-09-03T09:00:00+00:00"
    linha = {p["nome"]: p for p in a.get("/api/escritorio", params={"mes": "2026-08"}).json()["pessoas"]}
    assert linha["Ana"]["cafes"] == 1  # o café novo não entrou no mês já pago
