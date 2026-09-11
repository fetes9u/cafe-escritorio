"""Contract tests between the client (app/static/app.js) and the server
(app/main.py).

Two real bugs slipped past the existing suite because the backend lane
tested only what the server claims to return and the frontend lane tested
only the static files served, never the join between them: a field name
that matched on one side and not the other, and a payload shape the server
never actually parsed. Every test here extracts what the client actually
does from app.js/index.html with a regex instead of retyping a field name,
event name or route by hand, so changing either side without changing the
other is what makes the test fail, not a hand-maintained expectation.
"""
import re

from app.main import STATIC, app
from tests.conftest import regista


# ---------- GET /api/push/chave: the field the client reads must exist ----------

def _campo_lido_pela_chave_vapid() -> str:
    """The name of the field app.js reads off the JSON response of
    GET /api/push/chave (`chaveVapid = dados.<campo>`), extracted from the
    source instead of hardcoded, so renaming the field on either side (client
    or server) is what breaks this test, not the test itself going stale."""
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    m = re.search(r"chaveVapid\s*=\s*dados\.(\w+)", js)
    assert m, "could not find `chaveVapid = dados.<campo>` in app.js; did the client change?"
    return m.group(1)


def test_o_campo_que_o_cliente_le_da_chave_existe_na_resposta_real(cliente, monkeypatch):
    monkeypatch.setenv("CAFE_VAPID_PUBLIC", "chave-publica-de-teste")
    campo = _campo_lido_pela_chave_vapid()

    resposta = cliente.get("/api/push/chave").json()

    assert campo in resposta, (
        f"app.js reads `dados.{campo}` from GET /api/push/chave, but the "
        f"server's response has no such field: {sorted(resposta)}"
    )
    assert resposta[campo] == "chave-publica-de-teste"


# ---------- PUT /api/notificacoes/preferencias: the exact shape the client sends ----------

def _eventos_do_formulario() -> list[str]:
    """The event names the notification-settings form in index.html carries
    (`data-evento="..."`), the same set app.js's change handler iterates over
    to build the {evento: booleano} map it PUTs."""
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    eventos = re.findall(r'data-evento="(\w+)"', html)
    assert eventos, "could not find any data-evento attribute in index.html"
    return eventos


def test_o_mapa_que_o_cliente_monta_fica_mesmo_gravado(cliente):
    """Sends exactly what app.js's form change handler assembles: a map of
    every event in the form to its checked state, never a {"desligados": [...]}
    list. A 200 alone does not prove anything was written, so this reads the
    preference back through GET afterwards."""
    a = regista(cliente, "Ana")
    eventos = _eventos_do_formulario()

    desligados = set(eventos[::2])  # turn off every other toggle, to cover both states
    mapa = {evento: evento not in desligados for evento in eventos}

    r = a.put("/api/notificacoes/preferencias", json=mapa)
    assert r.status_code == 200

    persistido = a.get("/api/notificacoes/preferencias").json()
    for evento in eventos:
        assert persistido[evento] == mapa[evento], (
            f"evento={evento}: PUT enviou {mapa[evento]}, GET leu de volta {persistido[evento]}"
        )


# ---------- every /api/... route the client calls must exist in the app ----------

def _rotas_chamadas_no_app_js() -> set[tuple[str, str]]:
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    rotas: set[tuple[str, str]] = set()
    for metodo, caminho in re.findall(r'api\("([A-Z]+)",\s*"([^"]*)"', js):
        rotas.add((metodo, "/api" + caminho))
    for caminho in re.findall(r'fetch\("(/api/[^"]+)"', js):
        rotas.add(("GET", caminho))
    assert rotas, "could not find any api()/fetch() call in app.js"
    return rotas


def _rotas_da_app() -> set[tuple[str, str]]:
    rotas: set[tuple[str, str]] = set()
    for route in app.routes:
        metodos = getattr(route, "methods", None)
        path = getattr(route, "path", None)
        if not metodos or not path:
            continue
        for metodo in metodos:
            if metodo == "HEAD":
                continue
            rotas.add((metodo, path))
    return rotas


def test_todas_as_rotas_chamadas_pelo_app_js_existem_na_app():
    chamadas = _rotas_chamadas_no_app_js()
    existentes = _rotas_da_app()
    em_falta = chamadas - existentes
    assert not em_falta, f"app.js calls routes that do not exist in the app: {sorted(em_falta)}"
