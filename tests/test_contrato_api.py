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
import json
import re
from pathlib import Path
from unittest.mock import MagicMock

from app import main as app_main
from app.main import EVENTOS, STATIC, app
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


def _verifica_payload_do_cliente_para_notificacoes() -> str:
    """Verify that app.js sends a map of eventos (keyed by chk.dataset.evento, valued by
    chk.checked) to PUT /notificacoes/preferencias, NOT a {"desligados": [...]} list.

    Extracts the payload structure directly from the form change handler in app.js.
    Returns the field name used for the event key (expected to be "evento"), or raises
    AssertionError if the structure doesn't match, with a message explaining the
    client/server shape mismatch."""
    js = (STATIC / "app.js").read_text(encoding="utf-8")

    # Must find: prefs[chk.dataset.<evento>] = chk.checked
    # This proves client builds {evento: boolean}, not {desligados: [...]}.
    m = re.search(r'prefs\[chk\.dataset\.(\w+)\]\s*=\s*chk\.(checked)', js)
    assert m, (
        "Client payload structure mismatch: app.js form change handler does not build "
        "`prefs[chk.dataset.<field>] = chk.checked`. Is the client sending "
        "`{desligados: [...]}` or some other structure instead of an event map?"
    )

    event_field = m.group(1)
    value_field = m.group(2)
    assert value_field == "checked", (
        f"Expected checkbox value from `.checked`, but client uses `.{value_field}`"
    )

    return event_field


def test_o_mapa_que_o_cliente_monta_fica_mesmo_gravado(cliente):
    """Sends exactly what app.js's form change handler assembles: a map of
    every event in the form to its checked state, never a {"desligados": [...]}
    list. A 200 alone does not prove anything was written, so this reads the
    preference back through GET afterwards."""

    # Verify app.js builds the payload as a map, not a list
    event_field = _verifica_payload_do_cliente_para_notificacoes()
    assert event_field == "evento", (
        f"Client and server disagree on payload SHAPE: app.js uses field `{event_field}` "
        f"but server expects `evento`. The payload is a map keyed by the event name; "
        f"this mismatch means the client's change will be silently dropped."
    )

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


# ---------- push payload: every field sw.js reads must be present, and non-empty ----------

def _campos_lidos_pelo_push_handler() -> set[str]:
    """The fields the `push` listener in sw.js reads off the pushed JSON
    (`dados.<campo>`), extracted from the source instead of hardcoded, so a
    field renamed or removed on either side is what breaks this test."""
    js = (STATIC / "sw.js").read_text(encoding="utf-8")
    m = re.search(r'self\.addEventListener\("push".*?\n\}\);', js, re.S)
    assert m, "could not find the `push` event listener in sw.js; did it change?"
    campos = set(re.findall(r"dados\.(\w+)", m.group(0)))
    assert campos, "could not find any `dados.<campo>` read in the push handler in sw.js"
    return campos


def test_a_notificacao_enviada_tem_todos_os_campos_que_o_sw_le_para_todos_os_eventos(cliente, monkeypatch):
    """For every evento the server can send, the payload actually put on the
    wire must carry every field sw.js's push handler reads, and titulo/corpo
    must not just be present but non-empty: a payload with the right keys and
    an empty value is the same bug (empty notification) wearing a different
    hat."""
    monkeypatch.setenv("CAFE_VAPID_PRIVATE", "chave-privada-de-teste")
    monkeypatch.setenv("CAFE_VAPID_PUBLIC", "chave-publica-de-teste")
    monkeypatch.setenv("CAFE_VAPID_CONTACTO", "mailto:teste@exemplo.pt")
    monkeypatch.setattr(app_main, "_vapid_instance", lambda: object())
    mock = MagicMock()
    monkeypatch.setattr(app_main, "webpush", mock)

    destinatario = regista(cliente, "Ana")
    destinatario.post(
        "/api/push/subscricoes",
        json={"endpoint": "https://push.example/ana", "p256dh": "p", "auth": "a"},
    )
    autor_id = 0  # never matches the recipient's id, so Ana always gets notified

    campos_lidos = _campos_lidos_pelo_push_handler()

    for evento in EVENTOS:
        mock.reset_mock()
        app_main._enviar_notificacao(evento, f"mensagem de teste para {evento}", autor_id)

        mock.assert_called_once()
        payload = json.loads(mock.call_args.kwargs["data"])

        em_falta = campos_lidos - set(payload)
        assert not em_falta, (
            f"evento={evento}: sw.js reads {sorted(campos_lidos)} from the pushed JSON, "
            f"but the server's payload is missing {sorted(em_falta)}: {payload}"
        )
        assert isinstance(payload.get("titulo"), str) and payload["titulo"], (
            f"evento={evento}: `titulo` must be a non-empty string, got {payload.get('titulo')!r}"
        )
        assert isinstance(payload.get("corpo"), str) and payload["corpo"], (
            f"evento={evento}: `corpo` must be a non-empty string, got {payload.get('corpo')!r}"
        )


# ---------- POST /api/cafe: the offline queue seam ----------
#
# The offline feature (see docs/superpowers/specs/2026-09-11-offline-e-sincronizacao-design.md)
# introduced a new client/server seam: the coffee button and the queue flush
# both build a request body by hand and the server reads it back through the
# NovoCafe model. Everything below is extracted from app.js/main.py with a
# regex, never retyped, so a rename on either side is what breaks these
# tests, not a hand-maintained expectation.

def _texto_main() -> str:
    """Full source of app/main.py, read directly so the server-side field
    names checked below come from the file the server actually runs,
    never retyped by hand."""
    return Path(app_main.__file__).read_text(encoding="utf-8")


def _campos_de_objeto_js(literal: str) -> set[str]:
    """Given the text between `{` and `}` of a JS object literal, return the
    set of field names it assigns, whether written as `campo: valor` or as
    shorthand `campo`."""
    campos = set()
    for parte in literal.split(","):
        parte = parte.strip()
        if not parte:
            continue
        campos.add(parte.split(":")[0].strip())
    return campos


def _corpos_enviados_para_post_cafe() -> list[set[str]]:
    """The field names of the object each of app.js's two POST /api/cafe call
    sites sends: the coffee button (`btn-cafe`'s onclick) and the queue flush
    (`sincronizarFila`). Both are extracted from the source, never retyped,
    so a divergence between the two sites, itself the bug the spec calls out
    in section 8, or a rename on either side is what breaks this, not a
    hand-maintained list."""
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    chamadas = re.findall(r'api\("POST",\s*"/cafe",\s*\{([^}]*)\}\)', js)
    assert len(chamadas) == 2, (
        f"expected exactly 2 call sites for POST /api/cafe in app.js (button click and "
        f"queue flush), found {len(chamadas)}; did a call site get added or removed? "
        f"update this test if that change was intentional"
    )
    return [_campos_de_objeto_js(corpo) for corpo in chamadas]


def _campos_aceites_por_novo_cafe() -> set[str]:
    """The field names the server's NovoCafe pydantic model declares, i.e.
    what POST /api/cafe actually accepts, extracted from app/main.py's
    source instead of retyped."""
    main_src = _texto_main()
    # Capture only the class body: consecutive indented (or blank) lines
    # right after the class header, stopping at the first line back at
    # column 0. A lazy match up to "\nclass " is not enough, since it also
    # swallows every module-level function between NovoCafe and the next
    # class, including unrelated `try:` blocks indented 4 spaces inside them.
    m = re.search(r'class NovoCafe\(BaseModel\):\n((?:[ \t]+.*\n|\n)+)', main_src)
    assert m, "could not find `class NovoCafe(BaseModel):` in app/main.py; did the request model move or get renamed?"
    campos = set(re.findall(r'^ {4}(\w+):', m.group(1), re.M))
    assert campos, "found NovoCafe but no field declarations inside it"
    return campos


def test_os_dois_pontos_de_chamada_do_post_cafe_concordam_entre_si():
    """The button click and the queue flush must send the exact same field
    names, since a divergence between the two client sites is itself the bug
    this file exists to catch (spec section 8)."""
    sites = _corpos_enviados_para_post_cafe()
    assert sites[0] == sites[1], (
        f"app.js's two POST /api/cafe call sites disagree on which fields they send: "
        f"button click sends {sorted(sites[0])}, queue flush sends {sorted(sites[1])}"
    )


def test_o_servidor_aceita_o_corpo_que_o_cliente_envia_no_post_cafe_e_grava_o_cafe(cliente):
    """Sends exactly what app.js's own call sites build (extracted, not
    retyped) to the real POST /api/cafe endpoint via TestClient, and checks
    the coffee was actually recorded, not just that the request was accepted."""
    campos_cliente = _corpos_enviados_para_post_cafe()[0]
    campos_servidor = _campos_aceites_por_novo_cafe()

    em_falta = campos_cliente - campos_servidor
    assert not em_falta, (
        f"app.js sends {sorted(campos_cliente)} on POST /api/cafe, but the server's "
        f"NovoCafe model does not accept {sorted(em_falta)} (it accepts {sorted(campos_servidor)})"
    )

    a = regista(cliente, "Rita")
    antes = a.get("/api/eu").json()

    valores = {"cliente_id": "11111111-1111-1111-1111-111111111111", "em": "2026-09-11T09:00:00+00:00"}
    corpo = {campo: valores[campo] for campo in campos_cliente}

    r = a.post("/api/cafe", json=corpo)
    assert r.status_code in (200, 201), r.text

    depois = a.get("/api/eu").json()
    assert depois["cafes"] == antes["cafes"] + 1, (
        f"POST /api/cafe with the client's own body {corpo} was accepted but did not "
        f"record a coffee: before={antes['cafes']}, after={depois['cafes']}"
    )


def test_o_cliente_nao_le_nenhum_campo_da_resposta_do_post_cafe():
    """Both POST /api/cafe call sites in app.js (the button click and the
    queue flush) await the call without ever assigning its result to a
    variable: the coffee count is updated optimistically before the call is
    even made, not from the response body. There is nothing to assert a
    field of here, so this asserts the emptiness explicitly instead of
    silently checking nothing: if either call site starts capturing the
    response (`const r = await api(...)`, a destructuring assignment, or
    `.then(...)`), this regex catches it and this test must be extended to
    assert on the field(s) then read (per spec section 5.1: `ok`,
    `cliente_id`, `em`, `duplicado`)."""
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    chamadas_com_atribuicao = re.findall(r'=\s*await\s+api\("POST",\s*"/cafe"', js)
    assert not chamadas_com_atribuicao, (
        f"app.js now assigns the response of POST /api/cafe to something "
        f"({chamadas_com_atribuicao!r}); this test file must be updated to assert on "
        f"the field(s) the client reads from it, per spec section 5.1"
    )


# ---------- IndexedDB `fila`: the offline queue record shape ----------

def _keypath_da_fila() -> str:
    """The keyPath app.js declares for the `fila` object store, extracted
    from the `createObjectStore` call in `abrirDB`."""
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    m = re.search(r'createObjectStore\("fila",\s*\{\s*keyPath:\s*"(\w+)"', js)
    assert m, "could not find the `fila` object store's keyPath declaration in app.js; did abrirDB change?"
    return m.group(1)


def _campos_do_registo_da_fila() -> set[str]:
    """The field names of the record app.js actually writes to the `fila`
    store (`idbPut("fila", {...})` in the coffee button handler)."""
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    m = re.search(r'idbPut\("fila",\s*\{([^}]*)\}\)', js)
    assert m, 'could not find the idbPut("fila", {...}) call in app.js that builds a queue record'
    return _campos_de_objeto_js(m.group(1))


def test_o_registo_da_fila_bate_certo_com_a_spec_e_com_o_campo_que_o_servidor_le():
    """Spec section 5.2 fixes the `fila` record shape as
    {cliente_id, tipo, em, criado_em} keyed by cliente_id. This checks
    app.js's actual keyPath and record fields against those names (the spec
    names are hardcoded here because they are normative, not extracted; the
    client's own source is what is extracted), and separately that the
    keyPath the client stores under is the same field name the server reads
    off the request body, since idempotency depends on both sides meaning
    the same thing by "cliente_id"."""
    keypath = _keypath_da_fila()
    campos = _campos_do_registo_da_fila()

    esperado = {"cliente_id", "tipo", "em", "criado_em"}  # spec secção 5.2, normativo
    assert campos == esperado, (
        f"app.js's `fila` queue record has fields {sorted(campos)}, but spec section 5.2 "
        f"fixes it as {sorted(esperado)}"
    )
    assert keypath == "cliente_id", (
        f"app.js's `fila` object store has keyPath {keypath!r}, but spec section 5.2 fixes "
        f"it as 'cliente_id'"
    )

    campos_servidor = _campos_aceites_por_novo_cafe()
    assert keypath in campos_servidor, (
        f"the `fila` store's keyPath is {keypath!r}, but the server's NovoCafe model reads "
        f"no field of that name (it accepts {sorted(campos_servidor)}); a queued record can "
        f"never be matched for idempotency if the two sides disagree on this name"
    )


# ---------- idempotency across the real seam ----------

def test_o_mesmo_cliente_id_duas_vezes_da_um_cafe_so_e_a_segunda_diz_duplicado(cliente):
    """End to end proof that the client's retry is safe: builds the body
    from the field names app.js's own call site sends (not hardcoded),
    POSTs it twice, and checks exactly one coffee exists and the second
    response reports `duplicado`."""
    campos_cliente = _corpos_enviados_para_post_cafe()[0]
    a = regista(cliente, "Duplicado")

    valores = {"cliente_id": "22222222-2222-2222-2222-222222222222", "em": "2026-09-11T09:00:00+00:00"}
    corpo = {campo: valores[campo] for campo in campos_cliente}

    r1 = a.post("/api/cafe", json=corpo)
    assert r1.status_code == 201, r1.text
    assert r1.json()["duplicado"] is False, r1.json()

    r2 = a.post("/api/cafe", json=corpo)
    assert r2.status_code == 200, r2.text
    assert r2.json()["duplicado"] is True, (
        f"the same cliente_id sent twice must report duplicado=true on the second "
        f"response, got {r2.json()}"
    )

    eu = a.get("/api/eu").json()
    assert eu["cafes"] == 1, (
        f"the same cliente_id sent twice recorded {eu['cafes']} coffees instead of exactly one"
    )
