"""API do café do escritório. Uma app FastAPI que também serve o front-end estático."""
import hashlib
import json
import os
import secrets
import sqlite3
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path

from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from py_vapid import Vapid
from pydantic import BaseModel, Field
from pywebpush import WebPushException, webpush

from . import db, logic

MAX_TENTATIVAS = 5
BLOQUEIO = timedelta(seconds=60)
COOKIE = "cafe_sessao"
SESSAO_DIAS = 180
STATIC = Path(__file__).parent / "static"

EVENTOS = ("cafe", "compra", "pagamento", "stock_baixo", "registo")

# Titles shown in the push notification for each evento. These names must
# match exactly what app/static/sw.js's push handler reads off the pushed
# JSON (`dados.titulo`), see the contract test in tests/test_contrato_api.py
# that extracts the field names from sw.js and checks the server actually
# fills them. Short on purpose: a phone truncates a long title.
TITULOS_EVENTO = {
    "cafe": "Café",
    "compra": "Stock reposto",
    "pagamento": "Pagamento",
    "stock_baixo": "Stock baixo",
    "registo": "Colega novo",
}



@asynccontextmanager
async def _lifespan(_app):
    db.init()
    yield


app = FastAPI(title="Café do escritório", docs_url=None, redoc_url=None, lifespan=_lifespan)


@app.middleware("http")
async def _cabecalhos(request: Request, call_next):
    resp = await call_next(request)
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Content-Security-Policy"] = "default-src 'self'; img-src 'self' data:"
    # "no-cache" still allows the cheap ETag revalidation round trip on our
    # LAN; it only stops the browser from serving /static/app.js or
    # style.css straight out of heuristic freshness after a deploy.
    resp.headers["Cache-Control"] = "no-cache"
    return resp


def _hoje():
    return logic.local(logic.agora()).date()


def _data_registo(iso: str):
    return logic.local(datetime.fromisoformat(iso)).date()


# ---------- autenticação ----------

def _hash_pin(pin: str, salt: str | None = None) -> str:
    salt = salt or secrets.token_hex(8)
    h = hashlib.pbkdf2_hmac("sha256", pin.encode(), salt.encode(), 100_000).hex()
    return f"{salt}${h}"


def _verifica_pin(pin: str, guardado: str) -> bool:
    salt, _ = guardado.split("$", 1)
    return secrets.compare_digest(_hash_pin(pin, salt), guardado)


def _valida_pin(pin: str) -> None:
    if len(pin) != 4 or not pin.isdigit():
        raise HTTPException(400, "O PIN tem de ter exactamente 4 dígitos.")


def _abre_sessao(c, utilizador_id: int, request: Request, resp: Response) -> None:
    token = secrets.token_urlsafe(32)
    c.execute("INSERT INTO sessoes VALUES (?, ?, ?)", (token, utilizador_id, logic.agora().isoformat()))
    https = request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https"
    resp.set_cookie(COOKIE, token, httponly=True, samesite="lax", secure=https,
                    max_age=SESSAO_DIAS * 24 * 3600)


def utilizador_actual(request: Request) -> dict:
    token = request.cookies.get(COOKIE)
    if not token:
        raise HTTPException(401, "Sessão em falta.")
    with db.conn() as c:
        row = c.execute(
            "SELECT u.* FROM sessoes s JOIN utilizadores u ON u.id = s.utilizador_id WHERE s.token = ?",
            (token,),
        ).fetchone()
    if not row:
        raise HTTPException(401, "Sessão inválida.")
    return dict(row)


class Registo(BaseModel):
    nome: str = Field(min_length=1, max_length=40)
    pin: str
    cafes_dia: float = Field(default=1, ge=0, le=20)


class Login(BaseModel):
    utilizador_id: int
    pin: str


@app.get("/api/utilizadores")
def lista_utilizadores():
    with db.conn() as c:
        rows = c.execute("SELECT id, nome FROM utilizadores ORDER BY nome COLLATE NOCASE").fetchall()
    return [dict(r) for r in rows]


@app.post("/api/registar", status_code=201)
def registar(body: Registo, request: Request, resp: Response, background_tasks: BackgroundTasks):
    _valida_pin(body.pin)
    nome = body.nome.strip()
    with db.conn() as c:
        if c.execute("SELECT 1 FROM utilizadores WHERE nome = ?", (nome,)).fetchone():
            raise HTTPException(409, f"Já há alguém chamado «{nome}». Junta o apelido ou uma inicial.")
        cur = c.execute(
            "INSERT INTO utilizadores (nome, pin_hash, cafes_dia, criado_em) VALUES (?, ?, ?, ?)",
            (nome, _hash_pin(body.pin), body.cafes_dia, logic.agora().isoformat()),
        )
        _abre_sessao(c, cur.lastrowid, request, resp)
    _avisar(background_tasks, "registo", f"{nome} juntou-se ao café", cur.lastrowid)
    return {"id": cur.lastrowid, "nome": nome}


@app.post("/api/login")
def login(body: Login, request: Request, resp: Response):
    _valida_pin(body.pin)
    with db.conn() as c:
        u = c.execute("SELECT * FROM utilizadores WHERE id = ?", (body.utilizador_id,)).fetchone()
        if not u:
            raise HTTPException(404, "Utilizador não existe.")
        agora = logic.agora()
        if u["bloqueado_ate"] and datetime.fromisoformat(u["bloqueado_ate"]) > agora:
            raise HTTPException(429, "Demasiadas tentativas. Espera um minuto.")
        pin_certo = _verifica_pin(body.pin, u["pin_hash"])
        if pin_certo:
            c.execute("UPDATE utilizadores SET tentativas = 0, bloqueado_ate = NULL WHERE id = ?", (u["id"],))
            _abre_sessao(c, u["id"], request, resp)
        else:
            tentativas = u["tentativas"] + 1
            bloqueado = (agora + BLOQUEIO).isoformat() if tentativas >= MAX_TENTATIVAS else None
            c.execute(
                "UPDATE utilizadores SET tentativas = ?, bloqueado_ate = ? WHERE id = ?",
                (0 if bloqueado else tentativas, bloqueado, u["id"]),
            )
    # fora do `with`: o commit só acontece no caminho normal do gestor de contexto
    if not pin_certo:
        raise HTTPException(401, "PIN errado.")
    return {"id": u["id"], "nome": u["nome"]}


@app.post("/api/logout")
def logout(request: Request, resp: Response):
    token = request.cookies.get(COOKIE)
    if token:
        with db.conn() as c:
            c.execute("DELETE FROM sessoes WHERE token = ?", (token,))
    resp.delete_cookie(COOKIE)
    return {"ok": True}


# ---------- consultas partilhadas ----------

def _stock(c) -> int:
    compras = c.execute("SELECT COALESCE(SUM(capsulas), 0) AS n FROM compras").fetchone()["n"]
    cafes = c.execute("SELECT COUNT(*) AS n FROM cafes").fetchone()["n"]
    return compras - cafes


def _cafes_por_utilizador(c, mes: str) -> dict[int, int]:
    rows = c.execute(
        "SELECT utilizador_id, COUNT(*) AS n FROM cafes WHERE mes = ? GROUP BY utilizador_id", (mes,)
    ).fetchall()
    return {r["utilizador_id"]: r["n"] for r in rows}


def _resumo_stock(c, hoje) -> dict:
    """Ritmo do escritório = soma do ritmo de cada pessoa (real ou declarado)."""
    mes = hoje.strftime("%Y-%m")
    por_user = _cafes_por_utilizador(c, mes)
    ritmo = 0.0
    for u in c.execute("SELECT id, cafes_dia, criado_em FROM utilizadores").fetchall():
        decorridos = logic.dias_decorridos(mes, hoje, _data_registo(u["criado_em"]))
        ritmo += logic.ritmo_diario(por_user.get(u["id"], 0), decorridos, u["cafes_dia"])
    return logic.resumo_stock(_stock(c), ritmo, hoje, int(db.get_config(c, "stock_baixo")))


def _valor_por_utilizador(c, mes: str) -> dict[int, int]:
    rows = c.execute(
        "SELECT utilizador_id, SUM(valor_cent) AS v FROM cafes WHERE mes = ? GROUP BY utilizador_id", (mes,)
    ).fetchall()
    return {r["utilizador_id"]: r["v"] for r in rows}


def _preco(c) -> int:
    """O preço fixo das Definições, que cada café grava no momento em que o
    servidor o grava."""
    return int(db.get_config(c, "preco_cent"))


def _responsavel(c) -> dict | None:
    """Quem guarda a caixa ({"id", "nome"}), ou None se ninguém."""
    row = c.execute(
        "SELECT u.id, u.nome FROM config k JOIN utilizadores u ON u.id = CAST(k.valor AS INTEGER) "
        "WHERE k.chave = 'caixa_responsavel_id'"
    ).fetchone()
    return dict(row) if row else None


def _saldos(c) -> list[dict]:
    """Saldo corrido de cada pessoa, calculado e nunca guardado: pagamentos
    activos feitos, menos recebidos, mais compras pagas do próprio bolso,
    menos cafés. O lado da caixa de um pagamento está a NULL e não conta aqui."""
    rows = c.execute(
        """
        SELECT u.id, u.nome,
            COALESCE((SELECT SUM(valor_cent) FROM transferencias
                      WHERE pagador_id = u.id AND anulada_em IS NULL), 0)
          - COALESCE((SELECT SUM(valor_cent) FROM transferencias
                      WHERE recebedor_id = u.id AND anulada_em IS NULL), 0)
          + COALESCE((SELECT SUM(custo_cent) FROM compras
                      WHERE utilizador_id = u.id AND paga_pela_caixa = 0), 0)
          - COALESCE((SELECT SUM(valor_cent) FROM cafes WHERE utilizador_id = u.id), 0) AS saldo_cent
        FROM utilizadores u ORDER BY u.nome COLLATE NOCASE
        """
    ).fetchall()
    return [dict(r) for r in rows]


def _saldo(c, utilizador_id: int) -> int:
    return next(s["saldo_cent"] for s in _saldos(c) if s["id"] == utilizador_id)


def _saldo_caixa(c) -> int:
    """Saídas da caixa menos entradas, mais compras que ela pagou. Negativo
    quando quem a guarda tem dinheiro consigo."""
    return c.execute(
        """
        SELECT COALESCE((SELECT SUM(valor_cent) FROM transferencias
                         WHERE de_caixa = 1 AND anulada_em IS NULL), 0)
             - COALESCE((SELECT SUM(valor_cent) FROM transferencias
                         WHERE para_caixa = 1 AND anulada_em IS NULL), 0)
             + COALESCE((SELECT SUM(custo_cent) FROM compras WHERE paga_pela_caixa = 1), 0) AS v
        """
    ).fetchone()["v"]


def _fundo(c) -> int:
    """O que a oferta e a margem criaram: cafés cobrados menos compras. Por
    construção, soma dos saldos das pessoas + saldo da caixa = −fundo."""
    cobrado = c.execute("SELECT COALESCE(SUM(valor_cent), 0) AS v FROM cafes").fetchone()["v"]
    custo = c.execute("SELECT COALESCE(SUM(custo_cent), 0) AS v FROM compras").fetchone()["v"]
    return cobrado - custo


SEM_RESPONSAVEL = "Ainda não há ninguém responsável pela caixa. Escolhe nas Definições."


def _exige_responsavel(c) -> dict:
    responsavel = _responsavel(c)
    if responsavel is None:
        raise HTTPException(409, SEM_RESPONSAVEL)
    return responsavel


def _lados(t, responsavel_id: int | None) -> tuple[int | None, int | None]:
    """(lado de quem paga, lado de quem recebe) de um pagamento: quem guarda a
    caixa actua pela caixa."""
    paga = responsavel_id if t["de_caixa"] else t["pagador_id"]
    recebe = responsavel_id if t["para_caixa"] else t["recebedor_id"]
    return paga, recebe


# Campos cuja alteração marca um pagamento como `editada`; confirmar e anular não.
CAMPOS_EDITAVEIS_TRANSFERENCIA = ("valor_cent", "recebedor_id", "para_caixa")


def _editadas(c, entidade: str, campos: tuple[str, ...] | None = None) -> set[int]:
    """Ids de `entidade` com linhas no histórico (só destes campos, se dados)."""
    sql = "SELECT DISTINCT entidade_id FROM historico_alteracoes WHERE entidade = ?"
    params: list = [entidade]
    if campos:
        sql += f" AND campo IN ({', '.join('?' * len(campos))})"
        params += campos
    return {r["entidade_id"] for r in c.execute(sql, params).fetchall()}


def _euros(cent: int) -> str:
    return f"{cent / 100:.2f}".replace(".", ",")


# ---------- notificações push ----------
#
# As chaves VAPID vêm de variáveis de ambiente (nunca de código ou ficheiro):
# CAFE_VAPID_PRIVATE é o escalar privado de 32 bytes em base64url sem padding
# (43 caracteres, tudo numa linha, sem cabeçalhos PEM). CAFE_VAPID_PUBLIC é o
# ponto público X9.62 descomprimido (65 bytes) na mesma codificação
# (87 caracteres): é exactamente o valor devolvido por GET /api/push/chave e
# o que o browser usa como applicationServerKey. Sem as três variáveis
# (incluindo o contacto), a app arranca e funciona na mesma, sem push.

def _vapid_configurado() -> bool:
    return bool(
        os.environ.get("CAFE_VAPID_PRIVATE")
        and os.environ.get("CAFE_VAPID_PUBLIC")
        and os.environ.get("CAFE_VAPID_CONTACTO")
    )


def _vapid_instance() -> Vapid:
    """Reconstrói a chave EC a partir do escalar cru em base64url, em vez de
    depender de um formato PEM (que a variável de ambiente não usa)."""
    return Vapid.from_raw(os.environ["CAFE_VAPID_PRIVATE"].encode("ascii"))


def _preferencias_desligadas(c, utilizador_id: int) -> set[str]:
    rows = c.execute(
        "SELECT evento FROM notificacoes_desligadas WHERE utilizador_id = ?", (utilizador_id,)
    ).fetchall()
    return {r["evento"] for r in rows}


def _destinatarios_subscricoes(c, evento: str, autor_id: int) -> list[dict]:
    """Subscrições de quem deve receber esta notificação: nunca o autor do
    acto, nunca quem desligou este evento."""
    rows = c.execute(
        """
        SELECT s.endpoint, s.p256dh, s.auth
        FROM subscricoes s
        WHERE s.utilizador_id != ?
          AND NOT EXISTS (
              SELECT 1 FROM notificacoes_desligadas d
              WHERE d.utilizador_id = s.utilizador_id AND d.evento = ?
          )
        """,
        (autor_id, evento),
    ).fetchall()
    return [dict(r) for r in rows]


def _subscricoes_de(c, evento: str, utilizador_id: int) -> list[dict]:
    """Subscrições de uma só pessoa, se não desligou este evento."""
    rows = c.execute(
        """
        SELECT s.endpoint, s.p256dh, s.auth
        FROM subscricoes s
        WHERE s.utilizador_id = ?
          AND NOT EXISTS (
              SELECT 1 FROM notificacoes_desligadas d
              WHERE d.utilizador_id = s.utilizador_id AND d.evento = ?
          )
        """,
        (utilizador_id, evento),
    ).fetchall()
    return [dict(r) for r in rows]


def _enviar_notificacao(evento: str, mensagem: str, autor_id: int) -> None:
    """Difunde a todos menos ao autor. Corre em BackgroundTasks: nunca no
    caminho do pedido. Um push service lento não pode atrasar a resposta à
    acção que o despoletou."""
    _entregar(evento, mensagem, lambda c: _destinatarios_subscricoes(c, evento, autor_id))


def _enviar_notificacao_a(evento: str, mensagem: str, destinatario_id: int) -> None:
    """Envio dirigido a uma pessoa (pagamentos): os outros não têm nada com isso."""
    _entregar(evento, mensagem, lambda c: _subscricoes_de(c, evento, destinatario_id))


def _entregar(evento: str, mensagem: str, destinatarios_de) -> None:
    if not _vapid_configurado():
        return
    with db.conn() as c:
        destinatarios = destinatarios_de(c)
    if not destinatarios:
        return
    vv = _vapid_instance()
    titulo = TITULOS_EVENTO.get(evento, "Café do escritório")
    payload = json.dumps({"titulo": titulo, "corpo": mensagem, "url": "/", "evento": evento})
    claims_base = {"sub": os.environ["CAFE_VAPID_CONTACTO"]}
    for sub in destinatarios:
        try:
            webpush(
                subscription_info={
                    "endpoint": sub["endpoint"],
                    "keys": {"p256dh": sub["p256dh"], "auth": sub["auth"]},
                },
                data=payload,
                vapid_private_key=vv,
                vapid_claims=dict(claims_base),
            )
        except WebPushException as exc:
            if exc.status_code in (404, 410):
                with db.conn() as c:
                    c.execute("DELETE FROM subscricoes WHERE endpoint = ?", (sub["endpoint"],))


def _avisar(background_tasks: BackgroundTasks, evento: str, mensagem: str, autor_id: int) -> None:
    background_tasks.add_task(_enviar_notificacao, evento, mensagem, autor_id)


def _avisar_so(background_tasks: BackgroundTasks, evento: str, mensagem: str, destinatario_id: int) -> None:
    background_tasks.add_task(_enviar_notificacao_a, evento, mensagem, destinatario_id)


def _avisar_lado(background_tasks: BackgroundTasks, mensagem: str, lado_id: int | None, autor_id: int) -> None:
    """Push de pagamento a um lado de um pagamento: nunca ao autor, e nada se
    o lado é a caixa sem ninguém a guardá-la."""
    if lado_id is not None and lado_id != autor_id:
        _avisar_so(background_tasks, "pagamento", mensagem, lado_id)


def _dispara_stock_baixo(c, background_tasks: BackgroundTasks, autor_id: int, stock_antes: int) -> None:
    """Dispara só na transição para abaixo do limiar, ou ao chegar a zero:
    nunca a cada acção enquanto o stock já está baixo, senão repete-se até
    alguém repor."""
    limiar = int(db.get_config(c, "stock_baixo"))
    stock_depois = _stock(c)
    cruzou_o_limiar = stock_antes > limiar >= stock_depois
    chegou_a_zero = stock_antes > 0 and stock_depois == 0
    if cruzou_o_limiar or chegou_a_zero:
        _avisar(background_tasks, "stock_baixo", f"Restam {stock_depois} cápsulas", autor_id)


# ---------- a minha página ----------

@app.get("/api/eu")
def eu(u: dict = Depends(utilizador_actual)):
    hoje = _hoje()
    mes = hoje.strftime("%Y-%m")
    with db.conn() as c:
        preco = _preco(c)
        cafes = _cafes_por_utilizador(c, mes).get(u["id"], 0)
        valor = _valor_por_utilizador(c, mes).get(u["id"], 0)
        ultimo = c.execute(
            "SELECT em FROM cafes WHERE utilizador_id = ? ORDER BY em DESC LIMIT 1", (u["id"],)
        ).fetchone()
        estimativa = logic.estimativa_mes(cafes, hoje, mes, u["cafes_dia"], _data_registo(u["criado_em"]))
        saldo = _saldo(c, u["id"])
        responsavel = _responsavel(c)
        sou_responsavel = responsavel is not None and responsavel["id"] == u["id"]
        # O lado de quem recebe: eu como recebedor, ou a caixa se a guardo.
        por_confirmar = [
            {**dict(r), "para_caixa": bool(r["para_caixa"])}
            for r in c.execute(
                "SELECT t.id, t.pagador_id, COALESCE(p.nome, 'Caixa') AS pagador, t.para_caixa, "
                "t.valor_cent, t.em FROM transferencias t "
                "LEFT JOIN utilizadores p ON p.id = t.pagador_id "
                "WHERE (t.recebedor_id = :eu OR (t.para_caixa = 1 AND :responsavel)) "
                "AND t.confirmada_em IS NULL AND t.anulada_em IS NULL "
                "ORDER BY t.em DESC, t.id DESC",
                {"eu": u["id"], "responsavel": sou_responsavel},
            ).fetchall()
        ]
        caixa = None
        if responsavel:
            caixa = {"responsavel_id": responsavel["id"], "responsavel": responsavel["nome"],
                     "dinheiro_cent": -_saldo_caixa(c)}
        stock = _resumo_stock(c, hoje)
    return {
        "utilizador": {"id": u["id"], "nome": u["nome"], "cafes_dia": u["cafes_dia"]},
        "preco_cent": preco,
        "mes": mes,
        "cafes": cafes,
        "valor_cent": valor,
        "estimativa_cafes": estimativa,
        "estimativa_cent": valor + (estimativa - cafes) * preco,
        "ultimo_cafe": ultimo["em"] if ultimo else None,
        "saldo_cent": saldo,
        "sugestao": logic.sugestao_pagamento(saldo, responsavel["nome"] if responsavel else None),
        "caixa": caixa,
        "por_confirmar": por_confirmar,
        "stock": stock,
    }


@app.get("/api/historico")
def historico(mes: str | None = Query(default=None, pattern=r"^\d{4}-\d{2}$"),
              u: dict = Depends(utilizador_actual)):
    hoje = _hoje()
    mes = mes or hoje.strftime("%Y-%m")
    with db.conn() as c:
        rows = c.execute(
            "SELECT em FROM cafes WHERE utilizador_id = ? AND mes = ? ORDER BY em", (u["id"], mes)
        ).fetchall()
    por_dia: dict[str, list[str]] = {}
    for r in rows:
        dt = logic.local(datetime.fromisoformat(r["em"]))
        por_dia.setdefault(dt.strftime("%Y-%m-%d"), []).append(dt.strftime("%H:%M"))
    dias = [
        {"dia": dia, "n": len(horas), "horas": horas}
        for dia, horas in sorted(por_dia.items(), reverse=True)
    ]
    return {"mes": mes, "hoje": hoje.isoformat(), "dias": dias}


class NovoCafe(BaseModel):
    """Todos os campos opcionais: um cliente antigo em cache continua a
    funcionar sem corpo nenhum."""
    cliente_id: str | None = None
    em: str | None = None


JANELA_PASSADO = timedelta(days=7)
JANELA_FUTURO = timedelta(minutes=5)
ATRASO_SEM_NOTIFICAR = timedelta(minutes=15)


def _aceita_em(em_bruto: str | None, agora: datetime) -> datetime:
    """Regras da secção 4 da spec, por esta ordem: sem fuso ou não parseável
    conta como ausente; fora da janela [agora-7d, agora+5min] usa agora."""
    if not em_bruto:
        return agora
    try:
        em = datetime.fromisoformat(em_bruto.replace("Z", "+00:00"))
    except ValueError:
        return agora
    if em.tzinfo is None:
        return agora
    if not (agora - JANELA_PASSADO <= em <= agora + JANELA_FUTURO):
        return agora
    return em


@app.post("/api/cafe")
def marcar_cafe(background_tasks: BackgroundTasks, u: dict = Depends(utilizador_actual),
                body: NovoCafe | None = None):
    agora = logic.agora()
    cliente_id = body.cliente_id if body else None
    with db.conn() as c:
        if cliente_id:
            existente = c.execute(
                "SELECT em FROM cafes WHERE cliente_id = ?", (cliente_id,)
            ).fetchone()
            if existente:
                return JSONResponse(status_code=200, content={
                    "ok": True, "cliente_id": cliente_id, "em": existente["em"], "duplicado": True,
                })
        em = _aceita_em(body.em if body else None, agora)
        stock_antes = _stock(c)
        # O preço fica no café para sempre: é o do momento em que o servidor o grava.
        preco = _preco(c)
        try:
            c.execute(
                "INSERT INTO cafes (utilizador_id, em, mes, cliente_id, valor_cent) VALUES (?, ?, ?, ?, ?)",
                (u["id"], em.isoformat(), logic.mes_de(em), cliente_id, preco),
            )
        except sqlite3.IntegrityError as exc:
            # Duas requisições com o mesmo cliente_id podem intercalar-se entre
            # o SELECT de deteção acima e este INSERT (retry de uma ligação
            # instável, exactamente o caso que a idempotência existe para
            # cobrir). Quem perde a corrida do INSERT lê a linha que a outra
            # gravou e devolve o mesmo 200/duplicado, nunca um 500. Qualquer
            # outra violação de integridade continua a propagar-se.
            if cliente_id and "cafes.cliente_id" in str(exc):
                existente = c.execute(
                    "SELECT em FROM cafes WHERE cliente_id = ?", (cliente_id,)
                ).fetchone()
                return JSONResponse(status_code=200, content={
                    "ok": True, "cliente_id": cliente_id, "em": existente["em"], "duplicado": True,
                })
            raise
        _dispara_stock_baixo(c, background_tasks, u["id"], stock_antes)
    if agora - em <= ATRASO_SEM_NOTIFICAR:
        _avisar(background_tasks, "cafe", f"{u['nome']} bebeu um café", u["id"])
    return JSONResponse(status_code=201, content={
        "ok": True, "cliente_id": cliente_id, "em": em.isoformat(), "duplicado": False,
    })


@app.delete("/api/cafe/ultimo")
def desfazer_cafe(u: dict = Depends(utilizador_actual)):
    with db.conn() as c:
        ultimo = c.execute(
            "SELECT id FROM cafes WHERE utilizador_id = ? ORDER BY id DESC LIMIT 1", (u["id"],)
        ).fetchone()
        if not ultimo:
            raise HTTPException(404, "Não há café para desfazer.")
        c.execute("DELETE FROM cafes WHERE id = ?", (ultimo["id"],))
    return {"ok": True}


class Previsao(BaseModel):
    cafes_dia: float = Field(ge=0, le=20)


@app.put("/api/eu/previsao")
def alterar_previsao(body: Previsao, u: dict = Depends(utilizador_actual)):
    with db.conn() as c:
        c.execute("UPDATE utilizadores SET cafes_dia = ? WHERE id = ?", (body.cafes_dia, u["id"]))
    return {"ok": True}


class NovoPin(BaseModel):
    pin_actual: str
    pin_novo: str


@app.put("/api/eu/pin")
def alterar_pin(body: NovoPin, u: dict = Depends(utilizador_actual)):
    _valida_pin(body.pin_novo)
    if not _verifica_pin(body.pin_actual, u["pin_hash"]):
        raise HTTPException(401, "PIN actual errado.")
    with db.conn() as c:
        c.execute("UPDATE utilizadores SET pin_hash = ? WHERE id = ?", (_hash_pin(body.pin_novo), u["id"]))
    return {"ok": True}


# ---------- escritório ----------

@app.get("/api/escritorio")
def escritorio(mes: str | None = None, u: dict = Depends(utilizador_actual)):
    hoje = _hoje()
    mes_actual = hoje.strftime("%Y-%m")
    mes = mes or mes_actual
    with db.conn() as c:
        preco = _preco(c)
        por_user = _cafes_por_utilizador(c, mes)
        valor_por_user = _valor_por_utilizador(c, mes)
        pessoas = [
            {
                "id": s["id"], "nome": s["nome"], "cafes": por_user.get(s["id"], 0),
                "valor_cent": valor_por_user.get(s["id"], 0), "saldo_cent": s["saldo_cent"],
            }
            for s in _saldos(c)
        ]
        meses = [r["mes"] for r in c.execute("SELECT DISTINCT mes FROM cafes ORDER BY mes DESC").fetchall()]
        if mes_actual not in meses:
            meses.insert(0, mes_actual)
        editadas = _editadas(c, "compra")
        compras = [
            {
                **dict(r),
                "custo_estimado": bool(r["custo_estimado"]),
                "paga_pela_caixa": bool(r["paga_pela_caixa"]),
                "editada": r["id"] in editadas,
                "pode_editar": r["utilizador_id"] is None or r["utilizador_id"] == u["id"],
            }
            for r in c.execute(
                "SELECT co.id, co.capsulas, co.custo_cent, co.custo_estimado, co.paga_pela_caixa, co.em, "
                "co.nota, co.utilizador_id, u.nome FROM compras co "
                "LEFT JOIN utilizadores u ON u.id = co.utilizador_id ORDER BY co.id DESC LIMIT 20"
            ).fetchall()
        ]
        responsavel = _responsavel(c)
        caixa = {
            "responsavel_id": responsavel["id"] if responsavel else None,
            "responsavel": responsavel["nome"] if responsavel else None,
            "dinheiro_cent": -_saldo_caixa(c),
            "por_receber_cent": -sum(p["saldo_cent"] for p in pessoas if p["saldo_cent"] < 0),
            "fundo_cent": _fundo(c),
        }
        stock = _resumo_stock(c, hoje)
    total = sum(p["cafes"] for p in pessoas)
    return {
        "eu": u["id"],
        "mes": mes,
        "mes_actual": mes_actual,
        "meses": meses,
        "preco_cent": preco,
        "pessoas": pessoas,
        "total_cafes": total,
        "total_cent": sum(p["valor_cent"] for p in pessoas),
        "caixa": caixa,
        "stock": stock,
        "compras": compras,
    }


class Compra(BaseModel):
    capsulas: int = Field(ge=1, le=10_000)
    custo_cent: int = Field(ge=0, le=1_000_000)  # 0 = oferta
    paga_pela_caixa: bool = False
    nota: str | None = Field(default=None, max_length=80)


@app.post("/api/compras", status_code=201)
def registar_compra(body: Compra, background_tasks: BackgroundTasks, u: dict = Depends(utilizador_actual)):
    # Uma oferta não sai de lado nenhum.
    paga_pela_caixa = body.paga_pela_caixa and body.custo_cent > 0
    with db.conn() as c:
        if paga_pela_caixa:
            _exige_responsavel(c)
        c.execute(
            "INSERT INTO compras (utilizador_id, capsulas, custo_cent, paga_pela_caixa, em, nota) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (u["id"], body.capsulas, body.custo_cent, paga_pela_caixa, logic.agora().isoformat(), body.nota),
        )
    _avisar(
        background_tasks, "compra",
        f"{u['nome']} repôs {body.capsulas} cápsulas ({_euros(body.custo_cent)} €)", u["id"],
    )
    return {"ok": True}


class AlteracaoCompra(BaseModel):
    custo_cent: int | None = Field(default=None, ge=0, le=1_000_000)
    capsulas: int | None = Field(default=None, ge=1, le=10_000)
    paga_pela_caixa: bool | None = None


@app.patch("/api/compras/{compra_id}")
def alterar_compra(compra_id: int, body: AlteracaoCompra, u: dict = Depends(utilizador_actual)):
    """Quem registou a compra (ou qualquer pessoa, se não tem autor) corrige o
    custo, as cápsulas ou quem pagou; cada campo alterado fica no histórico.
    Um custo corrigido deixa de ser estimado, e um custo 0 é uma oferta, que
    nunca é paga pela caixa."""
    with db.conn() as c:
        compra = c.execute("SELECT * FROM compras WHERE id = ?", (compra_id,)).fetchone()
        if not compra:
            raise HTTPException(404, "Entrada não existe.")
        if compra["utilizador_id"] is not None and compra["utilizador_id"] != u["id"]:
            raise HTTPException(403, "Só quem registou a entrada a pode alterar.")
        novo = {
            "custo_cent": compra["custo_cent"] if body.custo_cent is None else body.custo_cent,
            "capsulas": compra["capsulas"] if body.capsulas is None else body.capsulas,
            "paga_pela_caixa": bool(compra["paga_pela_caixa"]) if body.paga_pela_caixa is None
            else body.paga_pela_caixa,
        }
        novo["paga_pela_caixa"] = novo["paga_pela_caixa"] and novo["custo_cent"] > 0
        if _stock(c) - compra["capsulas"] + novo["capsulas"] < 0:
            raise HTTPException(409, "Não se pode: o stock ficaria negativo.")
        if novo["paga_pela_caixa"] and not compra["paga_pela_caixa"]:
            _exige_responsavel(c)
        for campo in ("custo_cent", "capsulas", "paga_pela_caixa"):
            antes = bool(compra[campo]) if campo == "paga_pela_caixa" else compra[campo]
            if novo[campo] != antes:
                db.regista_alteracao(c, "compra", compra_id, campo, antes, novo[campo], u["id"])
        c.execute(
            "UPDATE compras SET custo_cent = ?, capsulas = ?, paga_pela_caixa = ?, "
            "custo_estimado = CASE WHEN ? THEN 0 ELSE custo_estimado END WHERE id = ?",
            (novo["custo_cent"], novo["capsulas"], novo["paga_pela_caixa"], body.custo_cent is not None,
             compra_id),
        )
    return {"ok": True}


@app.delete("/api/compras/{compra_id}")
def apagar_compra(compra_id: int, background_tasks: BackgroundTasks, u: dict = Depends(utilizador_actual)):
    with db.conn() as c:
        compra = c.execute("SELECT * FROM compras WHERE id = ?", (compra_id,)).fetchone()
        if not compra:
            raise HTTPException(404, "Entrada não existe.")
        if compra["utilizador_id"] != u["id"]:
            raise HTTPException(403, "Só quem registou a entrada a pode apagar.")
        stock_antes = _stock(c)
        if stock_antes - compra["capsulas"] < 0:
            raise HTTPException(409, "Não se pode apagar: o stock ficaria negativo.")
        c.execute("DELETE FROM compras WHERE id = ?", (compra_id,))
        _dispara_stock_baixo(c, background_tasks, u["id"], stock_antes)
    return {"ok": True}


# ---------- pagamentos por MB WAY ----------

@app.post("/api/pagamentos", include_in_schema=False)
@app.delete("/api/pagamentos/{mes}/{pagador_id}", include_in_schema=False)
def pagamentos_mensais_acabaram():
    """Um cliente antigo em cache ainda chama estas rotas: um 410 com uma frase
    que se perceba, em vez de um 404 ou de um 422 por causa do corpo."""
    raise HTTPException(410, "Os pagamentos mensais acabaram. Actualiza a app.")


class NovaTransferencia(BaseModel):
    recebedor_id: int | None = None
    para_caixa: bool = False
    de_caixa: bool = False
    valor_cent: int = Field(ge=1, le=100_000)


def _destino_valido(pagador_id: int | None, recebedor_id: int | None, para_caixa: bool, de_caixa: bool) -> None:
    """As combinações da secção 3.4: nunca as duas pontas na caixa; de_caixa
    exige recebedor; para_caixa não o tem; entre duas pessoas, pessoas
    diferentes."""
    if para_caixa and de_caixa:
        raise HTTPException(400, "Um pagamento não entra e sai da caixa ao mesmo tempo.")
    if para_caixa and recebedor_id is not None:
        raise HTTPException(400, "Um pagamento à caixa não tem recebedor.")
    if not para_caixa and recebedor_id is None:
        raise HTTPException(400, "Falta a quem foi o pagamento.")
    if not (para_caixa or de_caixa) and recebedor_id == pagador_id:
        raise HTTPException(400, "Não se paga a si próprio.")


def _existe_utilizador(c, utilizador_id: int) -> None:
    if not c.execute("SELECT 1 FROM utilizadores WHERE id = ?", (utilizador_id,)).fetchone():
        raise HTTPException(404, "Essa pessoa não existe.")


@app.post("/api/transferencias", status_code=201)
def registar_transferencia(body: NovaTransferencia, background_tasks: BackgroundTasks,
                           u: dict = Depends(utilizador_actual)):
    """Quem está autenticado é quem pagou, ou a caixa se `de_caixa` (só quem a
    guarda). Conta para o saldo desde já; se quem paga e quem recebe são a
    mesma pessoa (quem guarda a caixa), fica logo confirmado."""
    pagador_id = None if body.de_caixa else u["id"]
    _destino_valido(pagador_id, body.recebedor_id, body.para_caixa, body.de_caixa)
    em = logic.agora().isoformat()
    with db.conn() as c:
        responsavel_id = None
        if body.para_caixa or body.de_caixa:
            responsavel_id = _exige_responsavel(c)["id"]
            if body.de_caixa and responsavel_id != u["id"]:
                raise HTTPException(403, "Só quem guarda a caixa regista saídas da caixa.")
        if body.recebedor_id is not None:
            _existe_utilizador(c, body.recebedor_id)
        linha = {"pagador_id": pagador_id, "recebedor_id": body.recebedor_id,
                 "para_caixa": body.para_caixa, "de_caixa": body.de_caixa}
        paga, recebe = _lados(linha, responsavel_id)
        cur = c.execute(
            "INSERT INTO transferencias (pagador_id, recebedor_id, para_caixa, de_caixa, valor_cent, em, "
            "confirmada_em) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (pagador_id, body.recebedor_id, body.para_caixa, body.de_caixa, body.valor_cent, em,
             em if paga == recebe else None),
        )
    valor = _euros(body.valor_cent)
    if body.para_caixa:
        corpo = f"{u['nome']} registou {valor} € pagos à caixa"
    elif body.de_caixa:
        corpo = f"{u['nome']} registou {valor} € da caixa para ti"
    else:
        corpo = f"{u['nome']} registou {valor} € pagos a ti"
    _avisar_lado(background_tasks, corpo, recebe, u["id"])
    return {
        "id": cur.lastrowid, "pagador_id": pagador_id, "recebedor_id": body.recebedor_id,
        "para_caixa": body.para_caixa, "de_caixa": body.de_caixa, "valor_cent": body.valor_cent, "em": em,
    }


def _transferencia(c, transferencia_id: int) -> tuple[dict, int | None, int | None]:
    """A linha e os seus dois lados (quem guarda a caixa actua pela caixa)."""
    t = c.execute("SELECT * FROM transferencias WHERE id = ?", (transferencia_id,)).fetchone()
    if not t:
        raise HTTPException(404, "Pagamento não existe.")
    responsavel = _responsavel(c)
    paga, recebe = _lados(t, responsavel["id"] if responsavel else None)
    return dict(t), paga, recebe


def _exige_aberta(t: dict) -> None:
    if t["confirmada_em"] or t["anulada_em"]:
        raise HTTPException(409, "Esse pagamento já foi confirmado ou anulado.")


@app.post("/api/transferencias/{transferencia_id}/confirmar")
def confirmar_transferencia(transferencia_id: int, u: dict = Depends(utilizador_actual)):
    """Só o lado de quem recebe confirma."""
    with db.conn() as c:
        t, _, recebe = _transferencia(c, transferencia_id)
        if u["id"] != recebe:
            raise HTTPException(403, "Esse pagamento não é contigo.")
        _exige_aberta(t)
        cur = c.execute(
            "UPDATE transferencias SET confirmada_em = ? "
            "WHERE id = ? AND confirmada_em IS NULL AND anulada_em IS NULL",
            (logic.agora().isoformat(), transferencia_id),
        )
        if cur.rowcount != 1:
            raise HTTPException(409, "Esse pagamento já foi confirmado ou anulado.")
        db.regista_alteracao(c, "transferencia", transferencia_id, "confirmada", False, True, u["id"])
    return {"ok": True}


@app.post("/api/transferencias/{transferencia_id}/anular")
def anular_transferencia(transferencia_id: int, background_tasks: BackgroundTasks,
                         u: dict = Depends(utilizador_actual)):
    """O lado de quem paga anula um engano; o de quem recebe diz "Não
    recebi". Avisa-se só o outro lado."""
    with db.conn() as c:
        t, paga, recebe = _transferencia(c, transferencia_id)
        if u["id"] not in (paga, recebe):
            raise HTTPException(403, "Esse pagamento não é contigo.")
        _exige_aberta(t)
        cur = c.execute(
            "UPDATE transferencias SET anulada_em = ?, anulada_por = ? "
            "WHERE id = ? AND confirmada_em IS NULL AND anulada_em IS NULL",
            (logic.agora().isoformat(), u["id"], transferencia_id),
        )
        if cur.rowcount != 1:
            raise HTTPException(409, "Esse pagamento já foi confirmado ou anulado.")
        db.regista_alteracao(c, "transferencia", transferencia_id, "anulada", False, True, u["id"])
    valor = _euros(t["valor_cent"])
    if u["id"] == paga:
        _avisar_lado(background_tasks, f"{u['nome']} anulou o pagamento de {valor} €", recebe, u["id"])
    else:
        _avisar_lado(background_tasks, f"{u['nome']} disse que não recebeu os {valor} €", paga, u["id"])
    return {"ok": True}


class AlteracaoTransferencia(BaseModel):
    """Qualquer subconjunto; `recebedor_id: null` explícito é diferente de
    omitido (passar para a caixa), por isso lê-se model_fields_set."""
    valor_cent: int = Field(default=None, ge=1, le=100_000)
    recebedor_id: int | None = None
    para_caixa: bool = Field(default=None)


@app.patch("/api/transferencias/{transferencia_id}")
def alterar_transferencia(transferencia_id: int, body: AlteracaoTransferencia, background_tasks: BackgroundTasks,
                          u: dict = Depends(utilizador_actual)):
    """O lado de quem paga muda o valor e/ou o destino enquanto o pagamento
    está activo. Um pagamento confirmado volta a por confirmar; se no fim
    quem paga e quem recebe são a mesma pessoa, fica confirmado."""
    enviados = body.model_fields_set
    with db.conn() as c:
        t, paga, recebe_antes = _transferencia(c, transferencia_id)
        if u["id"] != paga:
            raise HTTPException(403, "Só quem pagou altera o pagamento.")
        if t["anulada_em"]:
            raise HTTPException(409, "Esse pagamento foi anulado.")
        novo = {
            "valor_cent": body.valor_cent if "valor_cent" in enviados else t["valor_cent"],
            "recebedor_id": body.recebedor_id if "recebedor_id" in enviados else t["recebedor_id"],
            "para_caixa": body.para_caixa if "para_caixa" in enviados else bool(t["para_caixa"]),
        }
        _destino_valido(t["pagador_id"], novo["recebedor_id"], novo["para_caixa"], bool(t["de_caixa"]))
        responsavel = _responsavel(c)
        if novo["para_caixa"] and not t["para_caixa"] and responsavel is None:
            raise HTTPException(409, SEM_RESPONSAVEL)
        if novo["recebedor_id"] is not None and novo["recebedor_id"] != t["recebedor_id"]:
            _existe_utilizador(c, novo["recebedor_id"])
        antes = {"valor_cent": t["valor_cent"], "recebedor_id": t["recebedor_id"],
                 "para_caixa": bool(t["para_caixa"])}
        mudou = [campo for campo in CAMPOS_EDITAVEIS_TRANSFERENCIA if novo[campo] != antes[campo]]
        if not mudou:
            raise HTTPException(400, "Nada mudou.")
        for campo in mudou:
            db.regista_alteracao(c, "transferencia", transferencia_id, campo, antes[campo], novo[campo], u["id"])
        _, recebe = _lados({**t, **novo}, responsavel["id"] if responsavel else None)
        confirmada_em = t["confirmada_em"]
        if paga == recebe:
            if not confirmada_em:
                confirmada_em = logic.agora().isoformat()
                db.regista_alteracao(c, "transferencia", transferencia_id, "confirmada", False, True, u["id"])
        elif confirmada_em:
            confirmada_em = None
            db.regista_alteracao(c, "transferencia", transferencia_id, "confirmada", True, False, u["id"])
        c.execute(
            "UPDATE transferencias SET valor_cent = ?, recebedor_id = ?, para_caixa = ?, confirmada_em = ? "
            "WHERE id = ?",
            (novo["valor_cent"], novo["recebedor_id"], novo["para_caixa"], confirmada_em, transferencia_id),
        )
    corpo = f"{u['nome']} alterou um pagamento: {_euros(t['valor_cent'])} € → {_euros(novo['valor_cent'])} €"
    for destinatario in dict.fromkeys((recebe, recebe_antes)):
        _avisar_lado(background_tasks, corpo, destinatario, u["id"])
    return {"ok": True}


@app.get("/api/transferencias")
def lista_transferencias(u: dict = Depends(utilizador_actual)):
    """Os pagamentos de toda a gente, mais recentes primeiro, com o que quem
    pede pode fazer a cada um."""
    with db.conn() as c:
        responsavel = _responsavel(c)
        responsavel_id = responsavel["id"] if responsavel else None
        editadas = _editadas(c, "transferencia", CAMPOS_EDITAVEIS_TRANSFERENCIA)
        rows = c.execute(
            "SELECT t.*, COALESCE(p.nome, 'Caixa') AS pagador, COALESCE(r.nome, 'Caixa') AS recebedor "
            "FROM transferencias t "
            "LEFT JOIN utilizadores p ON p.id = t.pagador_id "
            "LEFT JOIN utilizadores r ON r.id = t.recebedor_id "
            "ORDER BY t.em DESC, t.id DESC LIMIT 200"
        ).fetchall()
    lista = []
    for t in rows:
        paga, recebe = _lados(t, responsavel_id)
        activa = t["anulada_em"] is None
        aberta = activa and t["confirmada_em"] is None
        lista.append({
            "id": t["id"], "pagador_id": t["pagador_id"], "pagador": t["pagador"],
            "recebedor_id": t["recebedor_id"], "recebedor": t["recebedor"],
            "para_caixa": bool(t["para_caixa"]), "de_caixa": bool(t["de_caixa"]),
            "valor_cent": t["valor_cent"], "em": t["em"], "confirmada_em": t["confirmada_em"],
            "anulada_em": t["anulada_em"], "anulada_por": t["anulada_por"],
            "editada": t["id"] in editadas,
            "pode_editar": activa and u["id"] == paga,
            "pode_confirmar": aberta and u["id"] == recebe,
            "pode_anular": aberta and u["id"] in (paga, recebe),
        })
    return {"transferencias": lista}


@app.get("/api/historico-alteracoes")
def historico_alteracoes(entidade: str = Query(pattern="^(transferencia|compra|config|cafe)$"),
                         entidade_id: int | None = Query(default=None, alias="id"),
                         u: dict = Depends(utilizador_actual)):
    """O rasto de uma linha (ou das Definições), mais antigas primeiro."""
    if entidade != "config" and entidade_id is None:
        raise HTTPException(422, "Falta o id.")
    with db.conn() as c:
        rows = c.execute(
            "SELECT h.campo, h.antes, h.depois, u.nome AS utilizador, h.em FROM historico_alteracoes h "
            "LEFT JOIN utilizadores u ON u.id = h.utilizador_id "
            "WHERE h.entidade = ? AND h.entidade_id IS ? ORDER BY h.em, h.id",
            (entidade, None if entidade == "config" else entidade_id),
        ).fetchall()
    return {"alteracoes": [dict(r) for r in rows]}


@app.get("/api/movimentos")
def movimentos(u: dict = Depends(utilizador_actual)):
    """Os meus movimentos em dinheiro, do mais recente para o mais antigo. Os
    pagamentos à caixa que guardo são da caixa, não meus."""
    with db.conn() as c:
        saldo = _saldo(c, u["id"])
        editadas = _editadas(c, "transferencia", CAMPOS_EDITAVEIS_TRANSFERENCIA)
        transferencias = [
            {**dict(r), "para_caixa": bool(r["para_caixa"]), "de_caixa": bool(r["de_caixa"]),
             "editada": r["id"] in editadas}
            for r in c.execute(
                "SELECT t.id, CASE WHEN t.pagador_id = :eu THEN 'paguei' ELSE 'recebi' END AS sentido, "
                "o.id AS outro_id, COALESCE(o.nome, 'Caixa') AS outro, t.valor_cent, t.em, t.confirmada_em, "
                "t.anulada_em, t.anulada_por, t.para_caixa, t.de_caixa FROM transferencias t "
                "LEFT JOIN utilizadores o "
                "ON o.id = CASE WHEN t.pagador_id = :eu THEN t.recebedor_id ELSE t.pagador_id END "
                "WHERE t.pagador_id = :eu OR t.recebedor_id = :eu ORDER BY t.em DESC, t.id DESC",
                {"eu": u["id"]},
            ).fetchall()
        ]
        compras_editadas = _editadas(c, "compra")
        compras = [
            {**dict(r), "custo_estimado": bool(r["custo_estimado"]),
             "paga_pela_caixa": bool(r["paga_pela_caixa"]), "editada": r["id"] in compras_editadas}
            for r in c.execute(
                "SELECT id, capsulas, custo_cent, custo_estimado, paga_pela_caixa, em FROM compras "
                "WHERE utilizador_id = ? ORDER BY em DESC, id DESC",
                (u["id"],),
            ).fetchall()
        ]
        meses = [dict(r) for r in c.execute(
            "SELECT mes, COUNT(*) AS cafes, SUM(valor_cent) AS valor_cent FROM cafes "
            "WHERE utilizador_id = ? GROUP BY mes ORDER BY mes DESC",
            (u["id"],),
        ).fetchall()]
    return {"saldo_cent": saldo, "transferencias": transferencias, "compras": compras, "meses": meses}


class Config(BaseModel):
    """Qualquer subconjunto. `caixa_responsavel_id: null` tira o responsável,
    por isso lê-se model_fields_set. O preço nunca é 0: a migração repara os
    cafés gravados a 0 em cada arranque."""
    preco_cent: int = Field(default=None, ge=1, le=10_000)
    stock_baixo: int = Field(default=None, ge=0, le=10_000)
    caixa_responsavel_id: int | None = None


def _config(c) -> dict:
    responsavel = c.execute("SELECT valor FROM config WHERE chave = 'caixa_responsavel_id'").fetchone()
    return {
        "preco_cent": _preco(c),
        "stock_baixo": int(db.get_config(c, "stock_baixo")),
        "caixa_responsavel_id": int(responsavel["valor"]) if responsavel else None,
    }


@app.get("/api/config")
def obter_config(u: dict = Depends(utilizador_actual)):
    with db.conn() as c:
        return {**_config(c), "editada": bool(_editadas(c, "config"))}


@app.put("/api/config")
def alterar_config(body: Config, u: dict = Depends(utilizador_actual)):
    """Qualquer pessoa. Cada chave que muda de valor fica no histórico."""
    enviados = body.model_fields_set
    with db.conn() as c:
        if body.caixa_responsavel_id is not None:
            _existe_utilizador(c, body.caixa_responsavel_id)
        antes = _config(c)
        for chave in ("preco_cent", "stock_baixo", "caixa_responsavel_id"):
            novo = getattr(body, chave)
            if chave not in enviados or novo == antes[chave]:
                continue
            if novo is None:
                c.execute("DELETE FROM config WHERE chave = ?", (chave,))
            else:
                db.set_config(c, chave, str(novo))
            db.regista_alteracao(c, "config", None, chave, antes[chave], novo, u["id"])
    return {"ok": True}


# ---------- push e notificações ----------

@app.get("/api/push/chave")
def chave_push():
    chave = os.environ.get("CAFE_VAPID_PUBLIC")
    if not chave:
        raise HTTPException(503, "As notificações push não estão configuradas neste servidor.")
    return {"chave_publica": chave}


class Subscricao(BaseModel):
    endpoint: str
    p256dh: str
    auth: str
    dispositivo: str | None = Field(default=None, max_length=200)


@app.post("/api/push/subscricoes", status_code=201)
def subscrever_push(body: Subscricao, u: dict = Depends(utilizador_actual)):
    with db.conn() as c:
        c.execute(
            "INSERT INTO subscricoes (endpoint, utilizador_id, p256dh, auth, dispositivo, criado_em) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(endpoint) DO UPDATE SET utilizador_id = excluded.utilizador_id, "
            "p256dh = excluded.p256dh, auth = excluded.auth, dispositivo = excluded.dispositivo, "
            "criado_em = excluded.criado_em",
            (body.endpoint, u["id"], body.p256dh, body.auth, body.dispositivo, logic.agora().isoformat()),
        )
    return {"ok": True}


class CancelarSubscricao(BaseModel):
    endpoint: str


@app.delete("/api/push/subscricoes")
def cancelar_subscricao(body: CancelarSubscricao, u: dict = Depends(utilizador_actual)):
    with db.conn() as c:
        c.execute(
            "DELETE FROM subscricoes WHERE endpoint = ? AND utilizador_id = ?", (body.endpoint, u["id"])
        )
    return {"ok": True}


@app.get("/api/notificacoes/preferencias")
def obter_preferencias(u: dict = Depends(utilizador_actual)):
    with db.conn() as c:
        desligados = _preferencias_desligadas(c, u["id"])
    return {evento: evento not in desligados for evento in EVENTOS}


# Same shape GET returns: {evento: ligado}. The client always sends the
# full form state (every toggle, current checked value), so a missing key
# is never ambiguous here, it simply never occurs in practice; if it did,
# that event would be treated as ligado, matching the GET default.
@app.put("/api/notificacoes/preferencias")
def alterar_preferencias(body: dict[str, bool], u: dict = Depends(utilizador_actual)):
    invalidos = sorted(set(body) - set(EVENTOS))
    if invalidos:
        raise HTTPException(400, f"Evento(s) desconhecido(s): {', '.join(invalidos)}.")
    desligados = [evento for evento, ligado in body.items() if not ligado]
    with db.conn() as c:
        c.execute("DELETE FROM notificacoes_desligadas WHERE utilizador_id = ?", (u["id"],))
        c.executemany(
            "INSERT INTO notificacoes_desligadas (utilizador_id, evento) VALUES (?, ?)",
            [(u["id"], evento) for evento in desligados],
        )
    return {"ok": True}


# ---------- front-end ----------

@app.get("/", include_in_schema=False)
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/sw.js", include_in_schema=False)
def service_worker():
    # Served at the root, not under /static, so its default scope covers the
    # whole app (including "/"), not just /static/.
    return FileResponse(STATIC / "sw.js", media_type="application/javascript")


app.mount("/static", StaticFiles(directory=STATIC), name="static")
