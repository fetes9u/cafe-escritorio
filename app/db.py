"""Ligação SQLite e esquema. Um ficheiro, sem migrações — o esquema é criado se faltar."""
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
    mes TEXT NOT NULL        -- 'YYYY-MM' em hora local, derivado ao gravar
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
"""


def init(path: str | None = None) -> None:
    global DB_PATH
    if path:
        DB_PATH = path
    d = os.path.dirname(DB_PATH)
    if d:
        os.makedirs(d, exist_ok=True)
    with conn() as c:
        c.executescript(SCHEMA)


@contextmanager
def conn():
    c = sqlite3.connect(DB_PATH, timeout=10)
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
