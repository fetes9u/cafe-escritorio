"""Migração da coluna cliente_id em bases de dados já existentes.

A DDL em db.py tem `CREATE TABLE IF NOT EXISTS`, portanto uma base de dados já
criada nunca vê a coluna nova só por correr a DDL outra vez. Estes testes
escrevem à mão o esquema ANTERIOR (sem cliente_id), inserem uma linha, e só
depois correm init(), para provar que a migração dentro de init() é o que
acrescenta a coluna, não a DDL.
"""
import sqlite3

from app import db

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
