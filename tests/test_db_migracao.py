"""Migrações de bases de dados já existentes.

A DDL em db.py tem `CREATE TABLE IF NOT EXISTS`, portanto uma base de dados já
criada nunca vê uma coluna nova só por correr a DDL outra vez. Estes testes
escrevem à mão um esquema ANTERIOR, inserem linhas, e só depois correm init(),
para provar que é a migração dentro de init() que converte a base, não a DDL.
"""
import sqlite3

import pytest

from app import db
from app import main as app_main

SCHEMA_ANTERIOR = """
CREATE TABLE utilizadores (
    id INTEGER PRIMARY KEY,
    nome TEXT NOT NULL UNIQUE COLLATE NOCASE,
    pin_hash TEXT NOT NULL,
    cafes_dia REAL NOT NULL DEFAULT 1,
    tentativas INTEGER NOT NULL DEFAULT 0,
    bloqueado_ate TEXT,
    criado_em TEXT NOT NULL
);
CREATE TABLE cafes (
    id INTEGER PRIMARY KEY,
    utilizador_id INTEGER NOT NULL REFERENCES utilizadores(id),
    em TEXT NOT NULL,
    mes TEXT NOT NULL
);
CREATE INDEX cafes_mes ON cafes(mes, utilizador_id);
"""


def test_init_adiciona_cliente_id_a_base_existente_e_preserva_cafes_reais(tmp_path):
    caminho = str(tmp_path / "existente.db")

    # 1. base de dados anterior, sem cliente_id, escrita à mão.
    ligacao = sqlite3.connect(caminho)
    ligacao.executescript(SCHEMA_ANTERIOR)
    # 2. uma linha real, antes de qualquer migração.
    ligacao.execute(
        "INSERT INTO utilizadores (id, nome, pin_hash, criado_em) VALUES (1, 'Ana', 'x$y', '2026-01-01T00:00:00+00:00')"
    )
    ligacao.execute(
        "INSERT INTO cafes (id, utilizador_id, em, mes) VALUES (1, 1, '2026-01-01T09:00:00+00:00', '2026-01')"
    )
    ligacao.commit()
    ligacao.close()

    # 3. só agora corre a migração.
    db.init(caminho)

    ligacao = sqlite3.connect(caminho)
    ligacao.row_factory = sqlite3.Row
    cols = {r["name"] for r in ligacao.execute("PRAGMA table_info(cafes)")}
    assert "cliente_id" in cols

    indices = {r["name"] for r in ligacao.execute("PRAGMA index_list(cafes)")}
    assert "cafes_cliente_id" in indices

    linha = ligacao.execute("SELECT * FROM cafes WHERE id = 1").fetchone()
    assert linha is not None
    assert linha["cliente_id"] is None
    ligacao.close()


def test_indice_parcial_permite_varios_nulos_mas_nao_duplicados(tmp_path):
    caminho = str(tmp_path / "novo.db")
    db.init(caminho)
    with db.conn() as c:
        c.execute(
            "INSERT INTO utilizadores (nome, pin_hash, criado_em) VALUES ('Ana', 'x$y', '2026-01-01T00:00:00+00:00')"
        )
        # duas linhas com cliente_id NULL não colidem
        c.execute(
            "INSERT INTO cafes (utilizador_id, em, mes) VALUES (1, '2026-01-01T09:00:00+00:00', '2026-01')"
        )
        c.execute(
            "INSERT INTO cafes (utilizador_id, em, mes) VALUES (1, '2026-01-01T10:00:00+00:00', '2026-01')"
        )
        c.execute(
            "INSERT INTO cafes (utilizador_id, em, mes, cliente_id) VALUES (1, '2026-01-01T11:00:00+00:00', '2026-01', 'abc')"
        )
        try:
            c.execute(
                "INSERT INTO cafes (utilizador_id, em, mes, cliente_id) VALUES (1, '2026-01-01T12:00:00+00:00', '2026-01', 'abc')"
            )
            duplicou = True
        except sqlite3.IntegrityError:
            duplicou = False
    assert duplicou is False


# ---------- conversão do fecho de mês para saldos ----------

# DDL literal do esquema do commit 55e7ec6 (o que está em produção antes dos
# saldos), mais o índice que o init() dessa versão criava. Não se importa de
# db.py de propósito: o db.py actual já não tem este esquema.
SCHEMA_55E7EC6 = """
CREATE TABLE IF NOT EXISTS utilizadores (
    id INTEGER PRIMARY KEY,
    nome TEXT NOT NULL UNIQUE COLLATE NOCASE,
    pin_hash TEXT NOT NULL,
    cafes_dia REAL NOT NULL DEFAULT 1,   -- previsão declarada no registo
    tentativas INTEGER NOT NULL DEFAULT 0,
    bloqueado_ate TEXT,
    criado_em TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sessoes (
    token TEXT PRIMARY KEY,
    utilizador_id INTEGER NOT NULL REFERENCES utilizadores(id),
    criado_em TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS cafes (
    id INTEGER PRIMARY KEY,
    utilizador_id INTEGER NOT NULL REFERENCES utilizadores(id),
    em TEXT NOT NULL,        -- instante UTC ISO
    mes TEXT NOT NULL,       -- 'YYYY-MM' em hora local, derivado ao gravar
    cliente_id TEXT          -- UUID gerado pelo cliente, para idempotência offline
);
CREATE INDEX IF NOT EXISTS cafes_mes ON cafes(mes, utilizador_id);
CREATE TABLE IF NOT EXISTS compras (
    id INTEGER PRIMARY KEY,
    utilizador_id INTEGER REFERENCES utilizadores(id),
    capsulas INTEGER NOT NULL,
    em TEXT NOT NULL,
    nota TEXT
);
CREATE TABLE IF NOT EXISTS pagamentos (
    id INTEGER PRIMARY KEY,
    mes TEXT NOT NULL,
    pagador_id INTEGER NOT NULL REFERENCES utilizadores(id),
    recebedor_id INTEGER NOT NULL REFERENCES utilizadores(id),
    capsulas INTEGER NOT NULL,   -- fotografia no momento do pagamento
    valor_cent INTEGER NOT NULL, -- idem
    em TEXT NOT NULL,
    UNIQUE(mes, pagador_id)
);
CREATE TABLE IF NOT EXISTS config (
    chave TEXT PRIMARY KEY,
    valor TEXT NOT NULL
);
INSERT OR IGNORE INTO config VALUES ('preco_cent', '25');
INSERT OR IGNORE INTO config VALUES ('stock_baixo', '16');
CREATE TABLE IF NOT EXISTS subscricoes (
    endpoint TEXT PRIMARY KEY,
    utilizador_id INTEGER NOT NULL REFERENCES utilizadores(id) ON DELETE CASCADE,
    p256dh TEXT NOT NULL,
    auth TEXT NOT NULL,
    dispositivo TEXT,
    criado_em TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS subscricoes_utilizador ON subscricoes(utilizador_id);
-- Só guarda o que a pessoa DESLIGOU. Ausência de linha significa ligado, que é
-- o padrão. Evita semear cinco linhas por cada conta nova.
CREATE TABLE IF NOT EXISTS notificacoes_desligadas (
    utilizador_id INTEGER NOT NULL REFERENCES utilizadores(id) ON DELETE CASCADE,
    evento TEXT NOT NULL,
    PRIMARY KEY (utilizador_id, evento)
);
CREATE UNIQUE INDEX IF NOT EXISTS cafes_cliente_id ON cafes(cliente_id) WHERE cliente_id IS NOT NULL;
"""

ANA, BEA, RUI = 1, 2, 3


def _base_de_producao(caminho: str) -> None:
    """Uma base no esquema de 55e7ec6, como a de produção: Agosto pago pela
    Ana (à Bea) e pela Bea (a si própria), Setembro por pagar, um "pago a
    zero" do Rui num mês sem cafés, e uma caixa de cápsulas sem custo. O preço
    mudou de 25 para 30 depois dos pagamentos, para a fotografia e o preço de
    recurso darem resultados diferentes."""
    ligacao = sqlite3.connect(caminho)
    ligacao.executescript(SCHEMA_55E7EC6)
    ligacao.execute("UPDATE config SET valor = '30' WHERE chave = 'preco_cent'")
    for uid, nome in ((ANA, "Ana"), (BEA, "Bea"), (RUI, "Rui")):
        ligacao.execute(
            "INSERT INTO utilizadores (id, nome, pin_hash, criado_em) "
            "VALUES (?, ?, 'x$y', '2026-07-01T00:00:00+00:00')",
            (uid, nome),
        )
    cafes = [
        (1, ANA, "2026-08-10T09:00:00+00:00", "2026-08", None),
        (2, ANA, "2026-08-11T09:00:00+00:00", "2026-08", "c0ffee00-0000-4000-8000-000000000002"),
        (3, ANA, "2026-08-12T09:00:00+00:00", "2026-08", None),
        (4, BEA, "2026-08-12T10:00:00+00:00", "2026-08", None),
        (5, RUI, "2026-08-13T10:00:00+00:00", "2026-08", None),
        (6, ANA, "2026-09-10T09:00:00+00:00", "2026-09", None),
        (7, ANA, "2026-09-11T09:00:00+00:00", "2026-09", None),
    ]
    ligacao.executemany(
        "INSERT INTO cafes (id, utilizador_id, em, mes, cliente_id) VALUES (?, ?, ?, ?, ?)", cafes
    )
    ligacao.execute(
        "INSERT INTO compras (id, utilizador_id, capsulas, em, nota) "
        "VALUES (1, ?, 50, '2026-08-01T08:00:00+00:00', 'caixa')",
        (BEA,),
    )
    pagamentos = [
        (1, "2026-08", ANA, BEA, 3, 75, "2026-09-02T10:00:00+00:00"),
        (2, "2026-08", BEA, BEA, 1, 25, "2026-09-02T10:05:00+00:00"),
        (3, "2026-07", RUI, BEA, 0, 0, "2026-08-03T10:00:00+00:00"),
    ]
    ligacao.executemany(
        "INSERT INTO pagamentos (id, mes, pagador_id, recebedor_id, capsulas, valor_cent, em) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        pagamentos,
    )
    ligacao.commit()
    ligacao.close()


def _le(caminho: str, sql: str) -> list[dict]:
    ligacao = sqlite3.connect(caminho)
    ligacao.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in ligacao.execute(sql)]
    finally:
        ligacao.close()


def _executa(caminho: str, sql: str, params: tuple = ()) -> None:
    ligacao = sqlite3.connect(caminho)
    ligacao.execute(sql, params)
    ligacao.commit()
    ligacao.close()


def test_conversao_da_base_de_55e7ec6(tmp_path):
    caminho = str(tmp_path / "producao.db")
    _base_de_producao(caminho)

    db.init(caminho)

    precos = {r["id"]: r["valor_cent"] for r in _le(caminho, "SELECT id, valor_cent FROM cafes")}
    # Agosto da Ana: fotografia 75 / 3. Agosto da Bea: fotografia da linha
    # "própria", que não passa a transferência mas continua a ser o preço pago.
    assert precos[1] == precos[2] == precos[3] == 25
    assert precos[4] == 25
    # Agosto do Rui e Setembro da Ana: sem pagamento, preço de recurso.
    assert precos[5] == 30
    assert precos[6] == precos[7] == 30

    compras = _le(caminho, "SELECT * FROM compras")
    assert len(compras) == 1
    assert compras[0]["custo_cent"] == 50 * 30 and compras[0]["custo_estimado"] == 1
    assert compras[0]["nota"] == "caixa"

    # Só o pagamento da Ana à Bea moveu dinheiro: a linha própria da Bea e o
    # "pago a zero" do Rui não viram transferências.
    transf = _le(caminho, "SELECT * FROM transferencias")
    assert transf == [{
        "id": transf[0]["id"], "pagador_id": ANA, "recebedor_id": BEA, "para_caixa": 0, "de_caixa": 0,
        "valor_cent": 75, "em": "2026-09-02T10:00:00+00:00", "confirmada_em": "2026-09-02T10:00:00+00:00",
        "anulada_em": None, "anulada_por": None, "pagamento_origem_id": 1,
    }]
    # Uma base de 0.10 recebe logo a forma da caixa: o lado da caixa aceita NULL.
    assert _forma(caminho, "transferencias") == _forma_nova(tmp_path, "transferencias")

    # A tabela antiga fica como estava, e o resto das linhas também.
    assert len(_le(caminho, "SELECT * FROM pagamentos")) == 3
    assert _le(caminho, "SELECT cliente_id FROM cafes WHERE id = 2")[0]["cliente_id"].startswith("c0ffee00")

    # Sem caixa, a soma dos saldos é −fundo (o que as compras custaram menos
    # o que os cafés cobraram), como no ensaio da secção 6.
    with db.conn() as c:
        saldos = {s["id"]: s["saldo_cent"] for s in app_main._saldos(c)}
        assert app_main._saldo_caixa(c) == 0
        fundo = app_main._fundo(c)
    assert saldos == {ANA: 75 - (3 * 25 + 2 * 30), BEA: 1500 - 75 - 25, RUI: -30}
    assert sum(saldos.values()) == -fundo == 1500 - (4 * 25 + 3 * 30)


def test_segundo_init_nao_duplica_nem_reprecifica(tmp_path):
    caminho = str(tmp_path / "producao.db")
    _base_de_producao(caminho)
    db.init(caminho)
    consultas = (
        "SELECT * FROM cafes ORDER BY id",
        "SELECT * FROM compras ORDER BY id",
        "SELECT * FROM transferencias ORDER BY id",
        "SELECT * FROM historico_alteracoes ORDER BY id",
    )
    antes = [_le(caminho, q) for q in consultas]

    # Se algum passo não se limitasse às linhas por converter, um preço de
    # recurso diferente no segundo arranque via-se aqui.
    _executa(caminho, "UPDATE config SET valor = '40' WHERE chave = 'preco_cent'")
    db.init(caminho)

    depois = [_le(caminho, q) for q in consultas]
    assert depois == antes
    assert len(depois[2]) == 1


def test_conversao_que_falha_a_meio_nao_deixa_nada_convertido(tmp_path):
    """Os passos 4 a 6 entram juntos ou não entram. Um pagamento com um
    pagador que já não existe faz falhar o passo 6 (chave estrangeira); os
    preços e os custos, gravados antes na mesma transacção, têm de voltar a
    NULL. Depois de corrigido, o arranque seguinte converte tudo."""
    caminho = str(tmp_path / "producao.db")
    _base_de_producao(caminho)
    _executa(
        caminho,
        "INSERT INTO pagamentos (id, mes, pagador_id, recebedor_id, capsulas, valor_cent, em) "
        "VALUES (9, '2026-06', 99, ?, 2, 50, '2026-07-01T10:00:00+00:00')",
        (BEA,),
    )

    with pytest.raises(sqlite3.IntegrityError):
        db.init(caminho)
    assert {r["valor_cent"] for r in _le(caminho, "SELECT valor_cent FROM cafes")} == {None}
    assert _le(caminho, "SELECT custo_cent FROM compras") == [{"custo_cent": None}]
    assert _le(caminho, "SELECT * FROM transferencias") == []

    _executa(caminho, "DELETE FROM pagamentos WHERE id = 9")
    db.init(caminho)
    assert None not in {r["valor_cent"] for r in _le(caminho, "SELECT valor_cent FROM cafes")}
    assert len(_le(caminho, "SELECT * FROM transferencias")) == 1


def test_base_nova_nao_tem_a_tabela_pagamentos(tmp_path):
    caminho = str(tmp_path / "nova.db")
    db.init(caminho)
    tabelas = {r["name"] for r in _le(caminho, "SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert "pagamentos" not in tabelas
    assert {"cafes", "compras", "transferencias"} <= tabelas


def _forma(caminho: str, tabela: str) -> dict:
    """Colunas (nome, tipo, NOT NULL, omissão, chave) e índices únicos. Não se
    compara o texto de sqlite_master: depois de um RENAME o SQLite reescreve-o."""
    colunas = [
        (r["name"], r["type"], r["notnull"], r["dflt_value"], r["pk"])
        for r in _le(caminho, f"PRAGMA table_info({tabela})")
    ]
    unicos = sorted(
        tuple(c["name"] for c in _le(caminho, f"PRAGMA index_info('{i['name']}')"))
        for i in _le(caminho, f"PRAGMA index_list({tabela})") if i["unique"]
    )
    return {"colunas": colunas, "unicos": unicos}


def _forma_nova(tmp_path, tabela: str) -> dict:
    """A forma numa base nova. init() muda o caminho global: repõe-se."""
    anterior = db.DB_PATH
    caminho = str(tmp_path / "referencia.db")
    try:
        db.init(caminho)
    finally:
        db.DB_PATH = anterior
    return _forma(caminho, tabela)


# ---------- a caixa: da base de 0.11.0 para a forma nova ----------

# DDL literal do SCHEMA de d6db71d (0.11.0, em produção antes da caixa), mais
# o índice que o init() dessa versão criava e a tabela `pagamentos` que ficou
# da conversão de 0.10 (produção ainda a tem). Não se importa de db.py.
SCHEMA_D6DB71D = """
CREATE TABLE IF NOT EXISTS utilizadores (
    id INTEGER PRIMARY KEY,
    nome TEXT NOT NULL UNIQUE COLLATE NOCASE,
    pin_hash TEXT NOT NULL,
    cafes_dia REAL NOT NULL DEFAULT 1,   -- previsão declarada no registo
    tentativas INTEGER NOT NULL DEFAULT 0,
    bloqueado_ate TEXT,
    criado_em TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sessoes (
    token TEXT PRIMARY KEY,
    utilizador_id INTEGER NOT NULL REFERENCES utilizadores(id),
    criado_em TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS cafes (
    id INTEGER PRIMARY KEY,
    utilizador_id INTEGER NOT NULL REFERENCES utilizadores(id),
    em TEXT NOT NULL,        -- instante UTC ISO
    mes TEXT NOT NULL,       -- 'YYYY-MM' em hora local, derivado ao gravar
    cliente_id TEXT,         -- UUID gerado pelo cliente, para idempotência offline
    valor_cent INTEGER       -- preço em vigor quando o servidor gravou o café
);
CREATE INDEX IF NOT EXISTS cafes_mes ON cafes(mes, utilizador_id);
CREATE TABLE IF NOT EXISTS compras (
    id INTEGER PRIMARY KEY,
    utilizador_id INTEGER REFERENCES utilizadores(id),
    capsulas INTEGER NOT NULL,
    em TEXT NOT NULL,
    nota TEXT,
    custo_cent INTEGER,
    custo_estimado INTEGER NOT NULL DEFAULT 0  -- 1: custo deduzido na conversão, não declarado
);
-- Pagamentos por MB WAY. Contam para o saldo enquanto anulada_em for NULL.
CREATE TABLE IF NOT EXISTS transferencias (
    id INTEGER PRIMARY KEY,
    pagador_id INTEGER NOT NULL REFERENCES utilizadores(id),
    recebedor_id INTEGER NOT NULL REFERENCES utilizadores(id),
    valor_cent INTEGER NOT NULL CHECK (valor_cent > 0),
    em TEXT NOT NULL,
    confirmada_em TEXT,
    anulada_em TEXT,
    anulada_por INTEGER REFERENCES utilizadores(id),
    pagamento_origem_id INTEGER UNIQUE,  -- linha de pagamentos de onde veio, na conversão
    CHECK (pagador_id != recebedor_id)
);
CREATE TABLE IF NOT EXISTS config (
    chave TEXT PRIMARY KEY,
    valor TEXT NOT NULL
);
-- preco_cent já não se edita: é o preço de recurso sem histórico e o da conversão.
INSERT OR IGNORE INTO config VALUES ('preco_cent', '25');
INSERT OR IGNORE INTO config VALUES ('stock_baixo', '16');
CREATE TABLE IF NOT EXISTS subscricoes (
    endpoint TEXT PRIMARY KEY,
    utilizador_id INTEGER NOT NULL REFERENCES utilizadores(id) ON DELETE CASCADE,
    p256dh TEXT NOT NULL,
    auth TEXT NOT NULL,
    dispositivo TEXT,
    criado_em TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS subscricoes_utilizador ON subscricoes(utilizador_id);
-- Só guarda o que a pessoa DESLIGOU. Ausência de linha significa ligado, que é
-- o padrão. Evita semear cinco linhas por cada conta nova.
CREATE TABLE IF NOT EXISTS notificacoes_desligadas (
    utilizador_id INTEGER NOT NULL REFERENCES utilizadores(id) ON DELETE CASCADE,
    evento TEXT NOT NULL,
    PRIMARY KEY (utilizador_id, evento)
);
CREATE UNIQUE INDEX IF NOT EXISTS cafes_cliente_id ON cafes(cliente_id) WHERE cliente_id IS NOT NULL;
CREATE TABLE IF NOT EXISTS pagamentos (
    id INTEGER PRIMARY KEY,
    mes TEXT NOT NULL,
    pagador_id INTEGER NOT NULL REFERENCES utilizadores(id),
    recebedor_id INTEGER NOT NULL REFERENCES utilizadores(id),
    capsulas INTEGER NOT NULL,   -- fotografia no momento do pagamento
    valor_cent INTEGER NOT NULL, -- idem
    em TEXT NOT NULL,
    UNIQUE(mes, pagador_id)
);
"""

TRANSFERENCIAS_0_11 = [
    # convertida do pagamento 1 na passagem a 0.11.0
    (4, ANA, BEA, 75, "2026-09-02T10:00:00+00:00", "2026-09-02T10:00:00+00:00", None, None, 1),
    (9, RUI, ANA, 500, "2026-09-20T10:00:00+00:00", None, None, None, None),
    (12, BEA, RUI, 120, "2026-09-21T10:00:00+00:00", None, "2026-09-21T11:00:00+00:00", RUI, None),
]


def _base_de_0_11_0(caminho: str) -> None:
    """Como a produção antes da caixa: um pagamento mensal já convertido,
    cafés a 25 e três a 0 do preço médio, a oferta a 0,01 € e uma compra do
    bolso."""
    ligacao = sqlite3.connect(caminho)
    ligacao.executescript(SCHEMA_D6DB71D)
    for uid, nome in ((ANA, "Ana"), (BEA, "Bea"), (RUI, "Rui")):
        ligacao.execute(
            "INSERT INTO utilizadores (id, nome, pin_hash, criado_em) "
            "VALUES (?, ?, 'x$y', '2026-07-01T00:00:00+00:00')",
            (uid, nome),
        )
    ligacao.execute(
        "INSERT INTO pagamentos (id, mes, pagador_id, recebedor_id, capsulas, valor_cent, em) "
        "VALUES (1, '2026-08', ?, ?, 3, 75, '2026-09-02T10:00:00+00:00')",
        (ANA, BEA),
    )
    ligacao.executemany(
        "INSERT INTO transferencias (id, pagador_id, recebedor_id, valor_cent, em, confirmada_em, "
        "anulada_em, anulada_por, pagamento_origem_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        TRANSFERENCIAS_0_11,
    )
    ligacao.executemany(
        "INSERT INTO cafes (id, utilizador_id, em, mes, valor_cent) VALUES (?, ?, ?, '2026-09', ?)",
        [
            (1, ANA, "2026-09-10T09:00:00+00:00", 25),
            (2, BEA, "2026-09-10T10:00:00+00:00", 25),
            (3, RUI, "2026-09-22T09:00:00+00:00", 0),
            (4, ANA, "2026-09-22T10:00:00+00:00", 0),
            (5, RUI, "2026-09-22T11:00:00+00:00", 0),
        ],
    )
    ligacao.executemany(
        "INSERT INTO compras (id, utilizador_id, capsulas, em, custo_cent) VALUES (?, ?, ?, ?, ?)",
        [(1, BEA, 77, "2026-09-01T08:00:00+00:00", 1), (2, ANA, 41, "2026-09-15T08:00:00+00:00", 984)],
    )
    ligacao.commit()
    ligacao.close()


def test_conversao_da_base_de_0_11_0(tmp_path):
    caminho = str(tmp_path / "producao.db")
    _base_de_0_11_0(caminho)

    db.init(caminho)

    # Reconstruída com a forma de uma base nova: colunas, NOT NULL, omissões e
    # o UNIQUE de pagamento_origem_id.
    assert _forma(caminho, "transferencias") == _forma_nova(tmp_path, "transferencias")
    assert ("pagamento_origem_id",) in _forma(caminho, "transferencias")["unicos"]
    # Mesmos ids e os mesmos dados; ninguém passou a pagar à caixa.
    colunas = ("id, pagador_id, recebedor_id, valor_cent, em, confirmada_em, anulada_em, anulada_por, "
               "pagamento_origem_id")
    linhas = [tuple(r.values()) for r in _le(caminho, f"SELECT {colunas} FROM transferencias ORDER BY id")]
    assert linhas == TRANSFERENCIAS_0_11
    assert {(r["para_caixa"], r["de_caixa"]) for r in _le(caminho, "SELECT * FROM transferencias")} == {(0, 0)}
    # O INSERT OR IGNORE a partir de `pagamentos` corre depois da reconstrução:
    # sem o UNIQUE, o pagamento 1 entrava outra vez.
    assert len(_le(caminho, "SELECT id FROM transferencias WHERE pagamento_origem_id = 1")) == 1

    # Os CHECKs da secção 3.4 existem mesmo.
    ligacao = sqlite3.connect(caminho)
    for linha in (
        (ANA, None, 1, 1),   # entra e sai da caixa
        (ANA, ANA, 0, 0),    # a si próprio, sem caixa
        (ANA, BEA, 1, 0),    # à caixa com recebedor
        (None, None, 0, 1),  # da caixa sem recebedor
    ):
        with pytest.raises(sqlite3.IntegrityError):
            ligacao.execute(
                "INSERT INTO transferencias (pagador_id, recebedor_id, para_caixa, de_caixa, valor_cent, em) "
                "VALUES (?, ?, ?, ?, 100, '2026-09-23T10:00:00+00:00')",
                linha,
            )
    ligacao.close()

    # Cafés a 0 reparados ao preço configurado, cada um com a sua linha de
    # histórico do sistema; os outros ficam como estavam.
    assert {r["id"]: r["valor_cent"] for r in _le(caminho, "SELECT id, valor_cent FROM cafes")} == {
        1: 25, 2: 25, 3: 25, 4: 25, 5: 25,
    }
    historico = _le(
        caminho,
        "SELECT entidade, entidade_id, campo, antes, depois, utilizador_id FROM historico_alteracoes "
        "ORDER BY entidade_id",
    )
    assert historico == [
        {"entidade": "cafe", "entidade_id": i, "campo": "valor_cent", "antes": "0", "depois": "25",
         "utilizador_id": None}
        for i in (3, 4, 5)
    ]

    # As compras ficam como estão: a oferta corrige-se na app, com rasto.
    assert _le(caminho, "SELECT id, custo_cent, paga_pela_caixa FROM compras ORDER BY id") == [
        {"id": 1, "custo_cent": 1, "paga_pela_caixa": 0}, {"id": 2, "custo_cent": 984, "paga_pela_caixa": 0},
    ]
    # O responsável pela caixa escolhe-se na app, não na migração.
    assert _le(caminho, "SELECT * FROM config WHERE chave = 'caixa_responsavel_id'") == []
    assert _le(caminho, "PRAGMA foreign_key_check") == []


def test_segundo_init_de_0_11_0_nao_muda_nada(tmp_path):
    caminho = str(tmp_path / "producao.db")
    _base_de_0_11_0(caminho)
    db.init(caminho)
    consultas = (
        "SELECT * FROM cafes ORDER BY id",
        "SELECT * FROM compras ORDER BY id",
        "SELECT * FROM transferencias ORDER BY id",
        "SELECT * FROM historico_alteracoes ORDER BY id",
        "SELECT * FROM config ORDER BY chave",
    )
    antes = [_le(caminho, q) for q in consultas]
    forma = _forma(caminho, "transferencias")

    db.init(caminho)

    assert [_le(caminho, q) for q in consultas] == antes
    assert _forma(caminho, "transferencias") == forma
    assert len(antes[3]) == 3


def test_reconstrucao_com_chave_estrangeira_partida_nao_deixa_nada_feito(tmp_path):
    """Com as chaves desligadas durante a reconstrução, a verificação à mão
    é o que impede uma transferência órfã de passar calada: init() falha e a
    base fica em 0.11.0, sem cafés reparados. Depois de corrigida, converte."""
    caminho = str(tmp_path / "producao.db")
    _base_de_0_11_0(caminho)
    _executa(
        caminho,
        "INSERT INTO transferencias (id, pagador_id, recebedor_id, valor_cent, em) "
        "VALUES (20, 99, ?, 50, '2026-09-22T10:00:00+00:00')",
        (BEA,),
    )

    with pytest.raises(sqlite3.IntegrityError):
        db.init(caminho)
    assert "para_caixa" not in {r["name"] for r in _le(caminho, "PRAGMA table_info(transferencias)")}
    assert 0 in {r["valor_cent"] for r in _le(caminho, "SELECT valor_cent FROM cafes")}
    assert _le(caminho, "SELECT * FROM historico_alteracoes") == []

    _executa(caminho, "DELETE FROM transferencias WHERE id = 20")
    db.init(caminho)
    assert "para_caixa" in {r["name"] for r in _le(caminho, "PRAGMA table_info(transferencias)")}


# ---------- compras: AUTOINCREMENT ----------

# DDL literal do SCHEMA de 6429072 (0.12.0, a caixa já em produção, mas
# `compras.id` ainda sem AUTOINCREMENT). Não se importa de db.py.
SCHEMA_6429072 = """
CREATE TABLE IF NOT EXISTS utilizadores (
    id INTEGER PRIMARY KEY,
    nome TEXT NOT NULL UNIQUE COLLATE NOCASE,
    pin_hash TEXT NOT NULL,
    cafes_dia REAL NOT NULL DEFAULT 1,
    tentativas INTEGER NOT NULL DEFAULT 0,
    bloqueado_ate TEXT,
    criado_em TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sessoes (
    token TEXT PRIMARY KEY,
    utilizador_id INTEGER NOT NULL REFERENCES utilizadores(id),
    criado_em TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS cafes (
    id INTEGER PRIMARY KEY,
    utilizador_id INTEGER NOT NULL REFERENCES utilizadores(id),
    em TEXT NOT NULL,
    mes TEXT NOT NULL,
    cliente_id TEXT,
    valor_cent INTEGER
);
CREATE INDEX IF NOT EXISTS cafes_mes ON cafes(mes, utilizador_id);
CREATE UNIQUE INDEX IF NOT EXISTS cafes_cliente_id ON cafes(cliente_id) WHERE cliente_id IS NOT NULL;
CREATE TABLE IF NOT EXISTS compras (
    id INTEGER PRIMARY KEY,
    utilizador_id INTEGER REFERENCES utilizadores(id),
    capsulas INTEGER NOT NULL,
    em TEXT NOT NULL,
    nota TEXT,
    custo_cent INTEGER,
    custo_estimado INTEGER NOT NULL DEFAULT 0,
    paga_pela_caixa INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS transferencias (
    id INTEGER PRIMARY KEY,
    pagador_id INTEGER REFERENCES utilizadores(id),
    recebedor_id INTEGER REFERENCES utilizadores(id),
    para_caixa INTEGER NOT NULL DEFAULT 0,
    de_caixa INTEGER NOT NULL DEFAULT 0,
    valor_cent INTEGER NOT NULL CHECK (valor_cent > 0),
    em TEXT NOT NULL,
    confirmada_em TEXT,
    anulada_em TEXT,
    anulada_por INTEGER REFERENCES utilizadores(id),
    pagamento_origem_id INTEGER UNIQUE,
    CHECK (NOT (para_caixa = 1 AND de_caixa = 1)),
    CHECK ((para_caixa = 1) = (recebedor_id IS NULL)),
    CHECK ((de_caixa = 1) = (pagador_id IS NULL)),
    CHECK (pagador_id IS NULL OR recebedor_id IS NULL OR pagador_id != recebedor_id)
);
CREATE TABLE IF NOT EXISTS config (
    chave TEXT PRIMARY KEY,
    valor TEXT NOT NULL
);
INSERT OR IGNORE INTO config VALUES ('preco_cent', '25');
INSERT OR IGNORE INTO config VALUES ('stock_baixo', '16');
CREATE TABLE IF NOT EXISTS subscricoes (
    endpoint TEXT PRIMARY KEY,
    utilizador_id INTEGER NOT NULL REFERENCES utilizadores(id) ON DELETE CASCADE,
    p256dh TEXT NOT NULL,
    auth TEXT NOT NULL,
    dispositivo TEXT,
    criado_em TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS subscricoes_utilizador ON subscricoes(utilizador_id);
CREATE TABLE IF NOT EXISTS notificacoes_desligadas (
    utilizador_id INTEGER NOT NULL REFERENCES utilizadores(id) ON DELETE CASCADE,
    evento TEXT NOT NULL,
    PRIMARY KEY (utilizador_id, evento)
);
CREATE TABLE IF NOT EXISTS historico_alteracoes (
    id INTEGER PRIMARY KEY,
    entidade TEXT NOT NULL,
    entidade_id INTEGER,
    campo TEXT NOT NULL,
    antes TEXT,
    depois TEXT,
    utilizador_id INTEGER REFERENCES utilizadores(id),
    em TEXT NOT NULL
);
"""


def _base_de_6429072(caminho: str) -> None:
    """Como a produção antes deste arranjo: três compras vivas (ids 1 a 3) e
    uma linha de histórico à espera do id 7, resto de uma edição numa compra
    que entretanto foi apagada. Sem AUTOINCREMENT, o próximo INSERT ia
    reaproveitar um id e herdar esse histórico."""
    ligacao = sqlite3.connect(caminho)
    ligacao.executescript(SCHEMA_6429072)
    for uid, nome in ((ANA, "Ana"), (BEA, "Bea"), (RUI, "Rui")):
        ligacao.execute(
            "INSERT INTO utilizadores (id, nome, pin_hash, criado_em) "
            "VALUES (?, ?, 'x$y', '2026-07-01T00:00:00+00:00')",
            (uid, nome),
        )
    ligacao.executemany(
        "INSERT INTO compras (id, utilizador_id, capsulas, em, custo_cent, custo_estimado, paga_pela_caixa) "
        "VALUES (?, ?, ?, ?, ?, 0, 0)",
        [
            (1, BEA, 77, "2026-09-01T08:00:00+00:00", 1),
            (2, ANA, 41, "2026-09-15T08:00:00+00:00", 984),
            (3, RUI, 10, "2026-09-18T08:00:00+00:00", 250),
        ],
    )
    ligacao.execute(
        "INSERT INTO historico_alteracoes (entidade, entidade_id, campo, antes, depois, utilizador_id, em) "
        "VALUES ('compra', 7, 'custo_cent', '500', '600', ?, '2026-09-19T08:00:00+00:00')",
        (ANA,),
    )
    ligacao.commit()
    ligacao.close()


def test_conversao_de_compras_sem_autoincrement(tmp_path):
    caminho = str(tmp_path / "producao.db")
    _base_de_6429072(caminho)

    db.init(caminho)

    # Mesmos ids, mesmas colunas, mesmos dados.
    assert _le(caminho, "SELECT id, utilizador_id, capsulas, custo_cent FROM compras ORDER BY id") == [
        {"id": 1, "utilizador_id": BEA, "capsulas": 77, "custo_cent": 1},
        {"id": 2, "utilizador_id": ANA, "capsulas": 41, "custo_cent": 984},
        {"id": 3, "utilizador_id": RUI, "capsulas": 10, "custo_cent": 250},
    ]
    # AUTOINCREMENT em vigor.
    assert "AUTOINCREMENT" in _le(
        caminho, "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'compras'"
    )[0]["sql"]
    # O histórico da compra 7 (apagada antes da migração) continua lá.
    assert _le(caminho, "SELECT entidade_id FROM historico_alteracoes WHERE campo = 'custo_cent'") == [
        {"entidade_id": 7},
    ]
    # O próximo INSERT fica acima dos dois: nem o maior id vivo (3) nem o
    # histórico da compra 7 (id maior) são reaproveitados.
    ligacao = sqlite3.connect(caminho)
    cursor = ligacao.execute(
        "INSERT INTO compras (utilizador_id, capsulas, em, custo_cent) "
        "VALUES (?, 5, '2026-09-23T09:00:00+00:00', 125)",
        (ANA,),
    )
    ligacao.commit()
    assert cursor.lastrowid == 8
    ligacao.close()


def test_segundo_init_de_compras_sem_autoincrement_nao_muda_nada(tmp_path):
    caminho = str(tmp_path / "producao.db")
    _base_de_6429072(caminho)
    db.init(caminho)
    consultas = (
        "SELECT * FROM compras ORDER BY id",
        "SELECT * FROM historico_alteracoes ORDER BY id",
        "SELECT * FROM sqlite_sequence WHERE name = 'compras'",
    )
    antes = [_le(caminho, q) for q in consultas]
    forma = _forma(caminho, "compras")

    db.init(caminho)

    assert [_le(caminho, q) for q in consultas] == antes
    assert _forma(caminho, "compras") == forma
    assert antes[2] == [{"name": "compras", "seq": 7}]
