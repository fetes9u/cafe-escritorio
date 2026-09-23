"""Ligação SQLite e esquema. Um ficheiro; o esquema cria-se se faltar e init()
converte uma base antiga no lugar (colunas novas, dados do fecho de mês e a
forma das transferências com a caixa)."""
import os
import sqlite3
from contextlib import contextmanager

from . import logic

DB_PATH = os.environ.get("CAFE_DB", "data/cafe.db")


def _ddl_compras(tabela: str) -> str:
    """A mesma DDL serve a base nova e a reconstrução de uma base sem
    AUTOINCREMENT. AUTOINCREMENT evita que o próximo INSERT reaproveite o id
    de uma compra apagada e herde o histórico dela."""
    return f"""
CREATE TABLE IF NOT EXISTS {tabela} (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    utilizador_id INTEGER REFERENCES utilizadores(id),
    capsulas INTEGER NOT NULL,
    em TEXT NOT NULL,
    nota TEXT,
    custo_cent INTEGER,
    custo_estimado INTEGER NOT NULL DEFAULT 0,  -- 1: custo deduzido na conversão, não declarado
    paga_pela_caixa INTEGER NOT NULL DEFAULT 0  -- 1: o custo saiu da caixa, não do bolso de quem registou
);
"""


def _ddl_transferencias(tabela: str) -> str:
    """A mesma DDL serve a base nova e a reconstrução de uma base de 0.11.0."""
    return f"""
-- Pagamentos por MB WAY. Contam para o saldo enquanto anulada_em for NULL.
-- O lado da caixa fica a NULL: é de quem a guardar no momento em que se lê.
CREATE TABLE IF NOT EXISTS {tabela} (
    id INTEGER PRIMARY KEY,
    pagador_id INTEGER REFERENCES utilizadores(id),    -- NULL: saiu da caixa
    recebedor_id INTEGER REFERENCES utilizadores(id),  -- NULL: foi para a caixa
    para_caixa INTEGER NOT NULL DEFAULT 0,
    de_caixa INTEGER NOT NULL DEFAULT 0,
    valor_cent INTEGER NOT NULL CHECK (valor_cent > 0),
    em TEXT NOT NULL,
    confirmada_em TEXT,
    anulada_em TEXT,
    anulada_por INTEGER REFERENCES utilizadores(id),
    pagamento_origem_id INTEGER UNIQUE,  -- linha de pagamentos de onde veio, na conversão
    CHECK (NOT (para_caixa = 1 AND de_caixa = 1)),
    CHECK ((para_caixa = 1) = (recebedor_id IS NULL)),
    CHECK ((de_caixa = 1) = (pagador_id IS NULL)),
    CHECK (pagador_id IS NULL OR recebedor_id IS NULL OR pagador_id != recebedor_id)
);
"""


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
""" + _ddl_compras("compras") + _ddl_transferencias("transferencias") + """
CREATE TABLE IF NOT EXISTS config (
    chave TEXT PRIMARY KEY,
    valor TEXT NOT NULL
);
-- caixa_responsavel_id só tem linha enquanto alguém guarda a caixa.
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
-- Quem mudou o quê e quando. utilizador_id NULL = o sistema (migração).
CREATE TABLE IF NOT EXISTS historico_alteracoes (
    id INTEGER PRIMARY KEY,
    entidade TEXT NOT NULL,      -- transferencia, compra, config, cafe
    entidade_id INTEGER,         -- NULL para config
    campo TEXT NOT NULL,
    antes TEXT,
    depois TEXT,
    utilizador_id INTEGER REFERENCES utilizadores(id),
    em TEXT NOT NULL
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
        _acrescenta_coluna(c, "compras", "paga_pela_caixa", "INTEGER NOT NULL DEFAULT 0")
        _converte(c)


def _colunas(c: sqlite3.Connection, tabela: str) -> set[str]:
    return {r["name"] for r in c.execute(f"PRAGMA table_info({tabela})")}


def _acrescenta_coluna(c: sqlite3.Connection, tabela: str, coluna: str, tipo: str) -> None:
    if coluna not in _colunas(c, tabela):
        c.execute(f"ALTER TABLE {tabela} ADD COLUMN {coluna} {tipo}")


def _converte(c: sqlite3.Connection) -> None:
    """Os passos de dados, numa só transacção explícita: reconstruir as
    transferências de 0.11.0, reconstruir as compras sem AUTOINCREMENT,
    converter o fecho de mês e reparar os cafés gravados a zero pelo preço
    médio. Cada passo só toca no que está por converter (coluna em falta, IS
    NULL, UNIQUE, = 0, texto do DDL sem AUTOINCREMENT), portanto um segundo
    arranque não muda nada.

    A reconstrução segue o procedimento documentado do SQLite: chaves
    estrangeiras desligadas nesta ligação (o PRAGMA não faz nada dentro de uma
    transacção, por isso desliga-se antes do BEGIN) e verificadas à mão antes
    do COMMIT. Uma base de 0.10 recebe a forma nova do SCHEMA e converte com as
    chaves ligadas."""
    reconstruir_transferencias = "para_caixa" not in _colunas(c, "transferencias")
    reconstruir_compras = "AUTOINCREMENT" not in (_sql_tabela(c, "compras") or "")
    desliga_fk = reconstruir_transferencias or reconstruir_compras
    if desliga_fk:
        c.execute("PRAGMA foreign_keys = OFF")
    c.execute("BEGIN IMMEDIATE")
    try:
        if reconstruir_transferencias:
            _reconstroi_transferencias(c)
        if reconstruir_compras:
            _reconstroi_compras(c)
            _garante_sequencia_compras(c)
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
        # O preço médio chegou a zero e deixou cafés de graça: passam ao preço
        # fixo, com rasto. O preço configurado nunca é 0, portanto isto não
        # volta a encontrar nada no arranque seguinte. Uma só execução da
        # migração = um só instante em todas as linhas que ela grava.
        em_migracao = logic.agora().isoformat()
        for cafe in c.execute("SELECT id FROM cafes WHERE valor_cent = 0 ORDER BY id").fetchall():
            c.execute("UPDATE cafes SET valor_cent = ? WHERE id = ?", (preco, cafe["id"]))
            regista_alteracao(c, "cafe", cafe["id"], "valor_cent", 0, preco, None, em=em_migracao)
        # Com as chaves desligadas, nenhuma escrita desta transacção é
        # verificada em tempo real (não só as das tabelas reconstruídas), por
        # isso a verificação à mão cobre a base toda.
        if desliga_fk and c.execute("PRAGMA foreign_key_check").fetchone():
            raise sqlite3.IntegrityError("FOREIGN KEY constraint failed na conversão")
        c.execute("COMMIT")
    except BaseException:
        c.execute("ROLLBACK")
        raise
    finally:
        if desliga_fk:
            c.execute("PRAGMA foreign_keys = ON")


def _reconstroi_transferencias(c: sqlite3.Connection) -> None:
    """0.11.0 obrigava pagador e recebedor; o lado da caixa precisa de NULL, e
    o SQLite não muda uma restrição sem refazer a tabela. Mesmos ids, mesmo
    pagamento_origem_id UNIQUE."""
    c.execute(_ddl_transferencias("transferencias_nova"))
    c.execute(
        "INSERT INTO transferencias_nova (id, pagador_id, recebedor_id, valor_cent, em, "
        "confirmada_em, anulada_em, anulada_por, pagamento_origem_id) "
        "SELECT id, pagador_id, recebedor_id, valor_cent, em, confirmada_em, anulada_em, "
        "anulada_por, pagamento_origem_id FROM transferencias"
    )
    c.execute("DROP TABLE transferencias")
    c.execute("ALTER TABLE transferencias_nova RENAME TO transferencias")


def _reconstroi_compras(c: sqlite3.Connection) -> None:
    """Sem AUTOINCREMENT, apagar a compra mais recente deixava o próximo
    INSERT reaproveitar o id, herdando o histórico da compra apagada. Mesmos
    ids, mesmas colunas, mesmos dados."""
    c.execute(_ddl_compras("compras_nova"))
    c.execute(
        "INSERT INTO compras_nova (id, utilizador_id, capsulas, em, nota, custo_cent, "
        "custo_estimado, paga_pela_caixa) "
        "SELECT id, utilizador_id, capsulas, em, nota, custo_cent, custo_estimado, paga_pela_caixa "
        "FROM compras"
    )
    c.execute("DROP TABLE compras")
    c.execute("ALTER TABLE compras_nova RENAME TO compras")


def _garante_sequencia_compras(c: sqlite3.Connection) -> None:
    """Uma compra apagada antes desta migração pode ter id maior do que
    qualquer compra que sobrou, com linhas de histórico à espera desse id.
    AUTOINCREMENT só olha para as linhas que existem; sem isto, o próximo
    INSERT reaproveitava-o na mesma. sqlite_sequence não tem chave única em
    `name`, por isso lê-se e escreve-se à mão."""
    maximo = c.execute(
        "SELECT MAX(v) AS m FROM ("
        "  SELECT MAX(id) AS v FROM compras"
        "  UNION ALL"
        "  SELECT MAX(entidade_id) AS v FROM historico_alteracoes WHERE entidade = 'compra'"
        ")"
    ).fetchone()["m"]
    if maximo is None:
        return
    actual = c.execute("SELECT seq FROM sqlite_sequence WHERE name = 'compras'").fetchone()
    if actual is None:
        c.execute("INSERT INTO sqlite_sequence (name, seq) VALUES ('compras', ?)", (maximo,))
    elif actual["seq"] < maximo:
        c.execute("UPDATE sqlite_sequence SET seq = ? WHERE name = 'compras'", (maximo,))


def _sql_tabela(c: sqlite3.Connection, tabela: str) -> str | None:
    linha = c.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?", (tabela,)
    ).fetchone()
    return linha["sql"] if linha else None


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


def regista_alteracao(c: sqlite3.Connection, entidade: str, entidade_id: int | None, campo: str,
                      antes, depois, utilizador_id: int | None, *, em: str) -> None:
    """Uma linha do histórico. Os valores ficam como texto (booleanos como
    "0"/"1"); None fica NULL. utilizador_id None = o sistema. `em` é sempre o
    instante do pedido (ou da execução da migração) que despoletou esta
    linha, nunca um novo `logic.agora()` aqui dentro: as várias linhas
    gravadas por UM pedido têm de repetir o mesmo instante."""
    c.execute(
        "INSERT INTO historico_alteracoes (entidade, entidade_id, campo, antes, depois, utilizador_id, em) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (entidade, entidade_id, campo, _texto(antes), _texto(depois), utilizador_id, em),
    )


def _texto(valor) -> str | None:
    if valor is None:
        return None
    if isinstance(valor, bool):
        return "1" if valor else "0"
    return str(valor)
