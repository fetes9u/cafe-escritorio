"""Ligação SQLite e esquema. Um ficheiro; o esquema cria-se se faltar e init()
converte uma base antiga no lugar (colunas novas e dados do fecho de mês)."""
import os
import sqlite3
from contextlib import contextmanager

DB_PATH = os.environ.get("CAFE_DB", "data/cafe.db")

SCHEMA = """
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
"""


def init(path: str | None = None) -> None:
    global DB_PATH
    if path:
        DB_PATH = path
    d = os.path.dirname(DB_PATH)
    if d:
        os.makedirs(d, exist_ok=True)
    with conn(autocommit=True) as c:
        c.executescript(SCHEMA)
        # migração: a DDL acima só corre em base de dados nova.
        _acrescenta_coluna(c, "cafes", "cliente_id", "TEXT")
        c.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS cafes_cliente_id "
            "ON cafes(cliente_id) WHERE cliente_id IS NOT NULL"
        )
        _acrescenta_coluna(c, "cafes", "valor_cent", "INTEGER")
        _acrescenta_coluna(c, "compras", "custo_cent", "INTEGER")
        _acrescenta_coluna(c, "compras", "custo_estimado", "INTEGER NOT NULL DEFAULT 0")
        _converte_fecho_de_mes(c)


def _acrescenta_coluna(c: sqlite3.Connection, tabela: str, coluna: str, tipo: str) -> None:
    cols = {r["name"] for r in c.execute(f"PRAGMA table_info({tabela})")}
    if coluna not in cols:
        c.execute(f"ALTER TABLE {tabela} ADD COLUMN {coluna} {tipo}")


def _converte_fecho_de_mes(c: sqlite3.Connection) -> None:
    """Preenche os preços dos cafés, os custos das compras e passa os
    pagamentos mensais a transferências. Só toca em linhas por converter
    (IS NULL, UNIQUE), portanto um segundo arranque não duplica nada; a
    transacção explícita garante que os três passos entram juntos."""
    c.execute("BEGIN IMMEDIATE")
    try:
        preco = int(get_config(c, "preco_cent"))
        # A tabela antiga só existe em bases criadas antes dos saldos.
        tem_pagamentos = c.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'pagamentos'"
        ).fetchone()
        if tem_pagamentos:
            # Mês pago: o preço da fotografia (valor gravado como n × preço, divisão exacta).
            c.execute(
                "UPDATE cafes SET valor_cent = ("
                "  SELECT p.valor_cent / p.capsulas FROM pagamentos p"
                "  WHERE p.mes = cafes.mes AND p.pagador_id = cafes.utilizador_id AND p.capsulas > 0"
                ") WHERE valor_cent IS NULL"
            )
        c.execute("UPDATE cafes SET valor_cent = ? WHERE valor_cent IS NULL", (preco,))
        c.execute(
            "UPDATE compras SET custo_cent = capsulas * ?, custo_estimado = 1 WHERE custo_cent IS NULL",
            (preco,),
        )
        if tem_pagamentos:
            # Quem recebia marcava também a sua própria linha, e um mês sem
            # cafés ficava pago a zero: nenhum dos dois moveu dinheiro.
            c.execute(
                "INSERT OR IGNORE INTO transferencias "
                "(pagador_id, recebedor_id, valor_cent, em, confirmada_em, pagamento_origem_id) "
                "SELECT pagador_id, recebedor_id, valor_cent, em, em, id FROM pagamentos "
                "WHERE pagador_id != recebedor_id AND valor_cent > 0"
            )
        c.execute("COMMIT")
    except BaseException:
        c.execute("ROLLBACK")
        raise


@contextmanager
def conn(autocommit: bool = False):
    """Com autocommit, o sqlite3 não abre transacções sozinho e quem chama
    usa BEGIN e COMMIT explícitos."""
    c = sqlite3.connect(DB_PATH, timeout=10, isolation_level=None if autocommit else "")
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    c.execute("PRAGMA journal_mode = WAL")
    try:
        yield c
        c.commit()
    finally:
        c.close()


def get_config(c: sqlite3.Connection, chave: str) -> str:
    return c.execute("SELECT valor FROM config WHERE chave = ?", (chave,)).fetchone()["valor"]


def set_config(c: sqlite3.Connection, chave: str, valor: str) -> None:
    c.execute("INSERT OR REPLACE INTO config VALUES (?, ?)", (chave, valor))
