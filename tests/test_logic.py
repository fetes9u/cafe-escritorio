from datetime import date, datetime, timezone

from app import logic


def test_mes_de_usa_hora_local():
    # 00:30 do dia 1 em Lisboa (verão, UTC+1) é 23:30 do dia anterior em UTC
    utc = datetime(2026, 8, 31, 23, 30, tzinfo=timezone.utc)
    assert logic.mes_de(utc) == "2026-09"


def test_dias_uteis():
    # Setembro 2026: dia 1 é terça; 22 dias úteis no mês
    assert logic.dias_uteis(date(2026, 9, 1), date(2026, 9, 30)) == 22
    assert logic.dias_uteis(date(2026, 9, 5), date(2026, 9, 6)) == 0  # fim-de-semana
    assert logic.dias_uteis(date(2026, 9, 10), date(2026, 9, 9)) == 0


def test_ritmo_usa_declarado_sem_historico():
    assert logic.ritmo_diario(3, 2, 1.5) == 1.5
    assert logic.ritmo_diario(10, 5, 1.5) == 2.0


def test_estimativa_dia_1_nao_rebenta():
    # dia 1 (terça), 1 café bebido, declarou 2/dia → 1 + 2*21 = 43
    assert logic.estimativa_mes(1, date(2026, 9, 1), "2026-09", 2) == 43


def test_estimativa_com_historico():
    # até dia 11 (sexta) há 9 dias úteis; 18 cafés → 2/dia; faltam 13 dias úteis → 18 + 26
    assert logic.estimativa_mes(18, date(2026, 9, 11), "2026-09", 0.5) == 44


def test_estimativa_pessoa_registada_a_meio_do_mes():
    # registou-se dia 21 (seg); até dia 25 (sex) são 5 dias úteis; 10 cafés → 2/dia; faltam 3 → 16
    assert logic.estimativa_mes(10, date(2026, 9, 25), "2026-09", 0.5, date(2026, 9, 21)) == 16
    # registo em mês anterior não altera nada
    assert logic.dias_decorridos("2026-09", date(2026, 9, 11), date(2026, 8, 3)) == 9


def test_arredonda_meio_sobe():
    assert logic.arredonda(4.5) == 5
    assert logic.arredonda(4.4) == 4


def test_estimativa_ultimo_dia():
    assert logic.estimativa_mes(30, date(2026, 9, 30), "2026-09", 2) == 30


def test_data_fim_stock():
    hoje = date(2026, 9, 11)  # sexta
    assert logic.data_fim_stock(4, 2.0, hoje) == date(2026, 9, 15)  # seg + ter
    assert logic.data_fim_stock(4, 0, hoje) is None
    assert logic.data_fim_stock(0, 2.0, hoje) == hoje


def test_resumo_stock_avisa_quando_nao_chega():
    r = logic.resumo_stock(10, 2.0, date(2026, 9, 11), 16)
    assert r["baixo"] is True
    assert r["acaba_em"] == "2026-09-18"
    assert r["chega_ao_fim_do_mes"] is False
    r = logic.resumo_stock(100, 2.0, date(2026, 9, 11), 16)
    assert r["baixo"] is False
    assert r["chega_ao_fim_do_mes"] is True


# ---------- preço médio do armário ----------

def test_preco_medio_arredonda_ao_centimo():
    # 18,50 € por recuperar e 60 cápsulas no armário: 30,83 cêntimos
    assert logic.preco_corrente(1850, 60, 25) == 31


def test_preco_com_armario_vazio_usa_o_ultimo_preco():
    assert logic.preco_corrente(0, 0, 31) == 31
    assert logic.preco_corrente(500, -3, 27) == 27


def test_preco_nunca_e_negativo():
    assert logic.preco_corrente(-200, 10, 25) == 0


def test_preco_meio_centimo_sobe():
    assert logic.preco_corrente(25, 2, 99) == 13  # 12,5
    assert logic.preco_corrente(24, 2, 99) == 12


def test_exemplo_da_revisao_fecha_o_pote_ao_centimo():
    """Caixa de 100 a 25 €, 50 bebidas, caixa de 10 a 6 €, e bebe-se até o
    armário ficar vazio. Com o custo médio, o valor por recuperar nunca fica
    negativo pelo caminho e acaba exactamente em zero."""
    custo, capsulas, cobrado, bebidos = 0, 0, 0, 0
    ultimo = 25

    def bebe():
        nonlocal cobrado, bebidos, ultimo
        preco = logic.preco_corrente(custo - cobrado, capsulas - bebidos, ultimo)
        cobrado += preco
        bebidos += 1
        ultimo = preco
        assert custo - cobrado >= 0

    custo, capsulas = 2500, 100
    for _ in range(50):
        bebe()
    assert cobrado == 1250

    custo, capsulas = custo + 600, capsulas + 10
    assert logic.preco_corrente(custo - cobrado, capsulas - bebidos, ultimo) == 31
    while capsulas - bebidos > 0:
        bebe()
    assert bebidos == 110
    assert custo - cobrado == 0


# ---------- sugestão de a quem pagar ----------

def test_sugestao_so_para_quem_deve():
    assert logic.sugestao_pagamento(0, [(1, "Ana", 500)]) is None
    assert logic.sugestao_pagamento(100, [(1, "Ana", 500)]) is None


def test_sugestao_sem_credor_positivo_e_nula():
    assert logic.sugestao_pagamento(-300, [(1, "Ana", 0), (2, "Bea", -50)]) is None


def test_sugestao_escolhe_o_maior_credor_e_limita_ao_que_tem_a_receber():
    outros = [(1, "Ana", 200), (2, "Bea", 700), (3, "Rui", 50)]
    assert logic.sugestao_pagamento(-300, outros) == {"utilizador_id": 2, "nome": "Bea", "valor_cent": 300}
    assert logic.sugestao_pagamento(-900, outros) == {"utilizador_id": 2, "nome": "Bea", "valor_cent": 700}


def test_sugestao_em_empate_vai_pelo_nome():
    outros = [(1, "rui", 400), (2, "Ana", 400)]
    assert logic.sugestao_pagamento(-100, outros)["nome"] == "Ana"
