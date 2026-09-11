"""API do café do escritório. Uma app FastAPI que também serve o front-end estático."""
import hashlib
import secrets
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import db, logic

MAX_TENTATIVAS = 5
BLOQUEIO = timedelta(seconds=60)
COOKIE = "cafe_sessao"
SESSAO_DIAS = 180
STATIC = Path(__file__).parent / "static"



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
def registar(body: Registo, request: Request, resp: Response):
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


def _pagamento(c, mes: str, pagador_id: int) -> dict | None:
    row = c.execute(
        "SELECT p.*, r.nome AS recebedor FROM pagamentos p JOIN utilizadores r ON r.id = p.recebedor_id "
        "WHERE p.mes = ? AND p.pagador_id = ?",
        (mes, pagador_id),
    ).fetchone()
    return dict(row) if row else None


# ---------- a minha página ----------

@app.get("/api/eu")
def eu(u: dict = Depends(utilizador_actual)):
    hoje = _hoje()
    mes = hoje.strftime("%Y-%m")
    anterior = logic.mes_anterior(mes)
    with db.conn() as c:
        preco = int(db.get_config(c, "preco_cent"))
        cafes = _cafes_por_utilizador(c, mes).get(u["id"], 0)
        cafes_ant = _cafes_por_utilizador(c, anterior).get(u["id"], 0)
        ultimo = c.execute(
            "SELECT em FROM cafes WHERE utilizador_id = ? ORDER BY id DESC LIMIT 1", (u["id"],)
        ).fetchone()
        estimativa = logic.estimativa_mes(cafes, hoje, mes, u["cafes_dia"], _data_registo(u["criado_em"]))
        pag = _pagamento(c, anterior, u["id"])
        stock = _resumo_stock(c, hoje)
    return {
        "utilizador": {"id": u["id"], "nome": u["nome"], "cafes_dia": u["cafes_dia"]},
        "preco_cent": preco,
        "mes": mes,
        "cafes": cafes,
        "valor_cent": cafes * preco,
        "estimativa_cafes": estimativa,
        "estimativa_cent": estimativa * preco,
        "ultimo_cafe": ultimo["em"] if ultimo else None,
        "mes_anterior": {
            "mes": anterior,
            "cafes": cafes_ant,
            "valor_cent": pag["valor_cent"] if pag else cafes_ant * preco,
            "pago": pag is not None,
            "pagamento": pag,
        },
        "stock": stock,
    }


@app.post("/api/cafe", status_code=201)
def marcar_cafe(u: dict = Depends(utilizador_actual)):
    agora = logic.agora()
    with db.conn() as c:
        c.execute(
            "INSERT INTO cafes (utilizador_id, em, mes) VALUES (?, ?, ?)",
            (u["id"], agora.isoformat(), logic.mes_de(agora)),
        )
    return {"ok": True}


@app.delete("/api/cafe/ultimo")
def desfazer_cafe(u: dict = Depends(utilizador_actual)):
    with db.conn() as c:
        ultimo = c.execute(
            "SELECT id, mes FROM cafes WHERE utilizador_id = ? ORDER BY id DESC LIMIT 1", (u["id"],)
        ).fetchone()
        if not ultimo:
            raise HTTPException(404, "Não há café para desfazer.")
        if c.execute("SELECT 1 FROM pagamentos WHERE mes = ?", (ultimo["mes"],)).fetchone():
            raise HTTPException(409, "Esse mês já tem pagamentos registados; já não se pode alterar.")
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
        preco = int(db.get_config(c, "preco_cent"))
        por_user = _cafes_por_utilizador(c, mes)
        pessoas = []
        for r in c.execute("SELECT id, nome FROM utilizadores ORDER BY nome COLLATE NOCASE").fetchall():
            n = por_user.get(r["id"], 0)
            pag = _pagamento(c, mes, r["id"])
            pessoas.append({
                "id": r["id"], "nome": r["nome"], "cafes": n,
                "valor_cent": pag["valor_cent"] if pag else n * preco,
                "pago": pag is not None, "pagamento": pag,
            })
        meses = [r["mes"] for r in c.execute("SELECT DISTINCT mes FROM cafes ORDER BY mes DESC").fetchall()]
        if mes_actual not in meses:
            meses.insert(0, mes_actual)
        compras = [dict(r) for r in c.execute(
            "SELECT co.id, co.capsulas, co.em, co.nota, co.utilizador_id, u.nome FROM compras co "
            "LEFT JOIN utilizadores u ON u.id = co.utilizador_id ORDER BY co.id DESC LIMIT 20"
        ).fetchall()]
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
        "stock": stock,
        "compras": compras,
    }


class Compra(BaseModel):
    capsulas: int = Field(ge=1, le=10_000)
    nota: str | None = Field(default=None, max_length=80)


@app.post("/api/compras", status_code=201)
def registar_compra(body: Compra, u: dict = Depends(utilizador_actual)):
    with db.conn() as c:
        c.execute(
            "INSERT INTO compras (utilizador_id, capsulas, em, nota) VALUES (?, ?, ?, ?)",
            (u["id"], body.capsulas, logic.agora().isoformat(), body.nota),
        )
    return {"ok": True}


@app.delete("/api/compras/{compra_id}")
def apagar_compra(compra_id: int, u: dict = Depends(utilizador_actual)):
    with db.conn() as c:
        compra = c.execute("SELECT * FROM compras WHERE id = ?", (compra_id,)).fetchone()
        if not compra:
            raise HTTPException(404, "Entrada não existe.")
        if compra["utilizador_id"] != u["id"]:
            raise HTTPException(403, "Só quem registou a entrada a pode apagar.")
        if _stock(c) - compra["capsulas"] < 0:
            raise HTTPException(409, "Não se pode apagar: o stock ficaria negativo.")
        c.execute("DELETE FROM compras WHERE id = ?", (compra_id,))
    return {"ok": True}


class Pagamento(BaseModel):
    mes: str = Field(pattern=r"^\d{4}-\d{2}$")
    pagador_id: int


@app.post("/api/pagamentos", status_code=201)
def registar_pagamento(body: Pagamento, u: dict = Depends(utilizador_actual)):
    """Quem está autenticado é quem recebeu o dinheiro."""
    mes_actual = logic.mes_de(logic.agora())
    if body.mes >= mes_actual:
        raise HTTPException(400, "Só se fecham meses já terminados.")
    with db.conn() as c:
        if not c.execute("SELECT 1 FROM utilizadores WHERE id = ?", (body.pagador_id,)).fetchone():
            raise HTTPException(404, "Pagador não existe.")
        if _pagamento(c, body.mes, body.pagador_id):
            raise HTTPException(409, "Esse pagamento já está registado.")
        preco = int(db.get_config(c, "preco_cent"))
        n = _cafes_por_utilizador(c, body.mes).get(body.pagador_id, 0)
        c.execute(
            "INSERT INTO pagamentos (mes, pagador_id, recebedor_id, capsulas, valor_cent, em) VALUES (?, ?, ?, ?, ?, ?)",
            (body.mes, body.pagador_id, u["id"], n, n * preco, logic.agora().isoformat()),
        )
    return {"ok": True, "capsulas": n, "valor_cent": n * preco}


@app.delete("/api/pagamentos/{mes}/{pagador_id}")
def anular_pagamento(mes: str, pagador_id: int, u: dict = Depends(utilizador_actual)):
    """Só quem recebeu pode anular."""
    with db.conn() as c:
        pag = _pagamento(c, mes, pagador_id)
        if not pag:
            raise HTTPException(404, "Pagamento não existe.")
        if pag["recebedor_id"] != u["id"]:
            raise HTTPException(403, f"Só {pag['recebedor']} pode anular este pagamento.")
        c.execute("DELETE FROM pagamentos WHERE id = ?", (pag["id"],))
    return {"ok": True}


class Config(BaseModel):
    preco_cent: int | None = Field(default=None, ge=1, le=10_000)
    stock_baixo: int | None = Field(default=None, ge=0, le=10_000)


@app.put("/api/config")
def alterar_config(body: Config, u: dict = Depends(utilizador_actual)):
    with db.conn() as c:
        if body.preco_cent is not None:
            db.set_config(c, "preco_cent", str(body.preco_cent))
        if body.stock_baixo is not None:
            db.set_config(c, "stock_baixo", str(body.stock_baixo))
    return {"ok": True}


# ---------- front-end ----------

@app.get("/", include_in_schema=False)
def index():
    return FileResponse(STATIC / "index.html")


app.mount("/static", StaticFiles(directory=STATIC), name="static")
